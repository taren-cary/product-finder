"""Channel helpers: add a channel by its link, and list every video an approved
channel uploaded in the past year (free, via yt-dlp)."""

from datetime import date, timedelta

import yt_dlp

from core.config import settings

# Videos only paying channel members can watch have no public transcript.
LOCKED = {"subscriber_only", "premium_only", "needs_auth"}


def resolve_channel(url: str) -> dict:
    """A channel link (or any of its video links) -> {channel_id, name, url}."""
    url = url.strip()
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True,
                           "playlistend": 1, "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    cid = info.get("channel_id") or info.get("uploader_id") or info.get("id")
    if not cid:
        raise ValueError("Couldn't find a YouTube channel at that link.")
    return {"channel_id": cid, "name": info.get("channel") or info.get("uploader") or info.get("title") or cid,
            "url": info.get("channel_url") or f"https://www.youtube.com/channel/{cid}"}


def upload_date(video_id: str) -> date | None:
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    d = info.get("upload_date")
    return date(int(d[:4]), int(d[4:6]), int(d[6:8])) if d else None


def channel_videos(channel_id: str, limit: int = 1000) -> list[dict]:
    """Every regular video (not Shorts or livestreams) the channel uploaded
    since the age limit in config.yaml.

    YouTube's channel list has no dates but is newest-first, so we look up the
    date of a handful of videos (a binary search) to find where the cutoff falls.
    """
    url = f"https://www.youtube.com/channel/{channel_id}/videos"
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True,
                           "playlistend": limit, "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    videos = [v for v in info.get("entries") or [] if v.get("id")]
    oldest = oldest_upload_allowed()

    def recent(i: int) -> bool:
        try:
            d = upload_date(videos[i]["id"])
        except Exception:
            return True          # can't tell (e.g. members-only); assume it's in range
        return d is None or d >= oldest

    lo, hi = 0, len(videos)      # videos[:lo] are recent, videos[hi:] are too old
    while lo < hi:
        mid = (lo + hi) // 2
        if recent(mid):
            lo = mid + 1
        else:
            hi = mid
    return [{"id": v["id"], "title": v.get("title") or "", "duration": v.get("duration") or 0,
             "view_count": v.get("view_count")}
            for v in videos[:lo]
            if v.get("availability") not in LOCKED and "members only" not in (v.get("title") or "").lower()]


def oldest_upload_allowed() -> date:
    return date.today() - timedelta(days=settings["knowledge"]["max_age_days"])
