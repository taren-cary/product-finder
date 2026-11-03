"""Meta Ad Library collector (via Apify): who is running product ads right now.

For each search term in config.yaml ("meta_ads"), fetches currently active US
ads from Meta's Ad Library, highest impressions first, and saves the fields
we use for each ad:
  * when it started running (an ad still running after 30+ days is very
    likely profitable; nobody keeps paying for a losing ad that long),
  * how many near-identical versions Meta grouped with it (collation_count;
    more versions = the advertiser is testing and scaling it),
  * the landing page, so ads can be tied to a store and often to the exact
    product page (".../products/<handle>").

Each new landing-page domain becomes a store in gapfinder.stores, which the
store tracker (collectors/stores.py) then checks for its products and best
sellers. Marketplaces, social sites and link shorteners are skipped.

Actor: igolaizola/facebook-ad-library-scraper, ~$0.00025-0.0005 per ad.
"""

import re
from datetime import date, datetime, timezone
from urllib.parse import urlparse

from collectors.apify import run_actor
from collectors.base import BaseCollector, CostCapReached
from core.config import settings

ACTOR_ID = "igolaizola~facebook-ad-library-scraper"
PER_AD_USD = 0.0005   # Starter-plan price; the run's real cost is what's recorded

# Landing pages that aren't somebody's own store.
NOT_STORES = {
    "amazon.com", "amzn.to", "amzlink.to", "a.co", "walmart.com", "target.com", "etsy.com", "ebay.com", "temu.com",
    "shein.com", "aliexpress.com", "tiktok.com", "facebook.com", "fb.me", "fb.com", "m.me",
    "instagram.com", "messenger.com", "whatsapp.com", "wa.me", "youtube.com", "youtu.be",
    "linktr.ee", "bit.ly", "tinyurl.com", "apps.apple.com", "play.google.com", "google.com",
    "forms.gle", "calendly.com", "eventbrite.com", "shop.app", "apple.com", "itunes.apple.com",
    "charitywater.org", "gofundme.com", "spotify.com", "audible.com",
}


def store_domain(url: str | None) -> str | None:
    """The store's domain from an ad's landing page, or None if it isn't a store."""
    if not url:
        return None
    host = (urlparse(url if "//" in url else "https://" + url).hostname or "").lower()
    host = host.removeprefix("www.")
    if not host or "." not in host:
        return None
    if any(host == d or host.endswith("." + d) for d in NOT_STORES):
        return None
    return host


def product_handle(url: str | None) -> str | None:
    """The Shopify product handle in a landing page URL (".../products/<handle>")."""
    m = re.search(r"/products/([^/?#]+)", url or "")
    return m.group(1).lower() if m else None


class MetaAdsCollector(BaseCollector):
    name = "meta_ads"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cfg = settings["meta_ads"]
        self.cost_cap_usd = self.cfg["max_cost_per_run_usd"]

    def collect(self) -> int:
        terms = self.cfg["search_terms"]
        per_term = self.cfg["ads_per_term"]
        saved = 0
        for term in terms:
            try:
                payload = self.fetch_cached(
                    f"search:{term}", lambda: self._search(term, per_term),
                    cost_usd=PER_AD_USD * per_term + 0.01, actual_cost=lambda p: p["cost_usd"],
                )
            except CostCapReached:
                raise
            except Exception as e:
                self.log.warning("Ad Library search for %r failed, skipping: %s", term, e)
                continue
            saved += 1
            self._add_stores(payload["ads"])
        return saved

    def _search(self, term: str, max_ads: int) -> dict:
        run = run_actor(ACTOR_ID, {
            "query": term, "country": "US", "activeStatus": "active",
            "sortBy": "impressions", "mediaType": "all", "maxItems": max_ads,
        }, max_charge_usd=PER_AD_USD * max_ads + 0.01, memory_mb=1024)
        return {"term": term, "cost_usd": run["cost_usd"],
                "ads": [a for a in (_trim(item) for item in run["items"]) if a]}

    def _add_stores(self, ads: list[dict]) -> None:
        for ad in ads:
            if ad["domain"]:
                self.conn.execute(
                    """
                    insert into gapfinder.stores (domain, name, source, first_seen)
                    values (%s, %s, 'meta_ads', %s)
                    on conflict (domain) do nothing
                    """,
                    (ad["domain"], ad["page_name"], self.snapshot_date),
                )
        self.conn.commit()


def _trim(item: dict) -> dict | None:
    """Keep only the fields we use (full records carry bulky media metadata)."""
    if not item.get("ad_archive_id"):
        return None
    snap = item.get("snapshot") or {}
    cards = snap.get("cards") or []
    link = snap.get("link_url") or next((c.get("link_url") for c in cards if c.get("link_url")), None)
    images = snap.get("images") or []
    videos = snap.get("videos") or []
    image = (images[0].get("resized_image_url") if images else None) \
        or (videos[0].get("video_preview_image_url") if videos else None) \
        or next((c.get("resized_image_url") for c in cards if c.get("resized_image_url")), None)
    start = item.get("start_date")
    return {
        "ad_id": str(item["ad_archive_id"]),
        "page_id": str(item.get("page_id") or ""),
        "page_name": item.get("page_name") or snap.get("page_name"),
        "start_date": (datetime.fromtimestamp(start, timezone.utc).date().isoformat() if start else None),
        "versions": item.get("collation_count") or 1,
        "platforms": item.get("publisher_platform") or [],
        "format": snap.get("display_format"),
        "cta": snap.get("cta_type"),
        "title": snap.get("title") or next((c.get("title") for c in cards if c.get("title")), None),
        "body": ((snap.get("body") or {}).get("text") or "")[:500],
        "link_url": link,
        "domain": store_domain(link),
        "handle": product_handle(link),
        "image": image,
    }


def ad_days(ad: dict, on: date) -> int | None:
    """How many days an ad had been running on a given date."""
    if not ad.get("start_date"):
        return None
    return (on - date.fromisoformat(ad["start_date"])).days
