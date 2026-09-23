"""Scoring step: turn each concept's weekly metrics into an opportunity score.

    opportunity = demand_breadth_weighted_velocity
                * (1 / (1 + saturation_strength * tiktok_saturation))
                * sustained_trend_factor
                * margin_factor                      # 1.0 until Phase 2
                * 100                                # to get readable numbers

where
    demand_breadth_weighted_velocity =
        (sum over sources of weight x rising demand, each capped)
        x (1 + breadth_bonus x (number of rising sources - 1))

Only rising demand counts (falling sources add 0), so a concept scores
highest when several independent sources are climbing at once, TikTok Shop
is still quiet, and the climb has lasted several weeks. A one-off Google
spike is multiplied by spike_penalty.

Missing data is treated cautiously: unknown saturation counts as
"unknown_saturation" (middling), unknown sustainedness as
"unknown_sustained". Every number here is set in config.yaml under "scoring".
"""

import json
import logging
from datetime import date

from core.config import settings
from features.run import ALL_SOURCES, week_start

log = logging.getLogger(__name__)


def opportunity(row: dict, cfg: dict) -> tuple[float, dict]:
    """Score one concept-week. Returns (score, breakdown for the dashboard)."""
    cap = cfg["velocity_cap"]
    rising = {}
    for s in ALL_SOURCES:
        v = row.get(f"velocity_{s}")
        if v is not None and v > 0:
            rising[s] = min(float(v), cap)
    base = sum(cfg["source_weights"][s] * v for s, v in rising.items())
    breadth = sum(1 for v in rising.values() if v >= settings["features"]["rising_threshold"])
    breadth_multiplier = 1 + cfg["breadth_bonus"] * max(0, breadth - 1)
    demand = base * breadth_multiplier

    sat = row.get("tiktok_saturation")
    sat = float(sat) if sat is not None else cfg["unknown_saturation"]
    saturation_factor = 1 / (1 + cfg["saturation_strength"] * sat)

    sustained = row.get("sustained_factor")
    sustained = float(sustained) if sustained is not None else cfg["unknown_sustained"]
    if row.get("spike_risk"):
        sustained *= cfg["spike_penalty"]

    margin = float(row.get("margin_factor") or 1.0)
    score = 100 * demand * saturation_factor * sustained * margin
    breakdown = {
        "rising_sources": {s: round(v, 3) for s, v in rising.items()},
        "demand": round(demand, 4), "breadth_multiplier": round(breadth_multiplier, 3),
        "saturation_used": round(sat, 3), "saturation_factor": round(saturation_factor, 3),
        "sustained_used": round(sustained, 3), "margin_factor": margin,
    }
    return round(score, 3), breakdown


def run(conn, snapshot_date: date) -> dict:
    cfg = settings["scoring"]
    week = week_start(snapshot_date)
    rows = conn.execute(
        """
        select w.* from gapfinder.concept_weekly w
        join gapfinder.concepts c on c.id = w.concept_id
        where w.week_start = %s and c.merged_into_id is null and c.review_status <> 'rejected'
        """,
        (week,),
    ).fetchall()

    scored = []
    for r in rows:
        score, breakdown = opportunity(r, cfg)
        scored.append((score, r["concept_id"], breakdown))
    scored.sort(key=lambda x: x[0], reverse=True)

    with conn.cursor() as cur:
        cur.executemany(
            """
            update gapfinder.concept_weekly
            set opportunity_score = %s, rank = %s,
                details = details || jsonb_build_object('score_breakdown', %s::jsonb)
            where concept_id = %s and week_start = %s
            """,
            [(score, i + 1, json.dumps(breakdown), concept_id, week)
             for i, (score, concept_id, breakdown) in enumerate(scored)],
        )
    log.info("Scored %d concepts for the week of %s", len(scored), week)
    return {"records": len(scored)}

