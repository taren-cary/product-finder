"""Normalize step: turn raw API responses into items and daily snapshots.

Reads every raw response not yet processed (from discovery sources) and
writes, for each product in it:
  * gapfinder.items           one row per product per source (title, category, link)
  * gapfinder.item_snapshots  that product's numbers on that day (price, rank, metrics)

Each raw response is marked processed_at once done, so it's handled exactly
once. Rerunning is safe.

Enrichment responses (Google Trends interest, TikTok hashtags, Kalodata
keyword searches) aren't items; the features step reads those directly.
Google Trends *rising related searches* are the exception: each becomes an
item, because they're a way to discover new products.
"""

import logging
import re

from psycopg.types.json import Jsonb

from core.config import settings

log = logging.getLogger(__name__)

CHUNK = 50  # raw responses processed per database round trip


# --- One function per source: raw response -> list of items ----------------
# Each item is a dict with: source_item_id, title, and optionally category,
# url, body_text, price, rank, metrics.

def _kalodata(row) -> list[dict]:
    list_name, _, page = row["request_key"].partition(":p")
    if list_name.startswith("categories"):
        return []   # category rankings aren't products; used by the features step
    offset = (int(page or 1) - 1) * 100
    rows = row["payload"].get("data") or []
    if rows and "video_id" in rows[0]:
        return _kalodata_videos(rows, list_name, offset)
    items = []
    for i, p in enumerate(rows):
        if not p.get("product_id") or not p.get("product_name"):
            continue
        rank = offset + i + 1
        metrics = {k: v for k, v in p.items()
                   if k not in ("product_id", "product_name", "master_image_url")}
        metrics[f"rank:{list_name}"] = rank
        items.append({
            "source_item_id": p["product_id"],
            "title": p["product_name"],
            "price": p.get("unit_price"),
            "rank": rank,
            "metrics": metrics,
        })
    return items


def _kalodata_videos(rows, list_name, offset) -> list[dict]:
    """Viral shoppable videos. The title usually names the product being sold;
    the concept step reads it to find the product (like a Reddit post)."""
    items = []
    for i, v in enumerate(rows):
        if not v.get("video_id") or not v.get("video_title"):
            continue
        rank = offset + i + 1
        items.append({
            "source": "kalodata_video",
            "source_item_id": v["video_id"],
            "title": v["video_title"],
            "category": "shoppable TikTok video",
            "url": f"https://www.tiktok.com/@{v.get('belonged_creator_handle') or 'user'}/video/{v['video_id']}",
            "rank": rank,
            "metrics": {k: v.get(k) for k in ("views", "revenue", "revenue_growth_rate", "ad_view_ratio",
                                              "ad_revenue_ratio", "digg_count", "share_count",
                                              "comment_count", "publish_date", "belonged_creator_handle")}
                       | {f"rank:{list_name}": rank},
        })
    return items


def _amazon(row) -> list[dict]:
    slug = row["request_key"].removeprefix("bestsellers:")
    items = []
    for p in row["payload"].get("items") or []:
        if not p.get("asin") or not p.get("title"):
            continue
        category = (p.get("category") or "").removeprefix("Best Sellers in ").strip() or slug
        items.append({
            "source_item_id": p["asin"],
            "title": p["title"],
            "category": category,
            "url": f"https://www.amazon.com/dp/{p['asin']}",
            "price": _parse_price(p.get("price")),
            "rank": p.get("position"),
            "metrics": {"stars": p.get("stars"), "reviews": p.get("reviewsCount"),
                        "category_slug": slug, f"rank:{slug}": p.get("position")},
        })
    return items


def _reddit(row) -> list[dict]:
    sub = row["payload"].get("subreddit")
    items = []
    for p in row["payload"].get("posts") or []:
        items.append({
            "source_item_id": p["id"],
            "title": p["title"],
            "category": f"r/{sub}",
            "url": f"https://www.reddit.com{p.get('permalink') or ''}",
            "body_text": p.get("selftext") or None,
            "metrics": {"score": p.get("score"), "comments": p.get("num_comments"),
                        "matched_phrases": p.get("matched_phrases"),
                        "created_utc": p.get("created_utc")},
        })
    return items


def _google_trends(row) -> list[dict]:
    """Rising searches related to a keyword we track -> new product ideas.
    Off by default (config.yaml: google_trends.rising_searches_as_items); the
    raw searches stay saved either way."""
    if not settings["google_trends"].get("rising_searches_as_items", False):
        return []
    parent = row["request_key"].removeprefix("trends:")
    rising = ((row["payload"].get("relatedSearches") or {}).get("rising")) or []
    items = []
    for r in rising:
        query = " ".join(str(r.get("query") or "").lower().split())
        if not query or query == parent:
            continue
        items.append({
            "source_item_id": query,
            "title": query,
            "category": f"rising search related to '{parent}'",
            "metrics": {"parent_keyword": parent, "rise": r.get("formattedValue"),
                        "rise_value": r.get("value")},
        })
    return items


HANDLERS = {
    "kalodata": _kalodata,
    "amazon": _amazon,
    "reddit": _reddit,
    "google_trends": _google_trends,
}


def _parse_price(text) -> float | None:
    """'$29.99' -> 29.99; '$9.99 - $19.99' -> 9.99; anything else -> None."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = re.search(r"\d[\d,]*\.?\d*", str(text))
    return float(m.group().replace(",", "")) if m else None


# --- Saving ---------------------------------------------------------------

SAVE_SQL = """
    with item as (
        insert into gapfinder.items
            (source, source_item_id, title, category, url, body_text, first_seen, last_seen)
        values (%(source)s, %(source_item_id)s, %(title)s, %(category)s, %(url)s,
                %(body_text)s, %(date)s, %(date)s)
        on conflict (source, source_item_id) do update set
            title      = excluded.title,
            category   = coalesce(excluded.category, gapfinder.items.category),
            url        = coalesce(excluded.url, gapfinder.items.url),
            body_text  = coalesce(excluded.body_text, gapfinder.items.body_text),
            first_seen = least(gapfinder.items.first_seen, excluded.first_seen),
            last_seen  = greatest(gapfinder.items.last_seen, excluded.last_seen)
        returning id
    )
    -- The same product can show up in several lists on one day (e.g. both
    -- "fastest growing" and "top revenue"); merge those into one snapshot.
    insert into gapfinder.item_snapshots
        (item_id, snapshot_date, raw_response_id, price, rank, metrics)
    select id, %(date)s, %(raw_id)s, %(price)s, %(rank)s, %(metrics)s from item
    on conflict (item_id, snapshot_date) do update set
        price   = coalesce(excluded.price, gapfinder.item_snapshots.price),
        rank    = least(gapfinder.item_snapshots.rank, excluded.rank),
        metrics = gapfinder.item_snapshots.metrics || excluded.metrics
"""


def _save_all(conn, row, items: list[dict]) -> None:
    """Save one raw response's items. executemany sends them in one batch."""
    params = [{
        "source": item.get("source") or row["source"],
        "source_item_id": str(item["source_item_id"]),
        "title": item["title"][:1000],
        "category": item.get("category"),
        "url": item.get("url"),
        "body_text": item.get("body_text"),
        "date": row["snapshot_date"],
        "raw_id": row["id"],
        "price": item.get("price"),
        "rank": item.get("rank"),
        "metrics": Jsonb(item.get("metrics") or {}),
    } for item in items]
    if params:
        with conn.cursor() as cur:
            cur.executemany(SAVE_SQL, params)


def run(conn, snapshot_date) -> dict:
    """Process all unprocessed raw responses. Returns {"records": items saved}."""
    saved = 0
    while True:
        rows = conn.execute(
            """
            select id, source, snapshot_date, request_key, payload
            from gapfinder.raw_responses
            where processed_at is null and source = any(%s)
            order by id
            limit %s
            """,
            (list(HANDLERS), CHUNK),
        ).fetchall()
        if not rows:
            break
        for row in rows:
            try:
                items = HANDLERS[row["source"]](row)
                _save_all(conn, row, items)
                saved += len(items)
            except Exception:
                # A malformed response shouldn't block the rest; mark it and move on.
                conn.rollback()
                log.exception("Could not normalize raw response %s (%s %s); skipping it",
                              row["id"], row["source"], row["request_key"])
            conn.execute("update gapfinder.raw_responses set processed_at = now() where id = %s",
                         (row["id"],))
            conn.commit()
    log.info("Normalized %d items", saved)
    return {"records": saved}
