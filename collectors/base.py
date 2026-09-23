"""The shared base that every collector builds on.

A collector only has to do one thing: implement collect(), fetching its data
through self.fetch_cached(). The base class takes care of the rest:

  * Caching: a request already stored today is read from the database
    instead of being fetched (and paid for) again.
  * Retries: failed requests are retried with increasing waits.
  * Raw storage: every response is saved as-is in gapfinder.raw_responses.
  * Cost tracking and caps: pass each request's estimated cost to
    fetch_cached(). It's only counted when we actually fetch, and the
    collector stops fetching once it would go over its daily cap.
  * Run log: each run is recorded in gapfinder.source_runs with its status,
    record count, cost and any error.
  * Failing gracefully: run_collector() catches every error, logs it and
    returns, so one broken source never stops the pipeline.

Example collector:

    class ExampleCollector(BaseCollector):
        name = "example"
        cost_cap_usd = 2.00

        def collect(self) -> int:
            payload = self.fetch_cached(
                "top_products", lambda: call_the_api(), cost_usd=0.05
            )
            return len(payload["products"])
"""

import logging
from datetime import date

from psycopg.types.json import Jsonb

from core.db import connect
from core.retry import with_retries

log = logging.getLogger(__name__)


class CostCapReached(Exception):
    """Raised when the next request would take a collector over its cap."""


class BaseCollector:
    # Short source name, e.g. "kalodata". Used in the database and config.yaml.
    name: str = ""
    # Max spend per day in USD, across all runs that day (so re-running
    # the pipeline can't double the bill). None means no cap (free sources).
    cost_cap_usd: float | None = None

    def __init__(self, conn, source_run_id: int, snapshot_date: date):
        self.conn = conn
        self.source_run_id = source_run_id
        self.snapshot_date = snapshot_date
        self.cost_usd = 0.0          # spend so far in this run
        # Spend already recorded by earlier runs today.
        self.spent_earlier_today = float(conn.execute(
            """
            select coalesce(sum(estimated_cost_usd), 0) as spent
            from gapfinder.source_runs
            where source = %s and snapshot_date = %s and id <> %s
            """,
            (self.name, snapshot_date, source_run_id),
        ).fetchone()["spent"])
        self.responses_saved = 0     # new raw responses stored in this run
        self.log = logging.getLogger(f"collectors.{self.name}")

    def collect(self) -> int:
        """Fetch and store today's data. Return the number of records saved."""
        raise NotImplementedError

    def fetch_cached(self, request_key: str, fetch_fn, cost_usd: float = 0.0, actual_cost=None):
        """Return the stored response for request_key if we already have
        today's copy; otherwise call fetch_fn() (with retries), store the
        result, and return it.

        request_key should describe the request uniquely,
        e.g. "movers:beauty" or "hashtag:heatlesscurls".
        cost_usd is the most one real fetch can cost (0 for free APIs); it's
        used for the cap check. If the real cost is only known afterwards
        (e.g. Apify reports it), pass actual_cost: a function that reads it
        from the response, and that amount is recorded instead.
        """
        cached = self.get_cached(request_key)
        if cached is not None:
            self.log.info("Cache hit: %s (already fetched today)", request_key)
            return cached

        self.check_cap(cost_usd)
        payload = with_retries(fetch_fn, description=f"{self.name} {request_key}")
        self.cost_usd += actual_cost(payload) if actual_cost else cost_usd
        self.save_raw(request_key, payload)
        return payload

    def fetch_batched(self, keys: list[str], batch_size: int, fetch_batch_fn, max_cost_per_batch: float) -> dict:
        """Like fetch_cached, but for APIs that take many items in one call
        (e.g. 10 keywords in one Apify run).

        keys:            request keys, e.g. ["trends:sunset lamp", ...]
        fetch_batch_fn:  takes a list of keys, returns (results, cost_usd) where
                         results maps each key to its payload. Keys missing from
                         results count as failed and are skipped.
        Returns {key: payload} for every key we have today (cached or new).
        A failed batch is logged and skipped; the other batches still run.
        """
        out = {}
        todo = []
        for k in keys:
            cached = self.get_cached(k)
            if cached is not None:
                out[k] = cached
            else:
                todo.append(k)
        if len(out):
            self.log.info("Cache hit for %d of %d keys (already fetched today)", len(out), len(keys))

        for i in range(0, len(todo), batch_size):
            batch = todo[i:i + batch_size]
            self.check_cap(max_cost_per_batch)
            try:
                results, cost = with_retries(lambda: fetch_batch_fn(batch),
                                             description=f"{self.name} batch of {len(batch)}")
            except Exception as e:
                self.log.warning("Batch failed, skipping %d keys: %s", len(batch), e)
                continue
            self.cost_usd += cost
            for k in batch:
                if k in results:
                    self.save_raw(k, results[k])
                    out[k] = results[k]
                else:
                    self.log.warning("No data returned for %s", k)
        return out

    # --- Building blocks, for collectors that batch several requests into
    # --- one paid call (e.g. 10 keywords in one Apify run) -----------------

    def get_cached(self, request_key: str):
        """Today's stored response for request_key, or None."""
        row = self.conn.execute(
            """
            select payload from gapfinder.raw_responses
            where source = %s and snapshot_date = %s and request_key = %s
            """,
            (self.name, self.snapshot_date, request_key),
        ).fetchone()
        return row["payload"] if row else None

    def check_cap(self, next_cost_usd: float) -> None:
        """Raise CostCapReached if spending next_cost_usd would go over the cap."""
        spent_today = self.spent_earlier_today + self.cost_usd
        if self.cost_cap_usd is not None and spent_today + next_cost_usd > self.cost_cap_usd:
            raise CostCapReached(
                f"{self.name} stopped at its ${self.cost_cap_usd:.2f} daily cap "
                f"(spent ${spent_today:.2f} today; next request would cost ${next_cost_usd:.2f})"
            )

    def save_raw(self, request_key: str, payload) -> None:
        """Store one response in raw_responses."""
        self.conn.execute(
            """
            insert into gapfinder.raw_responses
                (source, snapshot_date, request_key, source_run_id, payload)
            values (%s, %s, %s, %s, %s)
            """,
            (self.name, self.snapshot_date, request_key, self.source_run_id, Jsonb(payload)),
        )
        # Commit right away so data we paid for is kept even if a later request fails.
        self.conn.commit()
        self.responses_saved += 1


def run_collector(collector_class, pipeline_run_id: int | None, snapshot_date: date) -> str:
    """Run one collector safely and record the outcome in source_runs.

    Never raises. Returns the final status: "success", "partial" or "failed".
    """
    name = collector_class.name
    log.info("--- %s: starting ---", name)
    try:
        return _run_collector(collector_class, pipeline_run_id, snapshot_date)
    except Exception:
        # Only reached if the database itself is unreachable or broken.
        log.exception("%s: database error around the run; skipping", name)
        return "failed"


def _run_collector(collector_class, pipeline_run_id, snapshot_date) -> str:
    name = collector_class.name
    with connect() as conn:
        source_run_id = conn.execute(
            """
            insert into gapfinder.source_runs (pipeline_run_id, source, snapshot_date)
            values (%s, %s, %s) returning id
            """,
            (pipeline_run_id, name, snapshot_date),
        ).fetchone()["id"]
        conn.commit()

        collector = None
        try:
            # Created inside the try: setup can fail too (e.g. a missing key in .env).
            collector = collector_class(conn, source_run_id, snapshot_date)
            records = collector.collect()
            status, error = "success", None
            log.info("%s: saved %d records (est. cost $%.4f)", name, records, collector.cost_usd)
        except CostCapReached as e:
            # Not an error: we keep what was fetched and stop spending.
            conn.rollback()
            records, status, error = collector.responses_saved, "partial", str(e)
            log.warning("%s: %s. Kept %d responses fetched before the cap.",
                        name, e, records)
        except Exception as e:
            conn.rollback()  # discard any half-finished work in the current transaction
            records, status, error = 0, "failed", f"{type(e).__name__}: {e}"
            log.exception("%s: failed; skipping and continuing", name)

        conn.execute(
            """
            update gapfinder.source_runs
            set finished_at = now(), status = %s, records_saved = %s,
                estimated_cost_usd = %s, error_message = %s
            where id = %s
            """,
            (status, records, collector.cost_usd if collector else 0, error, source_run_id),
        )

    return status
