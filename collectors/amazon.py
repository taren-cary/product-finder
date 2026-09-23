"""Amazon Best Sellers collector (via Apify).

Why Best Sellers and not Movers & Shakers: Amazon's Movers & Shakers pages
currently return no data to scrapers (tested 2026-09-23). Instead we save
each category's Best Sellers list every day and work out the movers
ourselves from the history (new entries, big rank jumps).

Actor: amazon-scraper/amazon-bestsellers-scraper. It returns the top 50
products per category page with title, price, rating and review count.
Categories and limits are set in config.yaml under "amazon".
"""

from collectors.apify import run_actor
from collectors.base import BaseCollector, CostCapReached
from core.config import settings

ACTOR_ID = "amazon-scraper~amazon-bestsellers-scraper"


class AmazonBestsellersCollector(BaseCollector):
    name = "amazon"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cfg = settings["amazon"]
        self.cost_cap_usd = settings["cost_caps"]["apify_per_day_usd"]

    def collect(self) -> int:
        max_per_category = self.cfg["max_charge_per_category_usd"]
        failed = []
        total = 0

        for slug in self.cfg["categories"]:
            url = f"https://www.amazon.com/gp/bestsellers/{slug}/"
            try:
                payload = self.fetch_cached(
                    f"bestsellers:{slug}",
                    lambda: self._scrape(url, max_per_category),
                    cost_usd=max_per_category,
                    actual_cost=lambda p: p["cost_usd"],
                )
            except CostCapReached:
                raise  # the daily cap stops the whole collector
            except Exception as e:
                # Anything else only skips this one category.
                self.log.warning("Category %s failed, skipping it: %s", slug, e)
                failed.append(slug)
                continue

            products = [i for i in payload["items"] if i.get("asin")]
            total += len(products)
            self.log.info("%s: %d products", slug, len(products))

        if failed:
            self.log.warning("Categories that failed today: %s", ", ".join(failed))
        if failed and len(failed) == len(self.cfg["categories"]):
            raise RuntimeError("Every Amazon category failed")
        return total

    def _scrape(self, url: str, max_charge_usd: float) -> dict:
        result = run_actor(
            ACTOR_ID,
            {"urls": [url], "subcategoryLevel": 0, "nextPage": self.cfg["top_100"]},
            max_charge_usd=max_charge_usd,
        )
        # The actor reports page problems as a row with an errorMessage and no
        # product. Treat "no products" as a failure so it's retried, not saved.
        if not any(i.get("asin") for i in result["items"]):
            errors = {i.get("errorMessage") for i in result["items"] if i.get("errorMessage")}
            raise RuntimeError(f"No products returned for {url} ({', '.join(errors) or 'empty'})")
        return result
