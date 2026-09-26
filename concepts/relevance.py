"""Relevance filter: keep only the TikTok Shop products that really are the concept.

A keyword search for "crevice cleaning brush" also returns a window blinds
brush; "vacuum wine stopper" also returns electric wine openers. Counting
those inflates seller counts, prices and payouts. After the TikTok checks,
this step sends each concept's search results to Claude (product titles and
prices, numbered) and asks which ones are NOT this product.

Each product is judged once per concept (gapfinder.concept_product_matches)
and reused every week after, so later runs only send new products. Features
and the dashboard ignore products marked not relevant. Products not judged
yet still count, so nothing breaks while the filter catches up.

Settings are in config.yaml under "relevance".
"""

import json
import logging
from datetime import timedelta

import anthropic

from collectors.keywords import merge_keywords
from concepts.run import _cost
from core.config import settings
from core.steps import StepStopped

log = logging.getLogger(__name__)

PROMPT = """You check TikTok Shop search results for a product research tool.

You get one product concept and a numbered list of TikTok Shop products that a keyword search returned for it. Some results are the concept's product; others only share words with it.

List the numbers of the products that are NOT this product:
- a different product type (a window blinds brush when the concept is a crevice cleaning brush; an electric wine opener when the concept is a vacuum wine stopper),
- an accessory, refill or spare part for it rather than the product itself,
- a bundle or set where this product is only a minor add-on.
Count as matching: the same product type in any brand, size, color, material or pack count, and bundles where it is the main item.
When unsure, count it as matching.
Return an empty list if every product matches."""

SCHEMA = {
    "type": "object",
    "properties": {"not_matching": {"type": "array", "items": {"type": "integer"}}},
    "required": ["not_matching"],
    "additionalProperties": False,
}


def run(conn, snapshot_date) -> dict:
    rcfg, ccfg = settings["relevance"], settings["concepts"]
    since = snapshot_date - timedelta(weeks=settings["features"]["tiktok_max_age_weeks"])

    concepts = conn.execute(
        """
        select c.id, c.name, c.category, c.description, c.keywords, c.keyword_before_refinement,
               c.review_status, w.opportunity_score
        from gapfinder.concepts c
        left join lateral (
            select opportunity_score from gapfinder.concept_weekly
            where concept_id = c.id order by week_start desc limit 1
        ) w on true
        where c.tiktok_fit is true and c.merged_into_id is null and c.review_status <> 'rejected'
        order by (c.review_status = 'shortlisted') desc, w.opportunity_score desc nulls last
        """
    ).fetchall()
    judged = {(r["concept_id"], r["product_id"]) for r in conn.execute(
        "select concept_id, product_id from gapfinder.concept_product_matches").fetchall()}

    client = anthropic.Anthropic()
    total_cost, done, checked = 0.0, 0, 0
    for c in concepts:
        if done >= rcfg["max_concepts_per_run"]:
            break
        if total_cost >= rcfg["max_cost_per_run_usd"]:
            log.warning("Reached the $%.2f cap for this run; the rest waits for the next run",
                        rcfg["max_cost_per_run_usd"])
            break
        products = _unjudged_products(conn, c, since, judged)
        if not products:
            continue
        done += 1
        try:
            not_matching, cost = _judge(client, ccfg, c, products)
        except anthropic.BadRequestError as e:
            if "credit balance" in str(e).lower():
                raise StepStopped("Anthropic account is out of credit; add credit at console.anthropic.com",
                                  total_cost, checked)
            log.warning("%s: relevance check failed, will retry next run: %s", c["name"], e)
            continue
        except Exception as e:
            log.warning("%s: relevance check failed, will retry next run: %s", c["name"], e)
            continue
        total_cost += cost
        with conn.cursor() as cur:
            cur.executemany(
                """
                insert into gapfinder.concept_product_matches (concept_id, product_id, relevant, title)
                values (%s, %s, %s, %s)
                on conflict (concept_id, product_id) do nothing   -- a manual verdict wins
                """,
                [(c["id"], p["product_id"], (i + 1) not in not_matching, (p.get("product_name") or "")[:300])
                 for i, p in enumerate(products)],
            )
        conn.commit()
        checked += len(products)
        log.info("%s: %d of %d products are something else (running cost $%.3f)",
                 c["name"], len(not_matching), len(products), total_cost)

    log.info("Checked %d products for %d concepts", checked, done)
    return {"records": checked, "cost_usd": round(total_cost, 4)}


def _unjudged_products(conn, concept, since, judged) -> list[dict]:
    """This concept's latest search results (all its keywords), not judged yet.
    Includes the broad keyword it was narrowed from: until the narrower one is
    searched, the metrics fall back to that older search, so it's filtered too."""
    keywords = merge_keywords(concept)
    if concept.get("keyword_before_refinement"):
        keywords.append(concept["keyword_before_refinement"])
    keys = [f"products:{k}" for k in keywords]
    rows = conn.execute(
        """
        select distinct on (request_key) payload
        from gapfinder.raw_responses
        where source = 'kalodata_keywords' and request_key = any(%s) and snapshot_date >= %s
        order by request_key, snapshot_date desc
        """,
        (keys, since),
    ).fetchall()
    out, seen = [], set()
    for r in rows:
        for p in r["payload"].get("data") or []:
            pid = str(p.get("product_id") or "")
            if pid and pid not in seen and (concept["id"], pid) not in judged:
                seen.add(pid)
                out.append({**p, "product_id": pid})
    return out


def _judge(client, cfg, concept, products):
    lines = "\n".join(
        f"{i + 1}. {(p.get('product_name') or '')[:110]} (${p.get('unit_price')})"
        for i, p in enumerate(products))
    content = (f"Concept: {concept['name']}\nCategory: {concept['category'] or ''}\n"
               f"Description: {concept['description'] or ''}\n\nSearch results:\n{lines}")
    request = dict(
        model=cfg["model"], max_tokens=4000, system=PROMPT,
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
    )
    if cfg["model"].startswith("claude-haiku"):
        response = client.messages.create(**request)
    else:
        request["output_config"]["effort"] = cfg["effort"]
        response = client.beta.messages.create(
            **request, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    cost = _cost(response.model, response.usage)
    if response.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"stopped early ({response.stop_reason})")
    text = next(b.text for b in response.content if b.type == "text")
    return set(json.loads(text)["not_matching"]), cost
