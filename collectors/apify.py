"""Small helper for running Apify actors (ready-made scrapers).

Usage:
    from collectors.apify import run_actor
    result = run_actor("some-user~some-actor", {"input": "..."}, max_charge_usd=0.25)
    result["items"]          # the scraped records
    result["cost_usd"]       # what Apify actually charged for this run

max_charge_usd is a hard limit enforced by Apify itself: the actor stops
once it has charged that much, so a misbehaving run can't overspend.
"""

import time

import requests

from core.config import require_env
from core.retry import PermanentError

API = "https://api.apify.com/v2"
POLL_SECONDS = 10
MAX_WAIT_SECONDS = 15 * 60


def _headers() -> dict:
    return {"Authorization": "Bearer " + require_env("APIFY_API_TOKEN")}


def _check(r: requests.Response) -> dict:
    if r.status_code in (401, 403):
        raise PermanentError(f"Apify rejected the token or access (HTTP {r.status_code}): {r.text[:200]}")
    if r.status_code == 402:
        raise PermanentError("Apify says the account is out of credits (HTTP 402). Check the Apify plan.")
    if r.status_code == 429 or r.status_code >= 500:
        raise RuntimeError(f"Apify busy or down (HTTP {r.status_code}); will retry")
    if r.status_code >= 400:
        raise PermanentError(f"Apify HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def run_actor(actor_id: str, actor_input: dict, max_charge_usd: float, memory_mb: int | None = None) -> dict:
    """Start an actor run, wait for it to finish, and return its results.

    memory_mb: some actors charge their start fee per GB of memory, and
    default to 4 GB or more. Setting 1024 keeps that fee to one unit.
    """
    params = {"maxTotalChargeUsd": max_charge_usd}
    if memory_mb:
        params["memory"] = memory_mb
    run = _check(requests.post(
        f"{API}/acts/{actor_id}/runs",
        headers=_headers(),
        params=params,
        json=actor_input,
        timeout=60,
    ))["data"]

    # Wait for the run to finish.
    waited = 0
    while run["status"] in ("READY", "RUNNING", "ABORTING", "TIMING-OUT"):
        if waited >= MAX_WAIT_SECONDS:
            requests.post(f"{API}/actor-runs/{run['id']}/abort", headers=_headers(), timeout=30)
            raise RuntimeError(f"Apify run {run['id']} took over {MAX_WAIT_SECONDS // 60} minutes; aborted")
        time.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
        run = _check(requests.get(f"{API}/actor-runs/{run['id']}", headers=_headers(), timeout=30))["data"]

    if run["status"] != "SUCCEEDED":
        reason = run.get("statusMessage") or ""
        raise RuntimeError(f"Apify run {run['id']} ended with status {run['status']}: {reason}")

    items = _check(requests.get(
        f"{API}/datasets/{run['defaultDatasetId']}/items",
        headers=_headers(), params={"clean": "true", "format": "json"}, timeout=120,
    ))
    # Re-read the run: Apify finalizes the charges a few seconds after it ends.
    run = _check(requests.get(f"{API}/actor-runs/{run['id']}", headers=_headers(), timeout=30))["data"]
    return {
        "run_id": run["id"],
        "cost_usd": _run_cost(run),
        "items": items,
    }


def _run_cost(run: dict) -> float:
    """What this run cost. Uses the larger of Apify's reported total and our
    own count (events charged x price per event), because the reported
    total can lag behind for a few seconds."""
    reported = float(run.get("usageTotalUsd") or 0)
    events = (((run.get("pricingInfo") or {}).get("pricingPerEvent") or {})
              .get("actorChargeEvents") or {})
    counted = sum(
        count * float((events.get(name) or {}).get("eventPriceUsd") or 0)
        for name, count in (run.get("chargedEventCounts") or {}).items()
    )
    return round(max(reported, counted), 5)
