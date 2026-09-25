"""Scoring step: turn each TikTok-fit concept's weekly metrics into an opportunity score.

TikTok leads; everything else confirms:

    opportunity = (tiktok_demand + outside_demand)
                * confirmation_multiplier
                * (1 / (1 + saturation_strength * tiktok_saturation))   # headroom on TikTok Shop
                * sustained_trend_factor
                * margin_factor                                        # profit per unit (config.yaml "pricing")
                * market_size_factor                                   # < 1 only for tiny markets
                * 100                                                  # readable numbers

where
    tiktok_demand   = sum of tiktok_weights x rising TikTok growth
                      (TikTok Shop revenue, shoppable-video views, hashtag views)
    outside_demand  = sum of confirmation_weights x rising Google / Amazon / Reddit growth
                      (small weights: they support, they don't lead)
    confirmation_multiplier = 1 + confirmation_bonus x (number of outside sources rising)

Only rising growth counts (falling adds 0). Growth is capped at velocity_cap
and counted on a log scale (ln(1 + growth)), so +300% ranks below +3,000% but a
single huge number can't swamp everything else. So the top
of the list is: climbing on TikTok, TikTok Shop still open, confirmed by
Google/Amazon/Reddit, and a steady climb rather than a spike. A product
rising on Amazon or Google but not yet on TikTok still scores, lower, as an
"arbitrage" candidate.

Only TikTok-fit concepts are scored. Concepts with no data get no score (not 0).
Every number here is in config.yaml under "scoring".
"""

import json
import logging
import math
from datetime import date

from core.config import settings
from features.run import OUTSIDE_SOURCES, TIKTOK_SOURCES, week_start

log = logging.getLogger(__name__)


def opportunity(row: dict, cfg: dict) -> tuple[float, dict]:
    """Score one concept-week. Returns (score, breakdown for the dashboard)."""
    cap = cfg["velocity_cap"]
    threshold = settings["features"]["rising_threshold"]

    def scaled(v: float) -> float:
        v = min(v, cap)
        return math.log1p(v) if cfg.get("growth_scale") == "log" else v

    def rising(sources):
        out = {}
        for s in sources:
            v = row.get(f"velocity_{s}")
            if v is not None and float(v) > 0:
                out[s] = scaled(float(v))
        return out

    tiktok_rising = rising(TIKTOK_SOURCES)
    outside_rising = rising(OUTSIDE_SOURCES)
    tiktok_demand = sum(cfg["tiktok_weights"][s] * v for s, v in tiktok_rising.items())
    outside_demand = sum(cfg["confirmation_weights"][s] * v for s, v in outside_rising.items())
    confirmations = sum(1 for v in outside_rising.values() if v >= scaled(threshold))
    confirmation = 1 + cfg["confirmation_bonus"] * confirmations

    sat = row.get("tiktok_saturation")
    sat = float(sat) if sat is not None else cfg["unknown_saturation"]
    headroom = 1 / (1 + cfg["saturation_strength"] * sat)

    sustained = row.get("sustained_factor")
    sustained = float(sustained) if sustained is not None else cfg["unknown_sustained"]
    if row.get("spike_risk"):
        sustained *= cfg["spike_penalty"]

    margin = float(row.get("margin_factor") or 1.0)

    # Too-small markets: scale the score down in proportion (unchecked = no change).
    market = 1.0
    revenue, floor = row.get("shop_revenue_7d"), cfg.get("min_market_revenue_7d") or 0
    if floor and revenue is not None and float(revenue) < floor:
        market = max(float(revenue), 0.0) / floor

    score = 100 * (tiktok_demand + outside_demand) * confirmation * headroom * sustained * margin * market
    breakdown = {
        "tiktok_rising": {s: round(v, 3) for s, v in tiktok_rising.items()},
        "outside_rising": {s: round(v, 3) for s, v in outside_rising.items()},
        "tiktok_demand": round(tiktok_demand, 4), "outside_demand": round(outside_demand, 4),
        "confirmations": confirmations, "confirmation_multiplier": round(confirmation, 3),
        "saturation_used": round(sat, 3), "headroom_factor": round(headroom, 3),
        "sustained_used": round(sustained, 3), "margin_factor": margin,
        "market_size_factor": round(market, 3),
    }
    return round(score, 3), breakdown


def run(conn, snapshot_date: date) -> dict:
    cfg = settings["scoring"]
    week = week_start(snapshot_date)
    rows = conn.execute(
        """
        select w.* from gapfinder.concept_weekly w
        join gapfinder.concepts c on c.id = w.concept_id
        where w.week_start = %s and c.merged_into_id is null
          and c.review_status <> 'rejected' and c.tiktok_fit is true
        """,
        (week,),
    ).fetchall()

    scored, unmeasured = [], []
    for r in rows:
        # A concept nobody has looked up yet has no score (not a score of 0).
        has_data = (any(r.get(f"velocity_{s}") is not None for s in TIKTOK_SOURCES + OUTSIDE_SOURCES)
                    or r.get("tiktok_saturation") is not None
                    or ((r.get("details") or {}).get("google") or {}).get("points", 0) > 0)
        if not has_data:
            unmeasured.append(r["concept_id"])
            continue
        score, breakdown = opportunity(r, cfg)
        scored.append((score, r["concept_id"], breakdown))
    scored.sort(key=lambda x: x[0], reverse=True)

    with conn.cursor() as cur:
        cur.execute(
            """
            update gapfinder.concept_weekly
            set opportunity_score = null, rank = null, details = details - 'score_breakdown'
            where week_start = %s and concept_id = any(%s)
            """,
            (week, unmeasured),
        )
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
    log.info("Scored %d concepts for the week of %s (%d not looked up yet)", len(scored), week, len(unmeasured))
    return {"records": len(scored)}
