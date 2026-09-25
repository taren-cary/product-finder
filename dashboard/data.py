"""Database reads and writes for the dashboard.

Reads are cached for a few minutes so clicking around is fast; every write
clears the cache so the page shows the change straight away.
"""

from datetime import date

import pandas as pd
import streamlit as st

from collectors.keywords import concept_keyword
from core.db import connect

CACHE_SECONDS = 60


def _df(sql: str, params=None) -> pd.DataFrame:
    with connect() as conn:
        rows = conn.execute(sql, params or ()).fetchall()
    df = pd.DataFrame(rows)
    # Postgres numeric columns arrive as Decimal; charts and tables want floats.
    for col in df.columns:
        if len(df) and df[col].map(lambda v: v.__class__.__name__ == "Decimal").any():
            df[col] = df[col].astype(float)
    return df


# --- Reads -------------------------------------------------------------------

@st.cache_data(ttl=CACHE_SECONDS)
def weeks() -> list[date]:
    df = _df("select distinct week_start from gapfinder.concept_weekly order by week_start desc")
    return list(df["week_start"]) if len(df) else []


@st.cache_data(ttl=CACHE_SECONDS)
def ranking(week: date) -> pd.DataFrame:
    """One row per active concept with this week's metrics."""
    return _df(
        """
        select c.id, c.name, c.category, c.keywords, c.review_status, c.created_at::date as first_spotted,
               c.tiktok_fit, c.tiktok_fit_reason,
               w.rank, w.opportunity_score, w.demand_breadth, w.outside_velocity,
               w.tiktok_momentum, w.velocity_shopvideos, w.confirmations, w.on_tiktok_lists,
               w.typical_price, w.price_floor, w.est_profit_per_unit, w.weekly_profit_potential,
               w.velocity_google, w.velocity_amazon, w.velocity_reddit, w.velocity_tiktok,
               w.velocity_tiktokshop, w.tiktok_saturation, w.sellers, w.creators,
               w.shop_revenue_7d, w.lead_lag_gap, w.paid_share, w.spike_risk, w.sustained_factor,
               (w.details->'crowding'->>'new_product_share')::numeric as new_product_share,
               (w.details->'shop'->>'revenue_per_active_seller')::numeric as revenue_per_seller,
               (w.tiktok_saturation is not null) as saturation_checked,
               (w.details->>'matching_products')::int as matching_products,
               (w.sellers is not null or w.hashtag_views is not null
                or coalesce((w.details->'google'->>'points')::int, 0) > 0) as has_data,
               (select count(*) from gapfinder.item_concept_map m where m.concept_id = c.id) as items
        from gapfinder.concepts c
        left join gapfinder.concept_weekly w on w.concept_id = c.id and w.week_start = %s
        where c.merged_into_id is null
        order by w.rank nulls last, c.name
        """,
        (week,),
    )


@st.cache_data(ttl=CACHE_SECONDS)
def score_history() -> pd.DataFrame:
    return _df("select concept_id, week_start, opportunity_score from gapfinder.concept_weekly order by week_start")


@st.cache_data(ttl=CACHE_SECONDS)
def google_series() -> dict:
    """keyword -> list of (week_date, interest) from the latest Google Trends lookup."""
    df = _df(
        """
        select distinct on (request_key) request_key, payload->'interestOverTime' as points
        from gapfinder.raw_responses
        where source = 'google_trends'
        order by request_key, snapshot_date desc
        """
    )
    out = {}
    for _, r in df.iterrows():
        points = [p for p in (r["points"] or []) if not p.get("isPartial")]
        out[r["request_key"].removeprefix("trends:")] = [
            (pd.to_datetime(int(p["time"]), unit="s").date(), float(p.get("value") or 0)) for p in points
        ]
    return out


@st.cache_data(ttl=CACHE_SECONDS)
def concept(concept_id: int) -> dict | None:
    df = _df("select * from gapfinder.concepts where id = %s", (concept_id,))
    return df.iloc[0].to_dict() if len(df) else None


@st.cache_data(ttl=CACHE_SECONDS)
def concept_weeks(concept_id: int) -> pd.DataFrame:
    return _df("select * from gapfinder.concept_weekly where concept_id = %s order by week_start", (concept_id,))


@st.cache_data(ttl=CACHE_SECONDS)
def concept_items(concept_id: int) -> pd.DataFrame:
    return _df(
        """
        select i.id, i.source, i.title, i.normalized_description, i.category, i.url,
               i.first_seen, i.last_seen, m.assigned_by, s.price, s.rank
        from gapfinder.item_concept_map m
        join gapfinder.items i on i.id = m.item_id
        left join lateral (
            select price, rank from gapfinder.item_snapshots
            where item_id = i.id order by snapshot_date desc limit 1
        ) s on true
        where m.concept_id = %s
        order by i.source, i.last_seen desc
        """,
        (concept_id,),
    )


@st.cache_data(ttl=CACHE_SECONDS)
def weekly_source_series(keyword: str, hashtag: str) -> pd.DataFrame:
    """Per-week TikTok Shop revenue/sellers and TikTok hashtag views for one keyword
    (the latest lookup in each week)."""
    df = _df(
        """
        select snapshot_date, request_key,
               (payload->>'total_views')::numeric as hashtag_views,
               p.shop_revenue, p.sellers
        from gapfinder.raw_responses r
        left join lateral (
            select sum(coalesce((d->>'revenue')::numeric, 0)) as shop_revenue,
                   count(distinct d->>'seller_id') as sellers
            from jsonb_array_elements(case when r.request_key like 'products:%%'
                                           then r.payload->'data' else '[]'::jsonb end) d
        ) p on true
        where (source = 'kalodata_keywords' and request_key = %s)
           or (source = 'tiktok' and request_key = %s)
        order by snapshot_date
        """,
        (f"products:{keyword}", f"hashtag:{hashtag}"),
    )
    if not len(df):
        return pd.DataFrame(columns=["week_start", "hashtag_views", "shop_revenue", "sellers"])
    df["week_start"] = pd.to_datetime(df["snapshot_date"]).dt.to_period("W-SUN").dt.start_time.dt.date
    shop = (df[df["request_key"].str.startswith("products:")]
            .groupby("week_start")[["shop_revenue", "sellers"]].last())
    tag = df[df["request_key"].str.startswith("hashtag:")].groupby("week_start")[["hashtag_views"]].last()
    out = shop.join(tag, how="outer").reset_index()
    out.loc[out["shop_revenue"].notna() & (out["sellers"] == 0), "shop_revenue"] = None
    return out


@st.cache_data(ttl=CACHE_SECONDS)
def shop_sellers(keywords: tuple, active_min_revenue: float,
                 concept_id: int | None = None) -> tuple[pd.DataFrame, list, pd.DataFrame]:
    """The TikTok Shop products behind a concept's seller count: the latest
    search for each of its keywords, merged, each product counted once.
    Products the relevance filter judged to be something else are left out
    and returned separately so they can be reviewed.
    Returns (one row per seller, [(keyword, date checked, products found)], excluded products)."""
    df = _df(
        """
        select distinct on (request_key) request_key, snapshot_date, payload
        from gapfinder.raw_responses
        where source = 'kalodata_keywords' and request_key = any(%s)
        order by request_key, snapshot_date desc
        """,
        ([f"products:{k}" for k in keywords],),
    )
    not_relevant = set()
    if concept_id is not None:
        nr = _df("select product_id from gapfinder.concept_product_matches where concept_id = %s and not relevant",
                 (concept_id,))
        not_relevant = set(nr["product_id"]) if len(nr) else set()
    searches, products, excluded, seen = [], [], [], set()
    for _, r in df.iterrows():
        data = r["payload"].get("data") or []
        searches.append((r["request_key"].removeprefix("products:"), r["snapshot_date"], len(data)))
        for p in data:
            pid = str(p.get("product_id"))
            if pid in seen:
                continue
            seen.add(pid)
            if pid in not_relevant:
                excluded.append({"product": p.get("product_name"), "seller": p.get("seller_name"),
                                 "price": p.get("unit_price"), "revenue_7d": p.get("revenue")})
            else:
                products.append(p)
    excluded_df = pd.DataFrame(excluded)
    if not products:
        return pd.DataFrame(), searches, excluded_df
    rows = {}
    for p in products:
        key = p.get("seller_id") or p.get("product_id")
        s = rows.setdefault(key, {"seller": p.get("seller_name") or "(unknown)", "revenue_7d": 0.0,
                                  "units_7d": 0, "products": 0, "top_product": "", "_top": -1,
                                  "newest_launch": None})
        rev = float(p.get("revenue") or 0)
        s["revenue_7d"] += rev
        s["units_7d"] += int(p.get("sales_volumn") or 0)
        s["products"] += 1
        if rev > s["_top"]:
            s["_top"], s["top_product"] = rev, p.get("product_name") or ""
        launch = str(p.get("launch_date") or "")[:10] or None
        if launch and (s["newest_launch"] is None or launch > s["newest_launch"]):
            s["newest_launch"] = launch
    out = pd.DataFrame(rows.values()).drop(columns="_top").sort_values("revenue_7d", ascending=False)
    out.insert(0, "active", out["revenue_7d"] >= active_min_revenue)
    return out, searches, excluded_df


@st.cache_data(ttl=CACHE_SECONDS)
def amazon_ranks(concept_id: int) -> pd.DataFrame:
    """Best Amazon Best Sellers rank per week among the concept's products."""
    return _df(
        """
        select date_trunc('week', s.snapshot_date)::date as week_start, min(s.rank) as best_rank
        from gapfinder.item_concept_map m
        join gapfinder.items i on i.id = m.item_id and i.source = 'amazon'
        join gapfinder.item_snapshots s on s.item_id = i.id
        where m.concept_id = %s
        group by 1 order by 1
        """,
        (concept_id,),
    )


@st.cache_data(ttl=CACHE_SECONDS)
def reddit_mentions(concept_id: int) -> pd.DataFrame:
    return _df(
        """
        select date_trunc('week', i.first_seen)::date as week_start, count(*) as mentions
        from gapfinder.item_concept_map m
        join gapfinder.items i on i.id = m.item_id and i.source = 'reddit'
        where m.concept_id = %s
        group by 1 order by 1
        """,
        (concept_id,),
    )


@st.cache_data(ttl=CACHE_SECONDS)
def concept_options() -> pd.DataFrame:
    return _df("select id, name, category from gapfinder.concepts where merged_into_id is null order by name")


@st.cache_data(ttl=60)
def recent_runs(days: int = 14) -> pd.DataFrame:
    return _df(
        """
        select started_at, source, snapshot_date, status, records_saved, estimated_cost_usd,
               extract(epoch from (finished_at - started_at))::int as seconds, error_message
        from gapfinder.source_runs
        where started_at > now() - make_interval(days => %s)
        order by started_at desc
        """,
        (days,),
    )


@st.cache_data(ttl=60)
def spend_by_source(since: date) -> pd.DataFrame:
    return _df(
        """
        select source, sum(estimated_cost_usd) as usd, count(*) as runs,
               count(*) filter (where status = 'failed') as failed
        from gapfinder.source_runs where snapshot_date >= %s
        group by source order by usd desc
        """,
        (since,),
    )


@st.cache_data(ttl=60)
def database_stats() -> dict:
    df = _df(
        """
        select pg_database_size(current_database()) as db_bytes,
               (select count(*) from gapfinder.items) as items,
               (select count(*) from gapfinder.items where classified_at is null) as unclassified,
               (select count(*) from gapfinder.concepts where merged_into_id is null) as concepts,
               (select count(*) from gapfinder.raw_responses) as raw_responses
        """
    )
    return df.iloc[0].to_dict()


# --- Writes (manual controls) -------------------------------------------------

def _write(fn):
    """Run fn(conn) in one transaction, then clear cached reads."""
    with connect() as conn:
        result = fn(conn)
    st.cache_data.clear()
    return result


def set_status(concept_ids: list[int], status: str) -> None:
    _write(lambda conn: conn.execute(
        "update gapfinder.concepts set review_status = %s, updated_at = now() where id = any(%s)",
        (status, concept_ids),
    ))


def set_fit(concept_ids: list[int], fit: bool) -> None:
    """Override Claude's TikTok-fit verdict by hand (never overwritten afterwards)."""
    reason = "Marked a TikTok fit by hand" if fit else "Marked not for TikTok by hand"
    _write(lambda conn: conn.execute(
        """
        update gapfinder.concepts
        set tiktok_fit = %s, tiktok_fit_by = 'manual', tiktok_fit_reason = %s, updated_at = now()
        where id = any(%s)
        """,
        (fit, reason, concept_ids),
    ))


def update_concept(concept_id: int, name: str, category: str, keywords: list[str]) -> None:
    _write(lambda conn: conn.execute(
        """
        update gapfinder.concepts set name = %s, category = %s, keywords = %s, updated_at = now()
        where id = %s
        """,
        (" ".join(name.lower().split()), category or None, keywords, concept_id),
    ))


def merge(source_id: int, target_id: int) -> None:
    """Fold source into target: its items move over, its keywords are added,
    and it's marked merged (kept for history, hidden everywhere)."""
    if source_id == target_id:
        raise ValueError("Pick a different concept to merge into.")

    def do(conn):
        src = conn.execute("select keywords from gapfinder.concepts where id = %s", (source_id,)).fetchone()
        tgt = conn.execute("select keywords from gapfinder.concepts where id = %s", (target_id,)).fetchone()
        keywords = list(dict.fromkeys((tgt["keywords"] or []) + (src["keywords"] or [])))
        conn.execute(
            """
            update gapfinder.item_concept_map
            set concept_id = %s, assigned_by = 'manual', model = null, assigned_at = now()
            where concept_id = %s
            """,
            (target_id, source_id),
        )
        conn.execute("update gapfinder.concepts set keywords = %s, updated_at = now() where id = %s",
                     (keywords, target_id))
        # Anything previously merged into the source now points at the target too.
        conn.execute("update gapfinder.concepts set merged_into_id = %s where merged_into_id = %s",
                     (target_id, source_id))
        conn.execute("update gapfinder.concepts set merged_into_id = %s, updated_at = now() where id = %s",
                     (target_id, source_id))
    _write(do)


def move_items(item_ids: list[int], target_id: int | None = None, new_name: str = "",
               category: str | None = None) -> int:
    """Split: move items to an existing concept, or to a new one by name.
    Returns the concept id they moved to."""
    def do(conn):
        concept_id = target_id
        if concept_id is None:
            name = " ".join(new_name.lower().split())
            if not name:
                raise ValueError("Give the new concept a name.")
            row = conn.execute(
                """
                insert into gapfinder.concepts (name, category, keywords) values (%s, %s, %s)
                on conflict (name) do update set updated_at = now()
                returning id, merged_into_id
                """,
                (name, category, [name]),
            ).fetchone()
            if row["merged_into_id"]:
                raise ValueError(f"'{name}' was merged into another concept; pick that one instead.")
            concept_id = row["id"]
        conn.execute(
            """
            update gapfinder.item_concept_map
            set concept_id = %s, assigned_by = 'manual', model = null, assigned_at = now()
            where item_id = any(%s)
            """,
            (concept_id, item_ids),
        )
        return concept_id
    return _write(do)


def mark_not_product(item_ids: list[int]) -> None:
    def do(conn):
        conn.execute("delete from gapfinder.item_concept_map where item_id = any(%s)", (item_ids,))
        conn.execute("update gapfinder.items set is_product = false where id = any(%s)", (item_ids,))
    _write(do)


def keyword_for(concept_row: dict) -> str:
    return concept_keyword(concept_row)
