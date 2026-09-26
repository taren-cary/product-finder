"""Runs a pipeline step (normalize, concepts, ...) safely and logs it.

Works like run_collector: the step's outcome, record count and cost go into
gapfinder.source_runs (with the step's name as the source), and any error is
logged without stopping the rest of the pipeline.

A step is a function: step(conn, snapshot_date) -> {"records": int, "cost_usd": float}
"""

import logging
from datetime import date

from core.db import connect

log = logging.getLogger(__name__)


class StepStopped(RuntimeError):
    """Raised by a step that has to stop partway (e.g. out of API credit).
    Carries what was already spent, so the run log records the true cost."""

    def __init__(self, message: str, cost_usd: float = 0.0, records: int = 0):
        super().__init__(message)
        self.cost_usd = cost_usd
        self.records = records


def run_step(name: str, step_fn, pipeline_run_id: int | None, snapshot_date: date) -> str:
    """Run one step. Never raises. Returns "success" or "failed"."""
    log.info("--- %s: starting ---", name)
    try:
        with connect() as conn:
            run_id = conn.execute(
                """
                insert into gapfinder.source_runs (pipeline_run_id, source, snapshot_date)
                values (%s, %s, %s) returning id
                """,
                (pipeline_run_id, name, snapshot_date),
            ).fetchone()["id"]
            conn.commit()

            try:
                result = step_fn(conn, snapshot_date) or {}
                status, error = "success", None
                log.info("%s: %d records (est. cost $%.4f)",
                         name, result.get("records", 0), result.get("cost_usd", 0))
            except Exception as e:
                conn.rollback()
                # Keep the cost and progress of work done before the failure.
                result = {"records": getattr(e, "records", 0), "cost_usd": getattr(e, "cost_usd", 0)}
                status, error = "failed", f"{type(e).__name__}: {e}"
                log.exception("%s: failed; continuing", name)

            conn.execute(
                """
                update gapfinder.source_runs
                set finished_at = now(), status = %s, records_saved = %s,
                    estimated_cost_usd = %s, error_message = %s
                where id = %s
                """,
                (status, result.get("records", 0), result.get("cost_usd", 0), error, run_id),
            )
        return status
    except Exception:
        log.exception("%s: database error around the step; skipping", name)
        return "failed"
