"""Kalodata keyword search: TikTok Shop saturation for each watchlist keyword.

For each keyword (last 7 days, US):
  * product ranking -> how many products and distinct sellers match, their
    revenue, and how concentrated sales are (saturation).
  * video ranking   -> shoppable videos about it: views, and how much of
    the attention/revenue comes from ads (ad_view_ratio, ad_revenue_ratio):
    the paid-vs-organic signal.

Cost: 0.1 credits per request, so 0.2 credits per keyword. The number of
keywords per run is capped in config.yaml (kalodata_keywords.max_keywords).
"""

from datetime import timedelta

from collectors.keywords import variant_keywords, watch_keywords
from collectors.kalodata import CREDITS_PER_PAGE, PAGE_SIZE, KalodataAuthError, KalodataCollector
from core.config import settings
from core.retry import PermanentError

SEARCHES = {
    # key prefix: (endpoint, extra request fields)
    "products": ("tiktok/product/rank", {"sort_field": {"field": "revenue", "type": "DESC"}, "need_extra": True}),
    "videos": ("tiktok/video/rank", {"sort_field": {"field": "views", "type": "DESC"}}),
}


class KalodataKeywordsCollector(KalodataCollector):
    name = "kalodata_keywords"

    def collect(self) -> int:
        kcfg = settings["kalodata_keywords"]
        keywords = watch_keywords(self.conn, kcfg["max_keywords"], self.name,
                                  lambda k: f"products:{k}", purpose="tiktok")
        if not keywords:
            self.log.info("No keywords to look up yet (watchlist and concepts are empty)")
            return 0

        # Look up as many keywords as the per-run cap and the balance allow,
        # most important first (the list is already in priority order).
        per_keyword = len(SEARCHES) * CREDITS_PER_PAGE
        balance = self.get_balance()
        spendable = min(kcfg["per_run_credit_cap"], balance - self.cfg["min_balance_reserve"])
        affordable = max(0, int(spendable / per_keyword + 1e-9))
        if affordable < len(keywords):
            self.log.warning(
                "Credits cover %d of %d keywords this run (balance %.2f, reserve %s, cap %s). "
                "Top up Kalodata credits to look up the rest.",
                affordable, len(keywords), balance, self.cfg["min_balance_reserve"],
                kcfg["per_run_credit_cap"])
            keywords = keywords[:affordable]
        if not keywords:
            raise PermanentError(
                f"Only {balance:.2f} Kalodata credits left; keeping a reserve of "
                f"{self.cfg['min_balance_reserve']}. Top up credits to continue."
            )
        self.log.info("Credit balance: %.2f. Looking up %d keywords (max %.1f credits)",
                      balance, len(keywords), len(keywords) * per_keyword)

        common = {"region": self.cfg["region"], "language": "en-US", "currency": "USD",
                  "date_range": "last7Day", "page_size": PAGE_SIZE, "page_number": 1}
        usd_per_page = CREDITS_PER_PAGE * self.cfg.get("usd_per_credit", 0)

        saved = 0
        for kw in keywords:
            for prefix, (endpoint, extra) in SEARCHES.items():
                body = {**common, **extra, "keyword": kw}
                try:
                    self.fetch_cached(
                        f"{prefix}:{kw}",
                        lambda: self._fetch_slowly(endpoint, body),
                        cost_usd=usd_per_page,
                    )
                    saved += 1
                except KalodataAuthError:
                    raise   # the key was rejected: stop, don't keep trying
                except Exception as e:
                    self.log.warning("%s search for %r failed, skipping: %s", prefix, kw, e)

        saved += self._search_variants(set(keywords), common, usd_per_page)

        after = self.get_balance()
        self.log.info("Credits used this run: %.2f (balance now %.2f)", balance - after, after)
        return saved

    def _search_variants(self, done: set, common: dict, usd_per_page: float) -> int:
        """For the top candidates, also search their other keywords (product
        search only, 0.1 credits each) so seller counts cover shops that word
        the product differently. Results are merged in the features step."""
        vcfg = settings["kalodata_keywords"]
        if not vcfg.get("variant_top_n"):
            return 0
        already = done | {r["request_key"].removeprefix("products:") for r in self.conn.execute(
            "select request_key from gapfinder.raw_responses where source = %s and snapshot_date >= %s",
            (self.name, self.snapshot_date - timedelta(days=self.snapshot_date.weekday())),
        ).fetchall()}
        variants = variant_keywords(self.conn, vcfg["variant_top_n"], vcfg["variants_per_concept"], already)
        spendable = min(vcfg["per_run_credit_cap"], self.get_balance() - self.cfg["min_balance_reserve"])
        variants = variants[:max(0, int(spendable / CREDITS_PER_PAGE + 1e-9))]
        if not variants:
            return 0
        self.log.info("Searching %d keyword variants for the top candidates", len(variants))
        endpoint, extra = SEARCHES["products"]
        saved = 0
        for kw in variants:
            body = {**common, **extra, "keyword": kw}
            try:
                self.fetch_cached(f"products:{kw}", lambda: self._fetch_slowly(endpoint, body),
                                  cost_usd=usd_per_page)
                saved += 1
            except KalodataAuthError:
                raise
            except Exception as e:
                self.log.warning("variant search for %r failed, skipping: %s", kw, e)
        return saved
