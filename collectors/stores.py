"""Competitor store tracker (free): what other online stores are selling.

For each store in gapfinder.stores (found by the Meta ads collector, or
added by hand in config.yaml under "stores: manual"), once a week:
  * /products.json   every product the store lists, with the date it was
                     added. Shopify serves this publicly on almost every store;
                     a store without it isn't on Shopify and is marked
                     not_shopify (not checked again).
  * /collections/all?sort_by=best-selling
                     the store's own best-seller order, which Shopify computes
                     from the store's real sales. Read from the page's product
                     links, keeping only handles that are real products.

Week over week this shows which products a store keeps (working), which are
new (being tested), and which climb its best-seller list. Plain web requests,
spaced out politely; no paid API.
"""

import re
import time

import requests

from collectors.base import BaseCollector
from core.config import settings
from core.retry import PermanentError

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
SECONDS_BETWEEN_REQUESTS = 1.0
PRODUCT_LINK = re.compile(r'href="(?:https?://[^"/]+)?/(?:collections/[^"/]+/)?products/([^"/?#]+)')


class NotShopify(PermanentError):
    """The store doesn't serve Shopify's products.json."""


class Unreachable(PermanentError):
    """The site is down, doesn't exist, or its certificate is broken; retrying now won't help."""


class StoreTrackerCollector(BaseCollector):
    name = "stores"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cfg = settings["stores"]
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._last = 0.0

    def collect(self) -> int:
        self._add_manual_stores()
        # Strongest advertisers first (most ads running 30+ days in the last
        # 4 weeks), so the best stores are covered even if a run stops early.
        stores = self.conn.execute(
            """
            with ads as (
                select ad->>'domain' as domain,
                       count(distinct ad->>'ad_id') filter (
                           where r.snapshot_date - (ad->>'start_date')::date >= 30) as long_ads,
                       count(distinct ad->>'ad_id') as ads
                from gapfinder.raw_responses r, jsonb_array_elements(r.payload->'ads') ad
                where r.source = 'meta_ads' and r.snapshot_date > %s - 28
                group by 1
            )
            select s.domain from gapfinder.stores s left join ads a using (domain)
            where s.status in ('new', 'tracking', 'unreachable')
              and (s.last_checked is null or s.last_checked < %s)
            order by (s.source = 'manual') desc, coalesce(a.long_ads, 0) desc, coalesce(a.ads, 0) desc
            limit %s
            """,
            (self.snapshot_date, self.snapshot_date, self.cfg["max_stores_per_run"]),
        ).fetchall()
        if not stores:
            self.log.info("No stores to check (the Meta ads collector finds them)")
            return 0
        self.log.info("Checking %d stores", len(stores))

        saved = 0
        for row in stores:
            domain = row["domain"]
            try:
                payload = self.fetch_cached(f"store:{domain}", lambda: self._check(domain))
                status, count = "tracking", len(payload["products"])
                saved += 1
            except NotShopify:
                status, count = "not_shopify", None
            except Exception as e:
                self.log.info("%s unreachable: %s", domain, e)
                status, count = "unreachable", None
            self.conn.execute(
                """
                update gapfinder.stores
                set status = %s, last_checked = %s, updated_at = now(),
                    product_count = coalesce(%s, product_count)
                where domain = %s
                """,
                (status, self.snapshot_date, count, domain),
            )
            self.conn.commit()
        return saved

    def _add_manual_stores(self) -> None:
        for d in self.cfg.get("manual") or []:
            d = d.lower().strip().removeprefix("https://").removeprefix("http://").removeprefix("www.").rstrip("/")
            self.conn.execute(
                """
                insert into gapfinder.stores (domain, source, first_seen) values (%s, 'manual', %s)
                on conflict (domain) do update set source = 'manual',
                    status = case when gapfinder.stores.status = 'ignored' then 'ignored' else gapfinder.stores.status end
                """,
                (d, self.snapshot_date),
            )
        self.conn.commit()

    # --- One store ---------------------------------------------------------

    def _get(self, url: str) -> requests.Response:
        wait = SECONDS_BETWEEN_REQUESTS - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            return self.session.get(url, timeout=20, allow_redirects=True)
        except (requests.exceptions.ConnectionError, requests.exceptions.SSLError,
                requests.exceptions.TooManyRedirects) as e:
            raise Unreachable(str(e)[:200]) from e
        finally:
            self._last = time.monotonic()

    def _check(self, domain: str) -> dict:
        products = []
        for page in range(1, self.cfg["max_product_pages"] + 1):
            r = self._get(f"https://{domain}/products.json?limit=250&page={page}")
            if r.status_code in (404, 403, 401) and page == 1:
                raise NotShopify(domain)
            r.raise_for_status()
            try:
                batch = r.json().get("products")
            except ValueError:
                if page == 1:
                    raise NotShopify(domain)
                break
            if batch is None and page == 1:
                raise NotShopify(domain)
            if not batch:
                break
            products += [_trim(p) for p in batch]
            if len(batch) < 250:
                break
        return {
            "domain": domain,
            "final_host": (r.url.split("/")[2] if r.url else domain),
            "products": products,
            "best_sellers": self._best_sellers(domain, {p["handle"] for p in products}),
        }

    def _best_sellers(self, domain: str, handles: set[str]) -> list[str]:
        """Product handles in the store's best-selling order (top of the first page)."""
        try:
            r = self._get(f"https://{domain}/collections/all?sort_by=best-selling")
            if r.status_code != 200:
                return []
        except (requests.RequestException, Unreachable):
            return []
        order = []
        for h in PRODUCT_LINK.findall(r.text):
            h = h.lower()
            if h in handles and h not in order:
                order.append(h)
        return order[: self.cfg["best_sellers_kept"]]


def _trim(p: dict) -> dict:
    variants = p.get("variants") or []
    prices = [float(v["price"]) for v in variants if v.get("price")]
    compare = [float(v["compare_at_price"]) for v in variants if v.get("compare_at_price")]
    images = p.get("images") or []
    return {
        "id": p.get("id"),
        "handle": (p.get("handle") or "").lower(),
        "title": p.get("title"),
        "type": p.get("product_type"),
        "vendor": p.get("vendor"),
        "created_at": p.get("created_at"),
        "published_at": p.get("published_at"),
        "price": min(prices) if prices else None,
        "compare_at": max(compare) if compare else None,
        "variants": len(variants),
        "skus": [v.get("sku") for v in variants[:3] if v.get("sku")],
        "image": images[0].get("src") if images else None,
    }
