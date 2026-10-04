"""Step 1: find YouTube channels that teach TikTok Shop selling / affiliate marketing.

Runs the searches in config.yaml ("knowledge" -> "searches") on YouTube,
limited to videos from the past year, and records the matching videos and
their channels. New channels start as "candidate": nothing is transcribed or
sent to Claude until the owner approves the channel in the dashboard.

Free: uses yt-dlp to read YouTube search results (no API key, no Claude).

    .venv\\Scripts\\python -m knowledge.discover
"""

import json
import logging

import yt_dlp

from core.config import settings
from core.db import connect

log = logging.getLogger(__name__)

# YouTube's "Upload date: This year" search filter (the past 12 months).
PAST_YEAR_FILTER = "&sp=EgIIBQ%253D%253D"


def search(query: str, max_results: int) -> list[dict]:
    """Videos for one YouTube search, newest year only."""
    url = ("https://www.youtube.com/results?search_query="
           + query.replace(" ", "+") + PAST_YEAR_FILTER)
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
            "playlistend": max_results, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return [e for e in (info.get("entries") or []) if e.get("id") and e.get("channel_id")]


def run() -> dict:
    cfg = settings["knowledge"]
    videos = {}
    for q in cfg["searches"]:
        try:
            found = search(q, cfg["results_per_search"])
        except Exception as e:
            log.warning("Search %r failed, skipping: %s", q, e)
            continue
        kept = 0
        for v in found:
            duration, views = v.get("duration") or 0, v.get("view_count") or 0
            if duration < cfg["min_duration_s"] or duration > cfg["max_duration_s"]:
                continue        # skip Shorts and multi-hour livestream dumps
            if views < cfg["min_views"]:
                continue
            videos.setdefault(v["id"], {**v, "found_by": q})
            kept += 1
        log.info("%-45s %3d results, %3d kept", q, len(found), kept)

    # Group by channel.
    channels = {}
    for v in videos.values():
        ch = channels.setdefault(v["channel_id"], {
            "name": v.get("channel") or v.get("uploader") or v["channel_id"],
            "url": v.get("channel_url") or f"https://www.youtube.com/channel/{v['channel_id']}",
            "videos": [],
        })
        ch["videos"].append(v)

    with connect() as conn:
        for cid, ch in channels.items():
            top = sorted(ch["videos"], key=lambda v: v.get("view_count") or 0, reverse=True)
            conn.execute(
                """
                insert into gapfinder.kb_channels (channel_id, name, url, videos_found, total_views, sample_titles)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (channel_id) do update set
                    name = excluded.name, url = excluded.url,
                    videos_found = greatest(gapfinder.kb_channels.videos_found, excluded.videos_found),
                    total_views = greatest(gapfinder.kb_channels.total_views, excluded.total_views),
                    sample_titles = excluded.sample_titles, updated_at = now()
                """,
                (cid, ch["name"], ch["url"], len(ch["videos"]),
                 sum(v.get("view_count") or 0 for v in ch["videos"]),
                 json.dumps([v.get("title") for v in top[:5]])),
            )
        with conn.cursor() as cur:
            cur.executemany(
                """
                insert into gapfinder.kb_videos (video_id, channel_id, title, duration_s, view_count, found_by)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (video_id) do update set view_count = excluded.view_count
                """,
                [(v["id"], v["channel_id"], v.get("title") or "", int(v.get("duration") or 0),
                  v.get("view_count"), v["found_by"]) for v in videos.values()],
            )
    log.info("Found %d videos from %d channels", len(videos), len(channels))
    return {"videos": len(videos), "channels": len(channels)}


if __name__ == "__main__":
    from core.logging_setup import setup_logging
    setup_logging()
    print(run())
