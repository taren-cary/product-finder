"""CJ Dropshipping: real product cost and US shipping for the top concepts.

API docs: https://developers.cjdropshipping.com (API 2.0). Free to use; free
accounts get 1,000 requests a day at 1 request per second. The API key is
made in CJ under My CJ -> Authorization -> API, and goes in .env as CJ_API_KEY.

For each concept keyword, in this order: shortlisted; then concepts with
TikTok Shop prices (so profit can be calculated), gift-ready ones first, by
score; then the rest by score. A concept is looked up again after
recheck_weeks:
  search:<keyword>  products stocked in a US warehouse that match the keyword
  freight:<pid>     for the best few matches (most of the keyword's words in
                    the title, enough US stock): the first variant's price and
                    the US-to-US shipping quotes with their delivery days

The relevance filter (concepts/relevance.py) then drops search results that
are a different product, and the features step uses the cheapest remaining
match as the concept's real product cost and shipping.

Settings are in config.yaml under "cj".
"""

import re
import time

import requests

from datetime import timedelta

from collectors.base import BaseCollector
from collectors.keywords import concept_keyword
from core.config import require_env, settings
from core.retry import PermanentError

BASE_URL = "https://developers.cjdropshipping.com/api2.0/v1"
SECONDS_BETWEEN_CALLS = 1.2   # the API allows 1 request per second


class CJAuthError(PermanentError):
    """The API key or token was rejected. Nothing else will work until it's fixed."""


def search_products(payload) -> list[dict]:
    """The product list from a stored search:<keyword> response."""
    content = ((payload or {}).get("data") or {}).get("content") or []
    return [p for block in content for p in (block.get("productList") or [])]


def _words(text: str) -> set[str]:
    """Lowercase words of 3+ letters, with a plural "s" dropped."""
    return {w[:-1] if w.endswith("s") and len(w) > 3 else w
            for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 3}


def word_match(keyword: str, title: str) -> float:
    """Share of the keyword's words that appear in a product title (0 to 1).
    CJ's own search order is loose ("board game" returns gaming tables), so
    this picks which results are worth a shipping quote."""
    wanted = _words(keyword)
    return len(wanted & _words(title)) / len(wanted) if wanted else 0.0


class CJCollector(BaseCollector):
    name = "cj"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cfg = settings["cj"]
        self.api_key = require_env("CJ_API_KEY")
        self.token = None
        self.requests_made = 0
        self._last_call = 0.0

    # --- API helpers -------------------------------------------------------

    def _call(self, method: str, endpoint: str, **kwargs) -> dict:
        if self.requests_made >= self.cfg["max_requests_per_run"]:
            raise PermanentError(f"Reached {self.cfg['max_requests_per_run']} CJ requests for this run")
        wait = SECONDS_BETWEEN_CALLS - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["CJ-Access-Token"] = self.token
        r = requests.request(method, f"{BASE_URL}/{endpoint}", headers=headers, timeout=60, **kwargs)
        self._last_call = time.monotonic()
        self.requests_made += 1
        if r.status_code in (401, 403):
            raise CJAuthError(f"CJ rejected the API key or token (HTTP {r.status_code})")
        if r.status_code == 429 or r.status_code >= 500:
            raise RuntimeError(f"CJ busy or down (HTTP {r.status_code}); will retry")
        if r.status_code >= 400:
            raise PermanentError(f"CJ HTTP {r.status_code}: {r.text[:300]}")
        body = r.json()
        if body.get("code") != 200 or body.get("result") is False:
            message = str(body.get("message") or "")
            if "too many" in message.lower() or "frequen" in message.lower():
                raise RuntimeError(f"CJ rate limit: {message}; will retry")
            if "token" in message.lower() or "apikey" in message.lower().replace(" ", ""):
                raise CJAuthError(f"CJ rejected the API key or token: {message}")
            raise PermanentError(f"CJ error: code={body.get('code')} message={message}")
        return body

    def _authenticate(self) -> None:
        # The same key returns the same token for 24 hours, so one call per run is fine.
        body = self._call("POST", "authentication/getAccessToken", json={"apiKey": self.api_key})
        self.token = body["data"]["accessToken"]

    # --- The weekly run ----------------------------------------------------

    def collect(self) -> int:
        keywords = self._keywords()
        if not keywords:
            self.log.info("No keywords to look up yet")
            return 0
        self._authenticate()
        self.log.info("Looking up %d keywords on CJ (US warehouses)", len(keywords))

        saved = 0
        for kw in keywords:
            params = {"keyWord": kw, "countryCode": "US", "page": 1,
                      "size": self.cfg["results_per_keyword"], "orderBy": 0}
            try:
                payload = self.fetch_cached(f"search:{kw}",
                                            lambda: self._call("GET", "product/listV2", params=params))
                saved += 1
            except CJAuthError:
                raise
            except PermanentError as e:
                if "requests for this run" in str(e):
                    self.log.warning("%s; the rest waits for the next run", e)
                    break
                self.log.warning("CJ search for %r failed, skipping: %s", kw, e)
                continue
            except Exception as e:
                self.log.warning("CJ search for %r failed, skipping: %s", kw, e)
                continue

            # Shipping quotes only for products that look like a real match and
            # have enough US stock, best match first.
            likely = [p for p in search_products(payload)
                      if word_match(kw, p.get("nameEn")) >= self.cfg["min_word_match"]
                      and (p.get("warehouseInventoryNum") or 0) >= self.cfg["min_us_inventory"]]
            likely.sort(key=lambda p: word_match(kw, p.get("nameEn")), reverse=True)
            for product in likely[:self.cfg["freight_checks_per_keyword"]]:
                try:
                    self.fetch_cached(f"freight:{product['id']}", lambda: self._freight(product["id"]))
                    saved += 1
                except CJAuthError:
                    raise
                except Exception as e:
                    self.log.warning("CJ shipping quote for %s failed, skipping: %s", product.get("id"), e)
                    if "requests for this run" in str(e):
                        break

        self.log.info("Made %d CJ requests this run", self.requests_made)
        return saved

    def _keywords(self) -> list[str]:
        """Concept keywords to look up, most useful first, skipping ones
        searched within recheck_weeks."""
        rows = self.conn.execute(
            """
            select c.name, c.keywords
            from gapfinder.concepts c
            left join lateral (
                select opportunity_score, typical_price from gapfinder.concept_weekly
                where concept_id = c.id order by week_start desc limit 1
            ) w on true
            where c.tiktok_fit is true and c.merged_into_id is null and c.review_status <> 'rejected'
            order by (c.review_status = 'shortlisted') desc,
                     (w.typical_price is not null) desc,
                     coalesce(c.giftable and c.easy_demo, false) desc,
                     w.opportunity_score desc nulls last
            """
        ).fetchall()
        since = self.snapshot_date - timedelta(weeks=self.cfg["recheck_weeks"])
        recent = {r["request_key"].removeprefix("search:") for r in self.conn.execute(
            "select request_key from gapfinder.raw_responses where source = %s and snapshot_date > %s",
            (self.name, since)).fetchall()}
        out = []
        for r in rows:
            kw = concept_keyword(r)
            if kw not in recent and kw not in out:
                out.append(kw)
        return out[:self.cfg["max_keywords"]]

    def _freight(self, pid: str) -> dict:
        """The product's first variant (price, weight) and its US-to-US shipping quotes."""
        detail = self._call("GET", "product/query", params={"pid": pid, "countryCode": "US"})["data"] or {}
        variants = detail.get("variants") or []
        if not variants:
            return {"pid": pid, "variant": None, "quotes": []}
        v = variants[0]
        quotes = self._call("POST", "logistic/freightCalculate", json={
            "startCountryCode": "US", "endCountryCode": "US",
            "products": [{"quantity": 1, "vid": v["vid"]}],
        })["data"] or []
        return {
            "pid": pid,
            "name": detail.get("productNameEn"),
            "variant": {"vid": v.get("vid"), "key": v.get("variantKey"),
                        "price": v.get("variantSellPrice"), "weight_g": v.get("variantWeight")},
            "quotes": [{"name": q.get("logisticName"), "price": q.get("logisticPrice"),
                        "days": q.get("logisticAging")} for q in quotes],
        }
