"""The keyword watchlist that enrichment collectors look up.

Google Trends, TikTok and the Kalodata keyword search don't discover products
by themselves. They look up keywords for products we already care about:

  1. Anything in config.yaml under "watchlist" (your own manual list).
  2. Concepts you shortlisted in the dashboard.
  3. Then alternating between the highest-scoring concepts (keep tracking
     the best opportunities) and concepts never looked up yet, newest first
     (so every new concept gets a score).
  Rejected and merged concepts are skipped.

Each collector caps how many keywords it looks up per run (to control cost).
"""

from itertools import zip_longest

from core.config import settings


def watch_keywords(conn, limit: int) -> list[str]:
    """Up to `limit` unique, lowercase keywords, most important first."""
    keywords = [k for k in (settings.get("watchlist") or []) if k]

    rows = conn.execute(
        """
        select c.name, c.keywords, c.review_status, w.opportunity_score,
               (w.sellers is not null or w.hashtag_views is not null
                or coalesce((w.details->'google'->>'points')::int, 0) > 0) as looked_up
        from gapfinder.concepts c
        left join lateral (
            select * from gapfinder.concept_weekly
            where concept_id = c.id order by week_start desc limit 1
        ) w on true
        where c.merged_into_id is null and c.review_status <> 'rejected'
        order by c.created_at desc
        """
    ).fetchall()
    shortlisted = [r for r in rows if r["review_status"] == "shortlisted"]
    others = [r for r in rows if r["review_status"] != "shortlisted"]
    best = sorted((r for r in others if r["looked_up"]),
                  key=lambda r: r["opportunity_score"] or 0, reverse=True)
    fresh = [r for r in others if not r["looked_up"]]   # already newest first

    ordered = list(shortlisted)
    for pair in zip_longest(best, fresh):
        ordered.extend(r for r in pair if r is not None)
    keywords.extend(concept_keyword(r) for r in ordered)

    unique = []
    for k in keywords:
        k = " ".join(k.lower().split())
        if k and k not in unique:
            unique.append(k)
    return unique[:limit]


def concept_keyword(concept) -> str:
    """A concept's main search term: its first keyword, or else its name."""
    raw = concept["keywords"][0] if concept["keywords"] else concept["name"]
    return " ".join(raw.lower().split())


def to_hashtag(keyword: str) -> str:
    """'Heatless Curlers' -> 'heatlesscurlers' (TikTok hashtags have no spaces)."""
    return "".join(ch for ch in keyword.lower() if ch.isalnum())
