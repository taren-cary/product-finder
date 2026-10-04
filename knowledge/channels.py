"""Channel helpers: add a channel by its link, and list every video an approved
channel uploaded in the past year (free, via yt-dlp)."""

import os
from datetime import date, timedelta

import yt_dlp
from youtube_transcript_api.proxies import WebshareProxyConfig

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


def webshare_proxy() -> WebshareProxyConfig | None:
    """Rotating residential proxy (Webshare), if its login is in .env.
    YouTube blocks home connections that fetch lots of transcripts; the proxy
    sends each request from a different address."""
    user, password = os.getenv("WEBSHARE_PROXY_USERNAME"), os.getenv("WEBSHARE_PROXY_PASSWORD")
    return WebshareProxyConfig(proxy_username=user, proxy_password=password) if user and password else None


def video_info(video_id: str) -> dict:
    """The video page's details (upload date, caption links). Uses the home
    connection first, since YouTube rarely blocks this part and it's the bulk
    of the data; falls back to the proxy if it does."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError:
        proxy = webshare_proxy()
        if not proxy:
            raise
        with yt_dlp.YoutubeDL({**opts, "proxy": proxy.url}) as ydl:
            return ydl.extract_info(url, download=False)


def upload_date(video_id: str, info: dict | None = None) -> date | None:
    d = (info or video_info(video_id)).get("upload_date")
    return date(int(d[:4]), int(d[4:6]), int(d[6:8])) if d else None


def english_caption_url(info: dict) -> str | None:
    """Link to the English captions (json3 format): the creator's own captions
    if there are any, otherwise YouTube's automatic ones."""
    for source, keys in ((info.get("subtitles") or {}, ("en", "en-US", "en-GB")),
                         (info.get("automatic_captions") or {}, ("en-orig", "en"))):
        for key in keys:
            for track in source.get(key) or []:
                if track.get("ext") == "json3":
                    return track["url"]
    return None


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
