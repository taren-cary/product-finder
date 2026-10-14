"""Gift step: would this concept make a good gift, and can it be sold on video
without showing a face?

Runs after the TikTok-fit step. Every TikTok-fit concept without a gift
verdict is sent to Claude (100 per request) with a few example titles and its
typical price. Claude answers:

  giftable   someone would buy it for someone else (girlfriend, mom, sister,
             coworker), at a gift price, and it looks good unwrapped
  easy_demo  it sells on video with only hands and the product (unboxing,
             close-ups, a mirror lighting up), no person on camera needed

The scoring step gives concepts that are both a boost (config.yaml,
scoring.gift_bonus). A verdict set by hand from the dashboard is never
overwritten. The first run labels every existing concept (~$2 one-time);
after that only new concepts are sent.
"""

import json
import logging

import anthropic

from concepts.run import _cost
from core.config import settings
from core.steps import StepStopped

log = logging.getLogger(__name__)

BATCH = 100

GIFT_PROMPT = """You screen product concepts for a founder selling physical products in an online store, mostly as gifts, with traffic from short videos (TikTok, Reels, Shorts). The founder is a man selling largely women's products, so videos can't rely on a woman using the product on camera.

For each concept decide:

giftable: would people commonly buy this FOR SOMEONE ELSE as a present?
- Yes: feels special or indulgent, looks good unwrapped, typical gift price (about $20 to $80), suits a girlfriend, wife, mom, sister, friend or coworker without knowing exact sizes or shades. Examples: LED makeup mirror, heated eyelash curler, silk pillowcase set, jewelry box, hair tool set, cozy robe, personalized items, gift sets.
- No: bought only for yourself or as a chore (cleaning supplies, refills, replacement parts), needs exact fit or shade (bras, foundation, jeans), too cheap to feel like a gift, or awkward to give (shapewear, hair removal, acne products).

easy_demo: can a video sell it showing only hands and the product, no face or body on camera?
- Yes: the product itself is the show. It lights up, transforms, organizes, has a satisfying unboxing, a visible before/after on an object or mannequin, or an obvious "wow" in close-up.
- No: it only makes sense worn or used on a person (clothing fit, makeup on skin, skincare results, shapewear).

reason: at most 12 words.
Return one result for every concept, using its concept_id."""

GIFT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "concept_id": {"type": "integer"},
                    "giftable": {"type": "boolean"},
                    "easy_demo": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["concept_id", "giftable", "easy_demo", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["results"],
    "additionalProperties": False,
}


def run(conn, snapshot_date) -> dict:
    cfg = settings["concepts"]
    gcfg = settings["gift"]
    concepts = conn.execute(
        """
        select c.id, c.name, c.category, c.audience,
               (array_agg(i.title order by i.last_seen desc))[1:3] as examples,
               w.typical_price
        from gapfinder.concepts c
        left join gapfinder.item_concept_map m on m.concept_id = c.id
        left join gapfinder.items i on i.id = m.item_id
        left join lateral (
            select typical_price from gapfinder.concept_weekly
            where concept_id = c.id order by week_start desc limit 1
        ) w on true
        where c.merged_into_id is null and c.tiktok_fit is true
          and c.review_status <> 'rejected' and c.giftable is null
        group by c.id, w.typical_price
        order by c.id
        limit %s
        """,
        (gcfg["max_concepts_per_run"],),
    ).fetchall()
    if not concepts:
        log.info("Every concept already has a gift verdict")
        return {"records": 0, "cost_usd": 0}
    log.info("Judging gift fit for %d concepts", len(concepts))

    client = anthropic.Anthropic()
    total_cost, judged = 0.0, 0
    for start in range(0, len(concepts), BATCH):
        if total_cost >= gcfg["max_cost_per_run_usd"]:
            log.warning("Reached the $%.2f cap for this run; the rest waits for the next run",
                        gcfg["max_cost_per_run_usd"])
            break
        batch = concepts[start:start + BATCH]
        lines = "\n".join(json.dumps({
            "concept_id": c["id"], "name": c["name"], "category": c["category"], "for": c["audience"],
            "typical_price": round(float(c["typical_price"]), 2) if c["typical_price"] is not None else None,
            "example_titles": [t[:90] for t in (c["examples"] or []) if t],
        }, ensure_ascii=False) for c in batch)
        request = dict(
            model=cfg["model"], max_tokens=16000, system=GIFT_PROMPT,
            messages=[{"role": "user", "content": "Concepts (one JSON object per line):\n" + lines}],
            output_config={"format": {"type": "json_schema", "schema": GIFT_SCHEMA}},
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
            log.warning("Gift batch failed, will retry next run: %s", e)
            continue
        except Exception as e:
            log.warning("Gift batch failed, will retry next run: %s", e)
            continue

        total_cost += _cost(response.model, response.usage)
        if response.stop_reason in ("refusal", "max_tokens"):
            log.warning("Gift batch stopped early (%s); will retry next run", response.stop_reason)
            continue
        text = next(b.text for b in response.content if b.type == "text")
        ids = {c["id"] for c in batch}
        rows = [{"giftable": r["giftable"], "easy_demo": r["easy_demo"], "reason": r["reason"][:200],
                 "id": r["concept_id"]}
                for r in json.loads(text)["results"] if r["concept_id"] in ids]
        with conn.cursor() as cur:
            # "giftable is null" keeps a verdict set by hand from being overwritten.
            cur.executemany(
                """
                update gapfinder.concepts
                set giftable = %(giftable)s, easy_demo = %(easy_demo)s,
                    gift_reason = %(reason)s, gift_by = 'claude'
                where id = %(id)s and giftable is null
                """,
                rows,
            )
        conn.commit()
        judged += len(rows)
        log.info("Judged %d/%d concepts (running cost $%.3f)", judged, len(concepts), total_cost)

    return {"records": judged, "cost_usd": round(total_cost, 4)}
