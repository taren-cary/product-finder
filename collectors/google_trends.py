"""Google Trends collector (via Apify).

For each watchlist keyword: 12 months of weekly search interest in the US
(0-100 scale), plus related searches (top and rising). Rising related
searches are also a discovery signal: new product ideas people search for
alongside ones we already track.

Actor: agenscrape/google-trends-scraper (99%+ success rate when tested).
Cost: about $0.025 per keyword plus $0.025 per run, so keywords are sent in
batches. Settings are in config.yaml under "google_trends".
"""

from collectors.apify import run_actor
from collectors.base import BaseCollector
from collectors.keywords import watch_keywords
from core.config import settings

ACTOR_ID = "agenscrape~google-trends-scraper"
START_FEE_USD = 0.025
PER_KEYWORD_USD = 0.025


class GoogleTrendsCollector(BaseCollector):
    name = "google_trends"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cfg = settings["google_trends"]
        self.cost_cap_usd = settings["cost_caps"]["apify_per_day_usd"]

    def collect(self) -> int:
        # Confirmation only: the top TikTok candidates (see collectors/keywords.py).
        keywords = watch_keywords(self.conn, self.cfg["max_keywords"], self.name,
                                  lambda k: f"trends:{k}", purpose="confirm")
        if not keywords:
            self.log.info("No keywords to look up yet (watchlist and concepts are empty)")
            return 0
        self.log.info("Looking up %d keywords", len(keywords))

        batch_size = self.cfg["batch_size"]
        results = self.fetch_batched(
            [f"trends:{k}" for k in keywords],
            batch_size,
            self._fetch_batch,
            max_cost_per_batch=START_FEE_USD + PER_KEYWORD_USD * batch_size + 0.01,
        )
        return len(results)

    def _fetch_batch(self, keys: list[str]):
        keywords = [k.removeprefix("trends:") for k in keys]
        run = run_actor(
            ACTOR_ID,
            {
                "keywords": keywords,
                "geo": self.cfg["geo"],
                "timeRange": self.cfg["time_range"],
                "includeInterestOverTime": True,
                "includeRelatedSearches": True,
                "includeRelatedTopics": False,
                "includeGeoData": False,
                "useBrowserForTopics": False,
            },
            max_charge_usd=START_FEE_USD + PER_KEYWORD_USD * len(keywords) + 0.01,
            memory_mb=1024,  # this actor's start fee is charged per GB
        )
        results = {}
        for item in run["items"]:
            kw = " ".join(str(item.get("keyword", "")).lower().split())
            if item.get("interestOverTime"):
                results[f"trends:{kw}"] = item
        return results, run["cost_usd"]
