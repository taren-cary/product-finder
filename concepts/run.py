"""Concept step: group items from every source into product concepts, using Claude.

The same product shows up under different names everywhere ("heatless
curling rod", "overnight curl ribbon", "satin curler"). This step sends new,
unclassified items to Claude in batches together with the existing concepts,
and Claude decides for each item:
  * is it a sellable physical product at all? (Reddit posts and search terms
    often aren't), and if so
  * which existing concept it belongs to, or what new concept to create.

Every item is classified once; the result is saved (item_concept_map, and
items.is_product / classified_at) and never re-sent. Manual merges and
splits from the dashboard (Milestone 4) take priority over Claude's choice.

Settings (model, batch size, spending cap) are in config.yaml under "concepts".
"""

import json
import logging

import anthropic

from core.config import settings

log = logging.getLogger(__name__)

# USD per million tokens (input, output). Cache writes cost 1.25x input,
# cache reads 0.1x input.
PRICES = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

SYSTEM_PROMPT = """You organize product listings, Reddit posts and search terms into product concepts, for a founder looking for physical products to sell on TikTok Shop.

A product concept is a generic product type that a new seller could source from a factory and sell under their own brand, e.g. "heatless hair curler", "sunset projection lamp", "hypochlorous acid body spray".
Naming rules for concepts:
- Name the product type, never the brand, color, size, scent, pack count or model number. "Owala FreeSip 32 oz, Very Very Dark" -> "insulated water bottle with straw lid".
- Specific enough to search for and source: "heatless hair curler", not "hair accessory"; "clumping cat litter", not "pet supplies".
- Broad enough that close variants land together: a "heatless curling rod" and a "satin heatless curler ribbon" are both "heatless hair curler".
- Lowercase, 2 to 6 words.

For each item:
1. is_product: true if the item is, or clearly centers on, one physical product type.
   false for: discussions or questions that don't center on a specific product type, services, digital goods, media (books, music, movies, video games), gift cards, vehicles, real estate, and any excluded type listed below.
   A Reddit post counts as the product it is about; if it's about no product, false.
   A search term counts only if it names a product type someone would buy.
2. If is_product is true:
   - If one of the existing concepts is the same product type, set existing_concept_id to its id and leave the new_concept_* fields empty.
   - Otherwise set existing_concept_id to 0 and fill in the new concept: name (rules above); category as "Top level > Sub level" (e.g. "Beauty > Hair tools", "Home > Lighting", "Pets > Cat supplies"); description as one short sentence; keywords as 1 to 3 search terms a shopper would type, most common first.
   - Within one batch, use the exact same new concept name for items that are the same product type.
3. short_description: 3 to 10 neutral words describing the item itself without its brand, e.g. "satin heatless curling rod with scrunchies". Empty string if is_product is false.
If is_product is false, set existing_concept_id to 0 and leave every other field empty.
Return one result for every item, using its item_id."""

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer"},
                    "is_product": {"type": "boolean"},
                    "short_description": {"type": "string"},
                    "existing_concept_id": {"type": "integer"},
                    "new_concept_name": {"type": "string"},
                    "new_concept_category": {"type": "string"},
                    "new_concept_description": {"type": "string"},
                    "new_concept_keywords": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["item_id", "is_product", "short_description", "existing_concept_id",
                             "new_concept_name", "new_concept_category",
                             "new_concept_description", "new_concept_keywords"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["results"],
    "additionalProperties": False,
}

MAX_BODY_CHARS = 600   # enough of a Reddit post to see what it's about


def run(conn, snapshot_date) -> dict:
    cfg = settings["concepts"]
    items = conn.execute(
        """
        select i.id, i.source, i.title, i.category, i.body_text, s.price
        from gapfinder.items i
        left join lateral (
            select price from gapfinder.item_snapshots
            where item_id = i.id order by snapshot_date desc limit 1
        ) s on true
        where i.classified_at is null
        order by i.last_seen desc, i.id
        limit %s
        """,
        (cfg["max_items_per_run"],),
    ).fetchall()
    if not items:
        log.info("No new items to classify")
        return {"records": 0, "cost_usd": 0}
    log.info("Classifying %d new items with %s", len(items), cfg["model"])

    client = anthropic.Anthropic()
    total_cost = 0.0
    classified = 0
    size = cfg["batch_size"]
    for start in range(0, len(items), size):
        if total_cost >= cfg["max_cost_per_run_usd"]:
            log.warning("Reached the $%.2f cap for this run; the rest waits for the next run",
                        cfg["max_cost_per_run_usd"])
            break
        batch = items[start:start + size]
        try:
            results, cost = _classify_batch(client, conn, cfg, batch)
        except Exception as e:
            log.warning("Batch of %d items failed, will retry next run: %s", len(batch), e)
            continue
        total_cost += cost
        classified += _save_results(conn, cfg["model"], batch, results)
        conn.commit()
        log.info("Batch %d: %d/%d items classified (running cost $%.3f)",
                 start // size + 1, classified, len(items), total_cost)

    return {"records": classified, "cost_usd": round(total_cost, 4)}


# --- Talking to Claude ------------------------------------------------------

def _candidate_concepts(conn, cfg, batch) -> list[dict]:
    """Existing concepts to show Claude. All of them while the list is small;
    once it's large, the ones whose names best match the batch's titles."""
    total = conn.execute(
        "select count(*) as n from gapfinder.concepts where merged_into_id is null"
    ).fetchone()["n"]
    if total <= cfg["max_concepts_in_prompt"]:
        return conn.execute(
            "select id, name, category from gapfinder.concepts where merged_into_id is null order by id"
        ).fetchall()
    titles = [f"{r['title']} {r['category'] or ''}".lower() for r in batch]
    return conn.execute(
        """
        select distinct c.id, c.name, c.category
        from unnest(%s::text[]) as t(title)
        cross join lateral (
            select id, name, category from gapfinder.concepts
            where merged_into_id is null
            order by extensions.word_similarity(name, t.title) desc
            limit 10
        ) c
        order by c.id
        """,
        (titles,),
    ).fetchall()


def _classify_batch(client, conn, cfg, batch):
    concepts = _candidate_concepts(conn, cfg, batch)
    concept_lines = "\n".join(f"{c['id']} | {c['name']} | {c['category'] or ''}" for c in concepts)
    item_lines = "\n".join(json.dumps({
        "item_id": r["id"],
        "source": r["source"],
        "title": r["title"],
        "category": r["category"],
        "text": (r["body_text"] or "")[:MAX_BODY_CHARS] or None,
        "price": float(r["price"]) if r["price"] is not None else None,
    }, ensure_ascii=False) for r in batch)

    excluded = settings.get("excluded_product_types") or []
    system = SYSTEM_PROMPT + "\n\nExcluded product types: " + (", ".join(excluded) or "none yet")

    request = dict(
        model=cfg["model"],
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": [
            # The concept list comes first and is marked for caching, so later
            # batches in the same run can reuse it at a tenth of the price.
            {"type": "text",
             "text": "Existing concepts (id | name | category):\n" + (concept_lines or "(none yet)"),
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "Items to classify (one JSON object per line):\n" + item_lines},
        ]}],
        output_config={"format": {"type": "json_schema", "schema": RESULT_SCHEMA}},
    )
    if cfg["model"].startswith("claude-haiku"):
        response = client.messages.create(**request)
    else:
        request["output_config"]["effort"] = cfg["effort"]
        # If Claude's safety filter ever declines a batch, let Anthropic re-run it
        # on its recommended fallback model instead of failing.
        response = client.beta.messages.create(
            **request, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )

    cost = _cost(response.model, response.usage)
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined this batch")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("response was cut off (max_tokens); try a smaller batch_size")
    text = next(b.text for b in response.content if b.type == "text")
    results = json.loads(text)["results"]
    return {"results": results, "shown_ids": {c["id"] for c in concepts}}, cost


def _cost(model: str, usage) -> float:
    price_in, price_out = next((p for m, p in PRICES.items() if model.startswith(m)), PRICES["claude-opus-5"])
    tokens_in = (usage.input_tokens
                 + 1.25 * (usage.cache_creation_input_tokens or 0)
                 + 0.10 * (usage.cache_read_input_tokens or 0))
    return (tokens_in * price_in + usage.output_tokens * price_out) / 1_000_000


# --- Saving the answers -----------------------------------------------------

def _save_results(conn, model: str, batch, answer) -> int:
    batch_ids = {r["id"] for r in batch}
    saved = 0
    for r in answer["results"]:
        item_id = r["item_id"]
        if item_id not in batch_ids:
            continue   # an id we didn't send; ignore it
        batch_ids.discard(item_id)

        if not r["is_product"]:
            conn.execute(
                "update gapfinder.items set is_product = false, classified_at = now() where id = %s",
                (item_id,),
            )
            saved += 1
            continue

        concept_id = r["existing_concept_id"] if r["existing_concept_id"] in answer["shown_ids"] else None
        if concept_id is None:
            concept_id = _get_or_create_concept(conn, r)
        if concept_id is None:
            log.warning("Item %s: no usable concept in Claude's answer; will retry next run", item_id)
            continue

        conn.execute(
            """
            insert into gapfinder.item_concept_map (item_id, concept_id, assigned_by, model)
            values (%s, %s, 'claude', %s)
            on conflict (item_id) do nothing
            """,
            (item_id, concept_id, model),
        )
        conn.execute(
            """
            update gapfinder.items
            set is_product = true, classified_at = now(), normalized_description = %s
            where id = %s
            """,
            (r["short_description"] or None, item_id),
        )
        saved += 1

    if batch_ids:
        log.warning("%d items got no answer; they'll be retried next run", len(batch_ids))
    return saved


def _get_or_create_concept(conn, r) -> int | None:
    name = " ".join(r["new_concept_name"].lower().split())
    if not name:
        return None
    keywords = [" ".join(k.lower().split()) for k in r["new_concept_keywords"] if k.strip()][:3]
    row = conn.execute(
        """
        insert into gapfinder.concepts (name, category, description, keywords)
        values (%s, %s, %s, %s)
        on conflict (name) do update set updated_at = now()
        returning id, merged_into_id
        """,
        (name, r["new_concept_category"] or None, r["new_concept_description"] or None, keywords or [name]),
    ).fetchone()
    # If that name belongs to a concept that was merged into another, use the survivor.
    concept_id, merged_into = row["id"], row["merged_into_id"]
    seen = set()
    while merged_into and merged_into not in seen:
        seen.add(merged_into)
        concept_id = merged_into
        merged_into = conn.execute(
            "select merged_into_id from gapfinder.concepts where id = %s", (concept_id,)
        ).fetchone()["merged_into_id"]
    return concept_id
