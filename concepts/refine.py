"""Keyword refinement step: narrow search keywords that are too broad.

When a concept's TikTok Shop keyword search returns Kalodata's maximum of 100
products, the keyword is measuring a whole category ("bronzer", "lace bra")
rather than the specific product, so its saturation numbers are unreliable.
This step asks Claude for a narrower keyword that still names the concept's
product type ("cream bronzer stick"), using the concept and the top products
the broad search found.

Each concept is narrowed at most once. The new keyword becomes the concept's
main keyword and gets looked up in the next TikTok checks; until then, the
old keyword's data keeps being used.
"""

import json
import logging

import anthropic

from collectors.keywords import concept_keyword
from concepts.run import _cost
from core.config import settings
from core.steps import StepStopped

log = logging.getLogger(__name__)

BATCH = 50

PROMPT = """You refine search keywords for a TikTok Shop product research tool.

Each concept below is a specific product type, but its current keyword is too broad: searching TikTok Shop for it returns more than 100 products, so it measures a whole category instead of this product.

For each concept, give a narrower keyword that:
- still names this concept's product type (not a brand, color or size),
- is what a shopper would actually type, 2 to 5 words,
- would mostly return products like this concept and fewer unrelated ones.
Use the concept name, category and the example products the broad search found to judge what to add (a material, a form, a use, a key feature).
If the concept itself is genuinely broad and can't be narrowed without changing what it is, return the current keyword unchanged.
Return one result for every concept, using its concept_id."""

SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"concept_id": {"type": "integer"}, "keyword": {"type": "string"}},
                "required": ["concept_id", "keyword"],
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
        select c.id, c.name, c.category, c.keywords, r.payload
        from gapfinder.concepts c
        join lateral (
            select payload from gapfinder.raw_responses
            where source = 'kalodata_keywords'
              and request_key = 'products:' || lower(coalesce(c.keywords[1], c.name))
            order by snapshot_date desc limit 1
        ) r on true
        where c.tiktok_fit is true and c.merged_into_id is null and c.review_status <> 'rejected'
          and c.keyword_refined_at is null
          and jsonb_array_length(r.payload->'data') >= 100
        """
    ).fetchall()
    if not concepts:
        log.info("No broad keywords to narrow")
        return {"records": 0, "cost_usd": 0}
    log.info("Narrowing %d broad keywords", len(concepts))

    client = anthropic.Anthropic()
    total_cost, refined = 0.0, 0
    for start in range(0, len(concepts), BATCH):
        batch = concepts[start:start + BATCH]
        lines = "\n".join(json.dumps({
            "concept_id": c["id"], "concept": c["name"], "category": c["category"],
            "current_keyword": concept_keyword(c),
            "example_products_found": [(p.get("product_name") or "")[:80]
                                       for p in (c["payload"].get("data") or [])[:5]],
        }, ensure_ascii=False) for c in batch)
        request = dict(
            model=cfg["model"], max_tokens=16000, system=PROMPT,
            messages=[{"role": "user", "content": "Concepts (one JSON object per line):\n" + lines}],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
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
                                  total_cost, refined)
            log.warning("Refinement batch failed, will retry next run: %s", e)
            continue
        except Exception as e:
            log.warning("Refinement batch failed, will retry next run: %s", e)
            continue

        total_cost += _cost(response.model, response.usage)
        if response.stop_reason in ("refusal", "max_tokens"):
            log.warning("Refinement batch stopped early (%s)", response.stop_reason)
            continue
        by_id = {c["id"]: c for c in batch}
        for r in json.loads(next(b.text for b in response.content if b.type == "text"))["results"]:
            c = by_id.get(r["concept_id"])
            if not c:
                continue
            old = concept_keyword(c)
            new = " ".join(r["keyword"].lower().split())
            keywords = [new] + [k for k in (c["keywords"] or []) if " ".join(k.lower().split()) != new]
            conn.execute(
                """
                update gapfinder.concepts
                set keywords = %s, keyword_before_refinement = %s, keyword_refined_at = now(), updated_at = now()
                where id = %s
                """,
                (keywords[:4], old, c["id"]),
            )
            if new != old:
                refined += 1
                log.info("  %s: '%s' -> '%s'", c["name"], old, new)
        conn.commit()

    return {"records": refined, "cost_usd": round(total_cost, 4)}
