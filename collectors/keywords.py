"""The keyword watchlist that enrichment collectors look up.

Google Trends, TikTok and the Kalodata keyword search don't discover products
by themselves. They look up keywords for products we already care about:

  1. Anything in config.yaml under "watchlist" (your own manual list).
  2. Product concepts found by the concept step (Milestone 2): shortlisted
     concepts first, then the newest. Rejected and merged concepts are skipped.

Each collector caps how many keywords it looks up per run (to control cost).
"""

from core.config import settings


def watch_keywords(conn, limit: int) -> list[str]:
    """Up to `limit` unique, lowercase keywords, most important first."""
    keywords = [k for k in (settings.get("watchlist") or []) if k]

    rows = conn.execute(
        """
        select name, keywords from gapfinder.concepts
        where merged_into_id is null and review_status <> 'rejected'
        order by (review_status = 'shortlisted') desc, created_at desc
        """
    ).fetchall()
    for r in rows:
        # A concept's first keyword is its main search term; fall back to its name.
        keywords.append(r["keywords"][0] if r["keywords"] else r["name"])

    unique = []
    for k in keywords:
        k = " ".join(k.lower().split())
        if k and k not in unique:
            unique.append(k)
    return unique[:limit]


def to_hashtag(keyword: str) -> str:
    """'Heatless Curlers' -> 'heatlesscurlers' (TikTok hashtags have no spaces)."""
    return "".join(ch for ch in keyword.lower() if ch.isalnum())
