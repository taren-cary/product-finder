"""Runs the whole daily pipeline, in order.

    python run_daily.py                 # run everything
    python run_daily.py --only kalodata # run just one collector now (for testing)

Each step runs on its own: if one fails, it is logged and the rest still run.
Windows Task Scheduler calls this once a day at noon. Each collector has its
own schedule in config.yaml (daily or weekly), so most days only the daily
collectors do anything.
"""

import argparse
import logging
import sys
from datetime import date, timedelta

from collectors.amazon import AmazonBestsellersCollector
from collectors.base import run_collector
from collectors.google_trends import GoogleTrendsCollector
from collectors.kalodata import KalodataCollector
from collectors.kalodata_keywords import KalodataKeywordsCollector
from collectors.reddit import RedditCollector
from collectors.tiktok import TikTokCollector
from concepts import run as concepts
from core.config import settings
from core.db import connect
from core.logging_setup import setup_logging
from core.steps import run_step
from features import run as features
from normalize import run as normalize
from scoring import run as scoring

log = logging.getLogger("run_daily")

# The pipeline, in order:
#   1. discovery collectors  - find candidate products
#   2. steps                 - turn raw data into items, group items into concepts
#   3. enrichment collectors - look up watchlist + concept keywords elsewhere
#      (after step 2, so new concepts get looked up the same run)
#   4. later steps           - weekly metrics per concept, then the opportunity score
# Switch collectors on/off and set their schedule in config.yaml.
DISCOVERY_COLLECTORS = [
    KalodataCollector,
    AmazonBestsellersCollector,
    RedditCollector,
]

STEPS = [
    ("normalize", normalize.run),
    ("concepts", concepts.run),
]

ENRICHMENT_COLLECTORS = [
    GoogleTrendsCollector,
    TikTokCollector,
    KalodataKeywordsCollector,
]

LATER_STEPS = [
    ("features", features.run),
    ("scoring", scoring.run),
]

ALL_COLLECTORS = DISCOVERY_COLLECTORS + ENRICHMENT_COLLECTORS


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def is_due(name: str, schedule: str, today: date) -> bool:
    """Should this collector run today?

    daily  -> every day.
    weekly -> on the weekly run day from config.yaml. If that day was missed
              (PC off all day, or the run failed), it catches up on the next
              day the pipeline runs, so no week is skipped.
    """
    if schedule == "daily":
        return True
    run_day = WEEKDAYS.index(settings["weekly_run_day"].lower())
    if today.weekday() == run_day:
        return True
    # Catch-up: the most recent run day, and whether it succeeded since then.
    last_run_day = today - timedelta(days=(today.weekday() - run_day) % 7)
    with connect() as conn:
        row = conn.execute(
            """
            select 1 from gapfinder.source_runs
            where source = %s and snapshot_date >= %s and status in ('success', 'partial')
            limit 1
            """,
            (name, last_run_day),
        ).fetchone()
    if row is None:
        log.info("%s missed its weekly run on %s; catching up today", name, last_run_day)
        return True
    return False


def collectors_to_run(only: str | None, today: date):
    """Pick the collectors that are switched on and due today.
    --only runs that one collector regardless of its switch or schedule."""
    if only:
        return [c for c in ALL_COLLECTORS if c.name == only]

    switches = settings.get("collectors") or {}
    chosen = []
    for c in ALL_COLLECTORS:
        cfg = switches.get(c.name) or {}
        if not cfg.get("enabled", False):
            continue
        schedule = cfg.get("schedule", "weekly")
        try:
            due = is_due(c.name, schedule, today)
        except Exception:
            log.exception("%s: could not check its schedule; running it to be safe", c.name)
            due = True
        if due:
            chosen.append(c)
        else:
            log.info("%s: not due today (%s)", c.name, schedule)
    return chosen


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the daily Product Gap Finder pipeline.")
    parser.add_argument("--only", help="run just this one collector, e.g. kalodata")
    args = parser.parse_args()

    setup_logging()
    today = date.today()
    log.info("===== Daily run for %s =====", today)

    # Record the start of the run. If the database is unreachable nothing
    # else can work, so stop here with a clear message.
    try:
        with connect() as conn:
            run_id = conn.execute(
                "insert into gapfinder.pipeline_runs default values returning id"
            ).fetchone()["id"]
    except Exception:
        log.exception("Could not reach the database. Check DATABASE_URL and DATABASE_PASSWORD in .env.")
        return 1

    results = {}
    collectors = collectors_to_run(args.only, today)
    if args.only and not collectors:
        log.error("No collector named %r. Known: %s", args.only, [c.name for c in ALL_COLLECTORS])

    for collector_class in [c for c in collectors if c in DISCOVERY_COLLECTORS]:
        results[collector_class.name] = run_collector(collector_class, run_id, today)

    # Steps are cheap and only process what's new, so they run every day.
    if not args.only:
        for step_name, step_fn in STEPS:
            results[step_name] = run_step(step_name, step_fn, run_id, today)

    for collector_class in [c for c in collectors if c in ENRICHMENT_COLLECTORS]:
        results[collector_class.name] = run_collector(collector_class, run_id, today)

    if not args.only:
        for step_name, step_fn in LATER_STEPS:
            results[step_name] = run_step(step_name, step_fn, run_id, today)

    # Overall status: success if everything worked, failed if nothing did.
    statuses = set(results.values())
    if not results or statuses == {"success"}:
        overall = "success"
    elif "success" in statuses:
        overall = "partial"
    else:
        overall = "failed"

    try:
        with connect() as conn:
            conn.execute(
                "update gapfinder.pipeline_runs set finished_at = now(), status = %s where id = %s",
                (overall, run_id),
            )
    except Exception:
        log.exception("Could not record the end of the run")

    log.info("===== Run finished: %s =====", overall)
    for name, status in results.items():
        log.info("  %-20s %s", name, status)
    if not results:
        log.info("  (no steps are set up yet)")

    return 0 if overall != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
