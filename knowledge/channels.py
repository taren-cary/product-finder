"""Channel helpers: add a channel by its link, and list an approved channel's
TikTok Shop videos from the past year (free, via yt-dlp)."""

import re
from datetime import date, timedelta

import yt_dlp

from core.config import settings

TOPIC = re.compile(r"tik\s*tok\s*shop|tts\b|tiktok affiliate", re.I)


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


def channel_videos(channel_id: str, limit: int = 150) -> list[dict]:
    """The channel's recent uploads whose titles are about TikTok Shop, within
    the length limits in config.yaml. Upload dates aren't known yet (filled in
    when the transcript is fetched); the newest `limit` uploads are checked."""
    cfg = settings["knowledge"]
    url = f"https://www.youtube.com/channel/{channel_id}/videos"
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True,
                           "playlistend": limit, "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    out = []
    for v in info.get("entries") or []:
        title, duration = v.get("title") or "", v.get("duration") or 0
        if not v.get("id") or not TOPIC.search(title):
            continue
        if duration and not (cfg["min_duration_s"] <= duration <= cfg["max_duration_s"]):
            continue
        out.append({"id": v["id"], "title": title, "duration": duration, "view_count": v.get("view_count")})
    return out


def oldest_upload_allowed() -> date:
    return date.today() - timedelta(days=settings["knowledge"]["max_age_days"])
