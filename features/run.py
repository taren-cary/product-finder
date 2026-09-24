"""Features step: compute each TikTok-fit concept's metrics for the current week.

For every active concept judged a TikTok fit, this gathers what we know and
writes one row to gapfinder.concept_weekly (week_start = this week's Monday).
It reruns safely; each run overwrites the week's row with the latest numbers.

TikTok comes first (keyword = the concept's main search term):

  velocity_tiktokshop  TikTok Shop revenue for the keyword (Kalodata keyword
                       search), weekly growth between the last two checks.
                       Until there are two checks: Kalodata's own 7-day growth
                       from the latest check, or from this week's TikTok Shop
                       discovery lists if the concept appeared in them.
  velocity_shopvideos  Views of shoppable TikTok videos for the keyword, weekly growth.
  velocity_tiktok      TikTok hashtag total views, weekly growth.
  tiktok_momentum      Weighted blend of those three (weights in config.yaml).
  tiktok_saturation    0 (nobody selling) to 1 (crowded): sellers, creators,
                       TikTok Shop revenue, top-3 seller share, hashtag views.

Confirmation from outside TikTok:

  velocity_google      Google Trends interest, last 4 weeks vs the 4 before
                       (from the latest check within the last 8 weeks).
  velocity_amazon      Amazon Best Sellers rank gains / new entries vs last week.
  velocity_reddit      Buy-intent Reddit posts, last 28 days vs the 28 before.
  confirmations        How many of those three are rising.

  lead_lag_gap         Combined demand growth minus TikTok Shop supply growth
                       (sellers + creators). Positive = demand rising faster
                       than TikTok Shop is filling up.
  sustained_factor     0.5 (brief rise) to 1.0 (steady climb): from TikTok Shop
                       revenue history once there are 3+ checks, else Google.
  spike_risk           The Google curve shows a one-off spike.
  paid_share           Share of shoppable-video views that come from ads.

Checks happen on a rotation (collectors/keywords.py), so growth between two
checks a few weeks apart is converted to a per-week rate. All thresholds and
weights are in config.yaml under "features".
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

TIKTOK_SOURCES = ("tiktokshop", "shopvideos", "tiktok")
OUTSIDE_SOURCES = ("google", "amazon", "reddit")
ALL_SOURCES = TIKTOK_SOURCES + OUTSIDE_SOURCES


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


def weekly_growth(cur_value, cur_date, prev_value, prev_date) -> float | None:
    """Growth between two checks, converted to a per-week rate.
    E.g. +21% over two weeks is about +10% per week."""
    if cur_value is None or prev_value is None or prev_value <= 0 or cur_value < 0:
        return None
    days = (cur_date - prev_date).days
    if days < 6:
        return None
    return (cur_value / prev_value) ** (7 / days) - 1


def sustained_from_series(values: list[float]) -> float | None:
    """0.5 to 1.0 from how many of the recent check-to-check changes were
    increases. Needs at least 3 checks."""
    if len(values) < 3:
        return None
    recent = values[-5:]
    ups = sum(1 for a, b in zip(recent, recent[1:]) if b > a)
    return 0.5 + 0.5 * ups / (len(recent) - 1)


def latest_growth(series: list[tuple]) -> float | None:
    """Per-week growth from the previous check (at least 6 days earlier) to the latest.
    series: [(date, value)] oldest first."""
    if len(series) < 2:
        return None
    cur_date, cur_value = series[-1]
    earlier = [(d, v) for d, v in series[:-1] if (cur_date - d).days >= 6]
    if not earlier:
        return None
    prev_date, prev_value = earlier[-1]
    return weekly_growth(cur_value, cur_date, prev_value, prev_date)


# --- Loading data -----------------------------------------------------------

def _latest_raw(conn, sources, start: date, end: date) -> dict:
    """Most recent row per (source, request_key) between start and end."""
    rows = conn.execute(
        """
        select distinct on (source, request_key) source, request_key, snapshot_date, payload
        from gapfinder.raw_responses
        where source = any(%s) and snapshot_date between %s and %s
        order by source, request_key, snapshot_date desc
        """,
        (list(sources), start, end),
    ).fetchall()
    return {(r["source"], r["request_key"]): r for r in rows}


def _history(conn, since: date) -> dict:
    """(source, request_key) -> checks oldest first, each with: TikTok Shop
    revenue and seller count, shoppable-video views and creator count, or
    hashtag total views."""
    rows = conn.execute(
        """
        select r.source, r.request_key, r.snapshot_date,
               (r.payload->>'total_views')::numeric as hashtag_views,
               a.revenue, a.sellers, a.views, a.creators
        from gapfinder.raw_responses r
        left join lateral (
            select sum(coalesce((d->>'revenue')::numeric, 0)) as revenue,
                   count(distinct d->>'seller_id') as sellers,
                   sum(coalesce((d->>'views')::numeric, 0)) as views,
                   count(distinct d->>'belonged_creator_id') as creators
            from jsonb_array_elements(case when r.source = 'kalodata_keywords'
                                           then r.payload->'data' else '[]'::jsonb end) d
        ) a on true
        where r.source in ('kalodata_keywords', 'tiktok') and r.snapshot_date >= %s
        order by r.snapshot_date
        """,
        (since,),
    ).fetchall()
    out = defaultdict(list)
    for r in rows:
        out[(r["source"], r["request_key"])].append(r)
    return out


def _series(history, source, key, field) -> list[tuple]:
    return [(r["snapshot_date"], float(r[field])) for r in history.get((source, key), [])
            if r[field] is not None]


def _tiktok_lists(conn, week_start_: date) -> dict:
    """concept_id -> (revenue-weighted Kalodata growth, number of listings) for
    concepts that appeared in this week's TikTok Shop discovery lists."""
    rows = conn.execute(
        """
        select m.concept_id, count(*) as listed,
               sum(least((s.metrics->>'revenue_growth_rate')::numeric, 500)
                   * (s.metrics->>'revenue')::numeric)
                 / nullif(sum((s.metrics->>'revenue')::numeric)
                          filter (where s.metrics ? 'revenue_growth_rate'), 0) / 100 as growth
        from gapfinder.item_concept_map m
        join gapfinder.items i on i.id = m.item_id and i.source in ('kalodata', 'kalodata_video')
        join gapfinder.item_snapshots s on s.item_id = i.id and s.snapshot_date >= %s
        group by m.concept_id
        """,
        (week_start_,),
    ).fetchall()
    return {r["concept_id"]: (float(r["growth"]) if r["growth"] is not None else None, r["listed"])
            for r in rows}


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
        where merged_into_id is null and review_status <> 'rejected' and tiktok_fit is true
        """
    ).fetchall()
    tiktok_latest = _latest_raw(conn, ("tiktok", "kalodata_keywords"),
                                snapshot_date - timedelta(weeks=cfg["tiktok_max_age_weeks"]), cur_end)
    google_latest = _latest_raw(conn, ("google_trends",),
                                snapshot_date - timedelta(weeks=cfg["google_max_age_weeks"]), cur_end)
    history = _history(conn, snapshot_date - timedelta(weeks=12))
    lists = _tiktok_lists(conn, cur_start)
    amazon = _amazon_ranks(conn, cur_start, cur_end, prev_start, prev_end)
    reddit = _reddit_mentions(conn, snapshot_date)

    rows = []
    for c in concepts:
        kw = concept_keyword(c)
        tag = to_hashtag(kw)
        pkey, vkey, hkey = f"products:{kw}", f"videos:{kw}", f"hashtag:{tag}"
        details = {"keyword": kw, "hashtag": tag}

        # --- TikTok: the core signal ---
        prod = tiktok_latest.get(("kalodata_keywords", pkey))
        vids = tiktok_latest.get(("kalodata_keywords", vkey))
        tag_row = tiktok_latest.get(("tiktok", hkey))
        shop = shop_stats((prod["payload"].get("data") or []) if prod else [])
        vstats = video_stats((vids["payload"].get("data") or []) if vids else [])
        hashtag_views = (tag_row["payload"] or {}).get("total_views") if tag_row else None
        has_shop = shop["products"] > 0
        list_growth, listed = lists.get(c["id"], (None, 0))

        v_shop, basis = latest_growth(_series(history, "kalodata_keywords", pkey, "revenue")), "our history"
        if v_shop is None and has_shop:
            v_shop, basis = shop["kalodata_growth"], "Kalodata 7-day growth"
        if v_shop is None and list_growth is not None:
            v_shop, basis = list_growth, "this week's TikTok Shop lists"
        v_videos = latest_growth(_series(history, "kalodata_keywords", vkey, "views"))
        v_tiktok = latest_growth(_series(history, "tiktok", hkey, "hashtag_views"))
        tiktok_v = {"tiktokshop": v_shop, "shopvideos": v_videos, "tiktok": v_tiktok}
        momentum = weighted_velocity(tiktok_v, cfg["tiktok_weights"], cfg["velocity_cap"])

        details["shop"] = shop | {"growth_basis": basis if v_shop is not None else None,
                                  "checked": str(prod["snapshot_date"]) if prod else None}
        details["videos"] = vstats
        details["tiktok_listings_this_week"] = listed

        # --- Confirmation from outside TikTok ---
        g_row = google_latest.get(("google_trends", f"trends:{kw}"))
        g = google_metrics((g_row["payload"] if g_row else {}) or {}, cfg)
        g["checked"] = str(g_row["snapshot_date"]) if g_row else None
        details["google"] = g
        v_amazon = amazon_velocity(amazon.get(c["id"], []), cfg["amazon_new_entry_value"])
        cur_mentions, prev_mentions = reddit.get(c["id"], (0, 0))
        v_reddit = mention_velocity(cur_mentions, prev_mentions, cfg["reddit_min_mentions"])
        details["reddit"] = {"mentions_28d": cur_mentions, "mentions_prev_28d": prev_mentions}
        outside_v = {"google": g["velocity"], "amazon": v_amazon, "reddit": v_reddit}
        outside = weighted_velocity(outside_v, cfg["outside_weights"], cfg["velocity_cap"])
        confirmations = sum(1 for v in outside_v.values()
                            if v is not None and v >= cfg["rising_threshold"])

        all_v = {**tiktok_v, **outside_v}
        breadth = sum(1 for v in all_v.values() if v is not None and v >= cfg["rising_threshold"])

        sat = saturation({
            "sellers": shop["sellers"] if has_shop else None,
            "creators": vstats["creators"] if vstats["videos"] else None,
            "revenue": shop["revenue"] if has_shop else None,
            "top3_share": shop["top3_share"],
            "hashtag_views": hashtag_views,
        }, cfg)

        # Supply growth on TikTok Shop: sellers + creators, per week.
        creators_by_date = dict(_series(history, "kalodata_keywords", vkey, "creators"))
        supply = [(d, v + creators_by_date.get(d, 0))
                  for d, v in _series(history, "kalodata_keywords", pkey, "sellers")]
        supply_growth = latest_growth(supply)
        details["supply_growth"] = supply_growth
        demand = weighted_velocity(all_v, cfg["demand_weights"], cfg["velocity_cap"])
        gap = None if demand is None else demand - max(0.0, supply_growth or 0.0)

        sustained = sustained_from_series(
            [v for _, v in _series(history, "kalodata_keywords", pkey, "revenue")])
        details["sustained_basis"] = "TikTok Shop revenue" if sustained is not None else None
        if sustained is None and g["sustained_weeks"] is not None:
            sustained = 0.5 + 0.5 * g["sustained_weeks"] / 8
            details["sustained_basis"] = "Google Trends"

        rows.append({
            "concept_id": c["id"], "week_start": cur_start,
            "velocity_tiktokshop": v_shop, "velocity_shopvideos": v_videos, "velocity_tiktok": v_tiktok,
            "tiktok_momentum": momentum, "on_tiktok_lists": listed > 0,
            "velocity_google": g["velocity"], "velocity_amazon": v_amazon, "velocity_reddit": v_reddit,
            "confirmations": confirmations, "outside_velocity": outside, "demand_breadth": breadth,
            "tiktok_saturation": sat,
            "sellers": shop["sellers"] if has_shop else None,
            "creators": vstats["creators"] if vstats["videos"] else None,
            "shop_revenue_7d": shop["revenue"] if has_shop else None,
            "top3_seller_share": shop["top3_share"],
            "hashtag_views": hashtag_views,
            "lead_lag_gap": gap, "paid_share": vstats["paid_share"],
            "spike_risk": g["spike_risk"], "sustained_factor": sustained,
            "details": Jsonb(_round_floats(details)),
        })

    # Concepts that are no longer a TikTok fit (or were rejected/merged) keep
    # their older weeks but get no row for this week.
    conn.execute(
        """
        delete from gapfinder.concept_weekly w using gapfinder.concepts c
        where w.concept_id = c.id and w.week_start = %s
          and (c.tiktok_fit is not true or c.review_status = 'rejected' or c.merged_into_id is not null)
        """,
        (cur_start,),
    )
    if rows:
        cols = list(rows[0].keys())
        updates = ", ".join(f"{k} = excluded.{k}" for k in cols if k not in ("concept_id", "week_start"))
        with conn.cursor() as cur:
            cur.executemany(
                f"""
                insert into gapfinder.concept_weekly ({", ".join(cols)})
                values ({", ".join(f"%({k})s" for k in cols)})
                on conflict (concept_id, week_start) do update set {updates}, computed_at = now()
                """,
                rows,
            )
    log.info("Computed metrics for %d TikTok-fit concepts (week of %s)", len(rows), cur_start)
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
