"""Features step: compute each concept's metrics for the current week.

For every active concept, this gathers what we know from each source and
writes one row to gapfinder.concept_weekly (week_start = this week's Monday).
It reruns safely during the week; each run overwrites the week's row with
the latest numbers.

Where each metric comes from (keyword = the concept's main search term):

  velocity_google      Google Trends interest: last 4 weeks vs the 4 before.
  velocity_amazon      Amazon Best Sellers: average rank gain of the concept's
                       products vs last week; a product new to the list counts
                       as a gain of +0.5. Needs two weeks of Amazon data.
  velocity_reddit      Buy-intent Reddit posts: last 28 days vs the 28 before.
  velocity_tiktok      TikTok hashtag total views, week over week.
  velocity_tiktokshop  Revenue of the top TikTok Shop products for the keyword
                       (Kalodata), week over week. Until there are two weeks of
                       data, Kalodata's own 7-day growth figure is used instead.

  tiktok_saturation    0 (nobody selling) to 1 (crowded), a weighted blend of:
                       sellers, creators, TikTok Shop revenue, top-3 seller
                       share, and hashtag views. Weights in config.yaml.
  lead_lag_gap         outside demand growth (Google, Amazon, Reddit) minus
                       TikTok Shop supply growth (sellers + creators, week over
                       week). Positive = demand is rising faster than TikTok
                       Shop is filling up. THE key metric.
  spike_risk /         From the 12-month Google Trends series: is the recent rise
  sustained_factor     a steady climb over several weeks, or one sudden spike?
  paid_share           Share of shoppable-video views that come from ads.

All thresholds and weights live in config.yaml under "features".
"""

import logging
import math
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean, median

from psycopg.types.json import Jsonb

from collectors.keywords import concept_keyword, to_hashtag
from core.config import settings

log = logging.getLogger(__name__)

OUTSIDE_SOURCES = ("google", "amazon", "reddit")
ALL_SOURCES = ("google", "amazon", "reddit", "tiktok", "tiktokshop")


def week_start(d: date) -> date:
    """The Monday of d's week."""
    return d - timedelta(days=d.weekday())


# --- Metric calculations (pure functions: numbers in, numbers out) ---------

def growth(current, previous) -> float | None:
    """(current / previous) - 1, or None if it can't be computed."""
    if current is None or previous is None or previous <= 0:
        return None
    return current / previous - 1


def google_metrics(payload: dict, cfg: dict) -> dict:
    """Velocity, sustained climb and spike check from a Trends payload."""
    points = [p for p in (payload.get("interestOverTime") or []) if not p.get("isPartial")]
    series = [float(p.get("value") or 0) for p in points]
    out = {"velocity": None, "sustained_weeks": None, "spike_risk": None,
           "recent_interest": None, "points": len(series)}
    if len(series) < 20:
        return out

    last4, prev4 = series[-4:], series[-8:-4]
    out["recent_interest"] = round(mean(last4), 1)
    # Tiny numbers (Trends values 0-3) swing wildly; don't read growth into them.
    if max(mean(last4), mean(prev4)) >= cfg["google_min_interest"]:
        out["velocity"] = growth(mean(last4), mean(prev4))

    baseline = mean(series[-20:-8])   # the 3 months before the last 8 weeks
    if baseline > 0:
        out["sustained_weeks"] = sum(1 for x in series[-8:] if x > baseline * 1.05)
    last12 = series[-12:]
    typical = median(last12)
    out["spike_risk"] = bool(
        typical > 0
        and max(last4) >= cfg["spike_ratio"] * typical
        and (out["sustained_weeks"] or 0) <= 2
    )
    return out


def amazon_velocity(ranks: list[tuple], new_entry_value: float) -> float | None:
    """ranks: (current_rank, previous_rank) per product; previous None = new to the list.
    Returns the average rank gain, e.g. rank 20 -> 10 is +0.5."""
    gains = []
    for cur, prev in ranks:
        if cur is None:
            continue
        if prev is None:
            gains.append(new_entry_value)
        else:
            gains.append((prev - cur) / prev)
    return mean(gains) if gains else None


def mention_velocity(current: int, previous: int, min_mentions: int) -> float | None:
    """Growth in mentions; None when there are too few to mean anything."""
    if current < min_mentions and previous < min_mentions:
        return None
    return growth(current, max(previous, 1))


def shop_stats(products: list[dict]) -> dict:
    """Sellers, revenue and concentration from a Kalodata product search."""
    revenue_by_seller = defaultdict(float)
    total = 0.0
    for p in products:
        rev = float(p.get("revenue") or 0)
        total += rev
        revenue_by_seller[p.get("seller_id") or p.get("product_id")] += rev
    top3 = sum(sorted(revenue_by_seller.values(), reverse=True)[:3])
    # Kalodata's own growth figure (percent, 7 days vs the 7 before), revenue-weighted.
    weighted = [(min(float(p["revenue_growth_rate"]), 500.0), float(p.get("revenue") or 0))
                for p in products if p.get("revenue_growth_rate") is not None]
    weight = sum(w for _, w in weighted)
    kalodata_growth = (sum(g * w for g, w in weighted) / weight / 100) if weight > 0 else None
    return {
        "products": len(products),
        "sellers": len({p.get("seller_id") for p in products if p.get("seller_id")}),
        "revenue": round(total, 2),
        "top3_share": round(top3 / total, 3) if total > 0 else None,
        "kalodata_growth": kalodata_growth,
    }


def video_stats(videos: list[dict]) -> dict:
    """Creators, views and ad share from a Kalodata video search."""
    views = [float(v.get("views") or 0) for v in videos]
    total = sum(views)
    ad = sum(float(v.get("ad_view_ratio") or 0) / 100 * w for v, w in zip(videos, views))
    return {
        "videos": len(videos),
        "creators": len({v.get("belonged_creator_id") for v in videos if v.get("belonged_creator_id")}),
        "views": total,
        "paid_share": round(ad / total, 3) if total > 0 else None,
    }


def saturation(parts: dict, cfg: dict) -> float | None:
    """Blend the available saturation signals into one 0-1 number.
    Each count is put on a log scale against a reference level from config
    (e.g. 100 sellers = fully crowded)."""
    refs = cfg["saturation_reference"]
    weights = cfg["saturation_weights"]

    def log_scale(value, ref):
        if value is None:
            return None
        return min(1.0, math.log1p(max(value, 0)) / math.log1p(ref))

    scaled = {
        "sellers": log_scale(parts.get("sellers"), refs["sellers"]),
        "creators": log_scale(parts.get("creators"), refs["creators"]),
        "revenue": log_scale(parts.get("revenue"), refs["revenue_7d"]),
        "hashtag_views": log_scale(parts.get("hashtag_views"), refs["hashtag_views"]),
        "top3_share": parts.get("top3_share"),
    }
    available = {k: v for k, v in scaled.items() if v is not None}
    if not available:
        return None
    total_weight = sum(weights[k] for k in available)
    return round(sum(weights[k] * v for k, v in available.items()) / total_weight, 3)


def weighted_velocity(velocities: dict, weights: dict, cap: float) -> float | None:
    """Weighted average of the sources that have data, each clipped to [-1, cap]."""
    usable = {s: max(-1.0, min(v, cap)) for s, v in velocities.items() if v is not None}
    if not usable:
        return None
    total_weight = sum(weights[s] for s in usable)
    return sum(weights[s] * v for s, v in usable.items()) / total_weight


# --- Loading data -----------------------------------------------------------

def _latest_raw(conn, sources, start: date, end: date) -> dict:
    """Most recent payload per (source, request_key) between start and end."""
    rows = conn.execute(
        """
        select distinct on (source, request_key) source, request_key, payload
        from gapfinder.raw_responses
        where source = any(%s) and snapshot_date between %s and %s
        order by source, request_key, snapshot_date desc
        """,
        (list(sources), start, end),
    ).fetchall()
    return {(r["source"], r["request_key"]): r["payload"] for r in rows}


def _amazon_ranks(conn, cur_start, cur_end, prev_start, prev_end) -> dict:
    """concept_id -> [(current_rank, previous_rank)], only if last week has Amazon data."""
    had_last_week = conn.execute(
        "select 1 from gapfinder.raw_responses where source = 'amazon' and snapshot_date between %s and %s limit 1",
        (prev_start, prev_end),
    ).fetchone()
    if not had_last_week:
        return {}
    rows = conn.execute(
        """
        select m.concept_id,
               min(s.rank) filter (where s.snapshot_date between %s and %s) as cur_rank,
               min(s.rank) filter (where s.snapshot_date between %s and %s) as prev_rank
        from gapfinder.item_concept_map m
        join gapfinder.items i on i.id = m.item_id and i.source = 'amazon'
        join gapfinder.item_snapshots s on s.item_id = i.id
        where s.snapshot_date between %s and %s
        group by m.concept_id, i.id
        """,
        (cur_start, cur_end, prev_start, prev_end, prev_start, cur_end),
    ).fetchall()
    out = defaultdict(list)
    for r in rows:
        out[r["concept_id"]].append((r["cur_rank"], r["prev_rank"]))
    return out


def _reddit_mentions(conn, today: date) -> dict:
    """concept_id -> (mentions in the last 28 days, mentions in the 28 before)."""
    rows = conn.execute(
        """
        select m.concept_id,
               count(*) filter (where i.first_seen > %s) as cur,
               count(*) filter (where i.first_seen <= %s and i.first_seen > %s) as prev
        from gapfinder.item_concept_map m
        join gapfinder.items i on i.id = m.item_id and i.source = 'reddit'
        group by m.concept_id
        """,
        (today - timedelta(days=28), today - timedelta(days=28), today - timedelta(days=56)),
    ).fetchall()
    return {r["concept_id"]: (r["cur"], r["prev"]) for r in rows}


# --- The step ---------------------------------------------------------------

def run(conn, snapshot_date: date) -> dict:
    cfg = settings["features"]
    cur_start = week_start(snapshot_date)
    cur_end = cur_start + timedelta(days=6)
    prev_start, prev_end = cur_start - timedelta(days=7), cur_start - timedelta(days=1)

    concepts = conn.execute(
        """
        select id, name, keywords from gapfinder.concepts
        where merged_into_id is null and review_status <> 'rejected'
        """
    ).fetchall()
    enrichment = ("google_trends", "tiktok", "kalodata_keywords")
    cur = _latest_raw(conn, enrichment, cur_start, cur_end)
    prev = _latest_raw(conn, enrichment, prev_start, prev_end)
    amazon = _amazon_ranks(conn, cur_start, cur_end, prev_start, prev_end)
    reddit = _reddit_mentions(conn, snapshot_date)

    rows = []
    for c in concepts:
        kw = concept_keyword(c)
        tag = to_hashtag(kw)
        details = {"keyword": kw, "hashtag": tag}

        # Google Trends
        g = google_metrics(cur.get(("google_trends", f"trends:{kw}")) or {}, cfg)
        details["google"] = g

        # Amazon and Reddit (from the concept's own items)
        v_amazon = amazon_velocity(amazon.get(c["id"], []), cfg["amazon_new_entry_value"])
        cur_mentions, prev_mentions = reddit.get(c["id"], (0, 0))
        v_reddit = mention_velocity(cur_mentions, prev_mentions, cfg["reddit_min_mentions"])
        details["reddit"] = {"mentions_28d": cur_mentions, "mentions_prev_28d": prev_mentions}

        # TikTok hashtag
        tt_cur = cur.get(("tiktok", f"hashtag:{tag}")) or {}
        tt_prev = prev.get(("tiktok", f"hashtag:{tag}")) or {}
        hashtag_views = tt_cur.get("total_views")
        v_tiktok = growth(hashtag_views, tt_prev.get("total_views"))

        # TikTok Shop (Kalodata keyword search)
        shop_cur = shop_stats((cur.get(("kalodata_keywords", f"products:{kw}")) or {}).get("data") or [])
        shop_prev = shop_stats((prev.get(("kalodata_keywords", f"products:{kw}")) or {}).get("data") or [])
        vid_cur = video_stats((cur.get(("kalodata_keywords", f"videos:{kw}")) or {}).get("data") or [])
        vid_prev = video_stats((prev.get(("kalodata_keywords", f"videos:{kw}")) or {}).get("data") or [])
        has_shop = shop_cur["products"] > 0
        v_shop = growth(shop_cur["revenue"], shop_prev["revenue"]) if shop_prev["products"] else None
        if v_shop is None and has_shop:
            v_shop = shop_cur["kalodata_growth"]   # Kalodata's own 7-day growth until we have history
        details["shop"] = shop_cur
        details["videos"] = vid_cur

        velocities = {"google": g["velocity"], "amazon": v_amazon, "reddit": v_reddit,
                      "tiktok": v_tiktok, "tiktokshop": v_shop}
        breadth = sum(1 for v in velocities.values() if v is not None and v >= cfg["rising_threshold"])
        outside = weighted_velocity({s: velocities[s] for s in OUTSIDE_SOURCES},
                                    cfg["outside_weights"], cfg["velocity_cap"])

        sat = saturation({
            "sellers": shop_cur["sellers"] if has_shop else None,
            "creators": vid_cur["creators"] if vid_cur["videos"] else None,
            "revenue": shop_cur["revenue"] if has_shop else None,
            "top3_share": shop_cur["top3_share"],
            "hashtag_views": hashtag_views,
        }, cfg)

        # Supply growth on TikTok Shop: sellers + creators, week over week.
        supply_now = (shop_cur["sellers"] + vid_cur["creators"]) if has_shop else None
        supply_before = (shop_prev["sellers"] + vid_prev["creators"]) if shop_prev["products"] else None
        supply_growth = growth(supply_now, supply_before)
        details["supply_growth"] = supply_growth
        gap = None if outside is None else outside - max(0.0, supply_growth or 0.0)

        sustained = None
        if g["sustained_weeks"] is not None:
            sustained = 0.5 + 0.5 * g["sustained_weeks"] / 8

        rows.append({
            "concept_id": c["id"], "week_start": cur_start,
            "velocity_google": g["velocity"], "velocity_amazon": v_amazon,
            "velocity_reddit": v_reddit, "velocity_tiktok": v_tiktok, "velocity_tiktokshop": v_shop,
            "demand_breadth": breadth, "outside_velocity": outside,
            "tiktok_saturation": sat,
            "sellers": shop_cur["sellers"] if has_shop else None,
            "creators": vid_cur["creators"] if vid_cur["videos"] else None,
            "shop_revenue_7d": shop_cur["revenue"] if has_shop else None,
            "top3_seller_share": shop_cur["top3_share"],
            "hashtag_views": hashtag_views,
            "lead_lag_gap": gap, "paid_share": vid_cur["paid_share"],
            "spike_risk": g["spike_risk"], "sustained_factor": sustained,
            "details": Jsonb(_round_floats(details)),
        })

    with conn.cursor() as cur_:
        cur_.executemany(
            """
            insert into gapfinder.concept_weekly (
                concept_id, week_start, velocity_google, velocity_amazon, velocity_reddit,
                velocity_tiktok, velocity_tiktokshop, demand_breadth, outside_velocity,
                tiktok_saturation, sellers, creators, shop_revenue_7d, top3_seller_share,
                hashtag_views, lead_lag_gap, paid_share, spike_risk, sustained_factor, details)
            values (
                %(concept_id)s, %(week_start)s, %(velocity_google)s, %(velocity_amazon)s, %(velocity_reddit)s,
                %(velocity_tiktok)s, %(velocity_tiktokshop)s, %(demand_breadth)s, %(outside_velocity)s,
                %(tiktok_saturation)s, %(sellers)s, %(creators)s, %(shop_revenue_7d)s, %(top3_seller_share)s,
                %(hashtag_views)s, %(lead_lag_gap)s, %(paid_share)s, %(spike_risk)s, %(sustained_factor)s,
                %(details)s)
            on conflict (concept_id, week_start) do update set
                velocity_google = excluded.velocity_google, velocity_amazon = excluded.velocity_amazon,
                velocity_reddit = excluded.velocity_reddit, velocity_tiktok = excluded.velocity_tiktok,
                velocity_tiktokshop = excluded.velocity_tiktokshop, demand_breadth = excluded.demand_breadth,
                outside_velocity = excluded.outside_velocity, tiktok_saturation = excluded.tiktok_saturation,
                sellers = excluded.sellers, creators = excluded.creators,
                shop_revenue_7d = excluded.shop_revenue_7d, top3_seller_share = excluded.top3_seller_share,
                hashtag_views = excluded.hashtag_views, lead_lag_gap = excluded.lead_lag_gap,
                paid_share = excluded.paid_share, spike_risk = excluded.spike_risk,
                sustained_factor = excluded.sustained_factor, details = excluded.details,
                computed_at = now()
            """,
            rows,
        )
    log.info("Computed metrics for %d concepts (week of %s)", len(rows), cur_start)
    return {"records": len(rows)}


def _round_floats(value):
    """Round floats in nested details to keep the stored JSON readable."""
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, dict):
        return {k: _round_floats(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_round_floats(v) for v in value]
    return value
