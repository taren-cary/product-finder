"""Kalodata collector: TikTok Shop products and categories.

API docs: https://www.kalodata.com/open-center/docs (a copy is saved in
docs/kalodata/openapi.json). Every call is a POST with the API key in the
X-API-Key header. Ranking calls cost 0.1 credits per 100 rows returned.

What we pull each run is set in config.yaml under "kalodata": a list of
ranking queries (fastest-growing products, top sellers, new launches,
category rankings). Each page of results is stored as-is in raw_responses.

Safety checks before spending anything:
  * The planned credits for the run must fit under per_run_credit_cap.
  * The account balance must stay above min_balance_reserve afterwards.
"""

import time

import requests

from collectors.base import BaseCollector
from core.config import require_env, settings
from core.retry import PermanentError

BASE_URL = "https://www.kalodata.com/openapi/v1"
PAGE_SIZE = 100               # the API's maximum rows per request
CREDITS_PER_PAGE = 0.1        # ranking endpoints: 0.1 credits per 100 rows
SECONDS_BETWEEN_CALLS = 1.5   # stay well inside the rate limits


class KalodataAuthError(PermanentError):
    """The API key was rejected. Nothing else will work until it's fixed."""


class KalodataCollector(BaseCollector):
    name = "kalodata"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cfg = settings["kalodata"]
        self.headers = {
            "X-API-Key": require_env("KALODATA_API_KEY"),
            "Content-Type": "application/json",
        }

    # --- API helpers -------------------------------------------------------

    def _check_response(self, r: requests.Response) -> dict:
        """Turn an HTTP response into data, or raise a clear error."""
        if r.status_code in (401, 403):
            raise KalodataAuthError(f"Kalodata rejected the API key (HTTP {r.status_code})")
        if r.status_code == 429 or r.status_code >= 500:
            raise RuntimeError(f"Kalodata busy or down (HTTP {r.status_code}); will retry")
        if r.status_code >= 400:
            raise PermanentError(f"Kalodata HTTP {r.status_code}: {r.text[:300]}")
        body = r.json()
        if not body.get("success"):
            # A bad request won't get better by retrying.
            raise PermanentError(
                f"Kalodata error: code={body.get('code')} message={body.get('message')}"
            )
        return body

    def get_balance(self) -> float:
        """Remaining credits. This call is free."""
        r = requests.get(f"{BASE_URL}/credit/balance", headers=self.headers, timeout=30)
        return float(self._check_response(r)["data"]["totalRemain"])

    def post(self, endpoint: str, body: dict) -> dict:
        r = requests.post(f"{BASE_URL}/{endpoint}", headers=self.headers, json=body, timeout=60)
        return self._check_response(r)

    # --- The weekly run ----------------------------------------------------

    def collect(self) -> int:
        queries = self.cfg["queries"]
        planned_credits = sum(q.get("pages", 1) for q in queries) * CREDITS_PER_PAGE

        if planned_credits > self.cfg["per_run_credit_cap"]:
            raise PermanentError(
                f"Planned {planned_credits:.2f} credits is over the per-run cap of "
                f"{self.cfg['per_run_credit_cap']}. Reduce the queries or raise the cap in config.yaml."
            )

        balance_before = self.get_balance()
        self.log.info("Credit balance: %.2f. Planned for this run (max): %.2f",
                      balance_before, planned_credits)
        if balance_before - planned_credits < self.cfg["min_balance_reserve"]:
            raise PermanentError(
                f"Only {balance_before:.2f} Kalodata credits left; keeping a reserve of "
                f"{self.cfg['min_balance_reserve']}. Top up credits to continue."
            )

        # Settings shared by every query.
        common = {
            "region": self.cfg["region"],
            "language": "en-US",
            "currency": "USD",
            "page_size": PAGE_SIZE,
        }
        usd_per_page = CREDITS_PER_PAGE * self.cfg.get("usd_per_credit", 0)

        total_rows = 0
        for q in queries:
            for page in range(1, q.get("pages", 1) + 1):
                body = {**common, **q["body"], "page_number": page}
                payload = self.fetch_cached(
                    f"{q['key']}:p{page}",
                    lambda: self._fetch_slowly(q["endpoint"], body),
                    cost_usd=usd_per_page,
                )
                rows = len(payload.get("data") or [])
                total_rows += rows
                self.log.info("%s page %d: %d rows", q["key"], page, rows)
                if rows < PAGE_SIZE:
                    break  # no more pages

        balance_after = self.get_balance()
        self.log.info("Credits used this run: %.2f (balance now %.2f)",
                      balance_before - balance_after, balance_after)
        return total_rows

    def _fetch_slowly(self, endpoint: str, body: dict) -> dict:
        time.sleep(SECONDS_BETWEEN_CALLS)
        return self.post(endpoint, body)
