"""TikTok-fit step: would this product concept sell on TikTok Shop?

Runs right after the concept step. Every concept not judged yet is sent to
Claude (100 per request), with a few example titles, its typical price and
which sources it came from. Claude answers fit / not fit with a short reason.

Concepts judged "not for TikTok" stay in the database (and can be flipped
back from the dashboard) but are never looked up on paid sources, scored or
ranked. A verdict set by hand from the dashboard is never overwritten.

In the same request Claude also labels who the product is for (women, men,
kids, pets, everyone), for the dashboard's audience filter. Concepts judged
before this existed get labeled only when config.yaml's
concepts.audience_backfill is true (a one-time pass).
"""

import json
import logging

import anthropic

from concepts.run import _cost
from core.config import settings
from core.steps import StepStopped

log = logging.getLogger(__name__)

BATCH = 100

FIT_PROMPT = """You screen product concepts for a founder who sells physical products on TikTok Shop (US).

For each concept decide tiktok_fit: could a new seller realistically sell it on TikTok Shop through short videos and creators?

Signs of a good fit:
- Easy to show on video: a visible result, a before/after, a satisfying or surprising demo, or it solves an everyday annoyance.
- An impulse purchase: typically about $10 to $80.
- Small and easy to ship; nothing bulky, fragile or perishable.
- Buyers don't care much about the brand, so a new seller can compete.
- Already selling on TikTok Shop (source "kalodata") is strong evidence of fit.

Not a fit:
- Replenished staples bought on price or loyalty to a brand: pet food, cat litter, groceries, paper towels, printer paper and office basics, batteries, generic vitamins.
- Products that live or die on a famous brand or license: branded makeup lines, licensed collectibles, trading cards, name-brand electronics.
- Big-ticket, bulky, technical or regulated items: appliances, furniture, tools needing installation, vehicles, medical devices, supplements making health claims.
- Anything in the excluded list below.

Be decisive. When a concept is borderline, lean toward fit only if you can picture a creator making a video that sells it.
reason: at most 12 words.

Also give audience, who the product is for:
- "women": made for women, or bought mostly by women (women's clothing, bras, makeup, hair tools, satin pillowcases, pregnancy products).
- "men": made for men, or bought mostly by men.
- "kids": for babies or children.
- "pets": for pets.
- "everyone": general products bought by anyone (most home, kitchen, car and tech items).
Return one result for every concept, using its concept_id."""

FIT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "concept_id": {"type": "integer"},
                    "tiktok_fit": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "audience": {"type": "string", "enum": ["women", "men", "kids", "pets", "everyone"]},
                },
                "required": ["concept_id", "tiktok_fit", "reason", "audience"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["results"],
    "additionalProperties": False,
}


def run(conn, snapshot_date) -> dict:
    cfg = settings["concepts"]
    concepts = conn.execute(
        """
        select c.id, c.name, c.category,
               array_agg(distinct i.source) filter (where i.source is not null) as sources,
               (array_agg(i.title order by i.last_seen desc))[1:3] as examples,
               percentile_cont(0.5) within group (order by s.price) as typical_price
        from gapfinder.concepts c
        left join gapfinder.item_concept_map m on m.concept_id = c.id
        left join gapfinder.items i on i.id = m.item_id
        left join lateral (
            select price from gapfinder.item_snapshots
            where item_id = i.id order by snapshot_date desc limit 1
        ) s on true
        where c.merged_into_id is null
          and (c.tiktok_fit is null
               or (%s and c.tiktok_fit is true and c.audience is null))
        group by c.id
        order by c.id
        """,
        (bool(cfg.get("audience_backfill")),),
    ).fetchall()
    if not concepts:
        log.info("Every concept already has a TikTok-fit verdict")
        return {"records": 0, "cost_usd": 0}
    log.info("Judging TikTok fit for %d concepts", len(concepts))

    excluded = settings.get("excluded_product_types") or []
    system = FIT_PROMPT + "\n\nExcluded product types: " + (", ".join(excluded) or "none yet")
    client = anthropic.Anthropic()
    total_cost, judged = 0.0, 0

    for start in range(0, len(concepts), BATCH):
        batch = concepts[start:start + BATCH]
        lines = "\n".join(json.dumps({
            "concept_id": c["id"], "name": c["name"], "category": c["category"],
            "sources": c["sources"] or [],
            "typical_price": round(float(c["typical_price"]), 2) if c["typical_price"] is not None else None,
            "example_titles": [t[:90] for t in (c["examples"] or []) if t],
        }, ensure_ascii=False) for c in batch)
        request = dict(
            model=cfg["model"], max_tokens=16000, system=system,
            messages=[{"role": "user", "content": "Concepts (one JSON object per line):\n" + lines}],
            output_config={"format": {"type": "json_schema", "schema": FIT_SCHEMA}},
        )
        try:
            if cfg["model"].startswith("claude-haiku"):
                response = client.messages.create(**request)
            else:
                request["output_config"]["effort"] = cfg["effort"]
                response = client.beta.messages.create(
                    **request, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        except anthropic.BadRequestError as e:
            if "credit balance" in str(e).lower():
                raise StepStopped("Anthropic account is out of credit; add credit at console.anthropic.com",
                                  total_cost, judged)
            log.warning("Fit batch failed, will retry next run: %s", e)
            continue
        except Exception as e:
            log.warning("Fit batch failed, will retry next run: %s", e)
            continue

        total_cost += _cost(response.model, response.usage)
        if response.stop_reason in ("refusal", "max_tokens"):
            log.warning("Fit batch stopped early (%s); will retry next run", response.stop_reason)
            continue
        text = next(b.text for b in response.content if b.type == "text")
        ids = {c["id"] for c in batch}
        rows = [(r["tiktok_fit"], r["reason"][:200], r["audience"], r["concept_id"])
                for r in json.loads(text)["results"] if r["concept_id"] in ids]
        with conn.cursor() as cur:
            # Only fill in what's missing: an existing verdict or audience
            # (including anything set by hand) is never overwritten.
            cur.executemany(
                """
                update gapfinder.concepts set
                    tiktok_fit_reason = case when tiktok_fit is null then %(reason)s else tiktok_fit_reason end,
                    tiktok_fit_by     = case when tiktok_fit is null then 'claude' else tiktok_fit_by end,
                    tiktok_fit        = coalesce(tiktok_fit, %(fit)s),
                    audience_by       = case when audience is null then 'claude' else audience_by end,
                    audience          = coalesce(audience, %(audience)s)
                where id = %(id)s
                """,
                [{"fit": f, "reason": why, "audience": aud, "id": cid} for f, why, aud, cid in rows],
            )
        conn.commit()
        judged += len(rows)
        log.info("Judged %d/%d concepts (running cost $%.3f)", judged, len(concepts), total_cost)

    return {"records": judged, "cost_usd": round(total_cost, 4)}
