"""TikTok organic attention collector (via Apify).

For each watchlist keyword we look up its hashtag (e.g. "heatless curlers"
-> #heatlesscurlers) and save:
  * the hashtag's total view count (a running total; week-over-week growth
    is the organic-attention trend), and
  * its top videos (TikTok's most popular for the tag, not the newest):
    post date, views, likes, shares, whether each is an ad, and whether it
    links a TikTok Shop product. New videos breaking into the top list, and
    the share of ads/shop links, show how commercial the tag has become.

Actor: clockworks/tiktok-hashtag-scraper. Cost: about $0.002-0.003 per video,
so videos_per_hashtag in config.yaml controls the price.

Only the fields we use are kept for each video (full video records include
media links and other bulk that would fill the free database quickly).
"""

from collectors.apify import run_actor
from collectors.base import BaseCollector
from collectors.keywords import to_hashtag, watch_keywords
from core.config import settings

ACTOR_ID = "clockworks~tiktok-hashtag-scraper"
PER_VIDEO_USD = 0.003   # free-plan price; cheaper on paid plans

VIDEO_FIELDS = [
    "id", "text", "createTimeISO", "playCount", "diggCount", "shareCount",
    "commentCount", "collectCount", "isAd", "isSponsored", "hasTikTokShopProduct",
    "webVideoUrl",
]


class TikTokCollector(BaseCollector):
    name = "tiktok"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cfg = settings["tiktok"]
        self.cost_cap_usd = settings["cost_caps"]["apify_per_day_usd"]

    def collect(self) -> int:
        keywords = watch_keywords(self.conn, self.cfg["max_keywords"], self.name,
                                  lambda k: f"hashtag:{to_hashtag(k)}", purpose="tiktok")
        # Several keywords can share a hashtag; look each hashtag up once.
        hashtags = list(dict.fromkeys(to_hashtag(k) for k in keywords if to_hashtag(k)))
        if not hashtags:
            self.log.info("No keywords to look up yet (watchlist and concepts are empty)")
            return 0
        self.log.info("Looking up %d hashtags", len(hashtags))

        batch_size = self.cfg["batch_size"]
        per_batch = PER_VIDEO_USD * self.cfg["videos_per_hashtag"] * batch_size + 0.01
        results = self.fetch_batched(
            [f"hashtag:{h}" for h in hashtags], batch_size, self._fetch_batch, per_batch,
        )
        return len(results)

    def _fetch_batch(self, keys: list[str]):
        hashtags = [k.removeprefix("hashtag:") for k in keys]
        per_video = self.cfg["videos_per_hashtag"]
        run = run_actor(
            ACTOR_ID,
            {"hashtags": hashtags, "resultsPerPage": per_video},
            max_charge_usd=PER_VIDEO_USD * per_video * len(hashtags) + 0.01,
        )
        # Group the videos by the hashtag they were found under.
        grouped = {}
        for item in run["items"]:
            tag_info = item.get("searchHashtag") or {}
            tag = str(tag_info.get("name") or item.get("input") or "").lower().lstrip("#")
            if not tag:
                continue
            entry = grouped.setdefault(f"hashtag:{tag}", {
                "hashtag": tag,
                "total_views": tag_info.get("views"),
                "videos": [],
            })
            entry["videos"].append({f: item.get(f) for f in VIDEO_FIELDS})
        return grouped, run["cost_usd"]
