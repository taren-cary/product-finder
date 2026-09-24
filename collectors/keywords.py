"""Which concepts to look up on the paid enrichment sources, most useful first.

Only concepts that fit TikTok (concepts.tiktok_fit = true) are ever looked
up. Each collector has a weekly limit (config.yaml), and this module decides
how to spend it, using when each concept was last looked up on that source.

TikTok checks (Kalodata keyword search, TikTok hashtags) - the core data:
  1. your watchlist (config.yaml)            5. rising on Google/Amazon/Reddit
  2. concepts you shortlisted                   (selling elsewhere, maybe not on TikTok yet)
  3. showed up in this week's TikTok         6. highest scores
     Shop discovery lists                    7. everything else, in rotation:
  4. never looked up yet (newest first)         the longest-unchecked first

Google Trends (confirmation only):
  1. watchlist   2. shortlisted   3. the top-scoring TikTok candidates that
  haven't had a Google check in the last few weeks.

Concepts already looked up this week are skipped (except 1 and 2): their
data is already saved, so the limit goes to new information.
"""

from datetime import date, timedelta

from core.config import settings


def concept_keyword(concept) -> str:
    """A concept's main search term: its first keyword, or else its name."""
    raw = concept["keywords"][0] if concept["keywords"] else concept["name"]
    return " ".join(raw.lower().split())


def to_hashtag(keyword: str) -> str:
    """'Heatless Curlers' -> 'heatlesscurlers' (TikTok hashtags have no spaces)."""
    return "".join(ch for ch in keyword.lower() if ch.isalnum())


def _normalize(k: str) -> str:
    return " ".join(k.lower().split())


def _last_lookups(conn, source: str, prefix: str) -> dict:
    """request key -> date it was last looked up on that source."""
    rows = conn.execute(
        """
        select request_key, max(snapshot_date) as last
        from gapfinder.raw_responses
        where source = %s and request_key like %s
        group by request_key
        """,
        (source, prefix + "%"),
    ).fetchall()
    return {r["request_key"]: r["last"] for r in rows}


def _fit_concepts(conn, week_start: date) -> list[dict]:
    return conn.execute(
        """
        select c.id, c.name, c.keywords, c.review_status, c.created_at,
               w.opportunity_score, w.outside_velocity, w.confirmations,
               exists (
                   select 1 from gapfinder.item_concept_map m
                   join gapfinder.items i on i.id = m.item_id
                   join gapfinder.item_snapshots s on s.item_id = i.id
                   where m.concept_id = c.id and i.source in ('kalodata', 'kalodata_video')
                     and s.snapshot_date >= %s
               ) as on_tiktok_lists
        from gapfinder.concepts c
        left join lateral (
            select * from gapfinder.concept_weekly
            where concept_id = c.id order by week_start desc limit 1
        ) w on true
        where c.merged_into_id is null and c.review_status <> 'rejected' and c.tiktok_fit is true
        order by c.created_at desc
        """,
        (week_start,),
    ).fetchall()


def watch_keywords(conn, limit: int, source: str, key_for, purpose: str = "tiktok",
                   today: date | None = None) -> list[str]:
    """Up to `limit` keywords to look up, most useful first.

    source:   the collector's source name (to see what it already looked up)
    key_for:  keyword -> that collector's request key, e.g. "trends:<kw>"
    purpose:  "tiktok" (TikTok checks) or "confirm" (Google Trends)
    """
    cfg = settings["enrichment"]
    today = today or date.today()
    week_start = today - timedelta(days=today.weekday())
    prefix = key_for("")
    last = _last_lookups(conn, source, prefix)

    def looked_up_since(kw, since):
        d = last.get(key_for(kw))
        return d is not None and d >= since

    concepts = _fit_concepts(conn, week_start)
    for c in concepts:
        c["kw"] = concept_keyword(c)

    ordered = [_normalize(k) for k in (settings.get("watchlist") or []) if k]
    ordered += [c["kw"] for c in concepts if c["review_status"] == "shortlisted"]
    fresh_only = [c for c in concepts if c["review_status"] != "shortlisted"
                  and not looked_up_since(c["kw"], week_start)]
    by_score = sorted(fresh_only, key=lambda c: float(c["opportunity_score"] or 0), reverse=True)

    if purpose == "confirm":
        recheck = today - timedelta(weeks=cfg["google_recheck_weeks"])
        ordered += [c["kw"] for c in by_score
                    if c["opportunity_score"] is not None and not looked_up_since(c["kw"], recheck)]
    else:
        rising_threshold = settings["features"]["rising_threshold"]
        ordered += [c["kw"] for c in by_score if c["on_tiktok_lists"]]
        ordered += [c["kw"] for c in fresh_only if c["on_tiktok_lists"]]   # unscored ones next
        ordered += [c["kw"] for c in fresh_only if last.get(key_for(c["kw"])) is None]
        ordered += [c["kw"] for c in fresh_only
                    if c["outside_velocity"] is not None and float(c["outside_velocity"]) >= rising_threshold]
        ordered += [c["kw"] for c in by_score if c["opportunity_score"] is not None]
        stale_first = sorted(fresh_only, key=lambda c: last.get(key_for(c["kw"])) or date.min)
        ordered += [c["kw"] for c in stale_first]

    unique = []
    for k in ordered:
        if k and k not in unique:
            unique.append(k)
    return unique[:limit]
