"""Step 4: merge duplicate items into insights (Claude).

Many creators say the same thing in different words. Items are grouped by
topic and kind (tip, benchmark, tool, ...). For each group, Claude gets the new
(unmerged) items plus the insights we already have in that group, and puts
each item either under an existing insight or into a new one. Where creators
disagree, it notes the disagreement on the insight.

Hooks/scripts and case studies are never blended together: each one stays its
own insight with its exact wording (no Claude needed for those).

Groups are independent, so each round sends one request per group in a single
Batch API job (half price), waits for it, saves, and repeats until every item
is merged. Afterwards each insight's counts (how many videos and channels said
it) and newest upload date are recalculated, so well-supported advice ranks higher.
"""

import json
import logging
import time

import anthropic

from concepts.run import _cost
from core.config import settings

log = logging.getLogger(__name__)

ITEMS_PER_REQUEST = 200
KEEP_SEPARATE = {"hook or script", "case study"}   # never merged; each stays its own insight
BATCH_DISCOUNT = 0.5
POLL_SECONDS = 60
MAX_ROUNDS = 80

PROMPT = """You maintain a knowledge base of practical knowledge for a founder starting on TikTok Shop (selling, and possibly affiliate marketing). All items below share one topic and one kind.

You get EXISTING insights (with ids) and NEW items extracted from YouTube videos (with ids). Assign every new item to exactly one insight:
- If it says the same thing as an existing insight (same advice or fact, even in different words), put it under that insight_id. If the item adds useful specifics (numbers, conditions, steps, names), rewrite that insight's statement/details to include them; otherwise return an empty statement to keep the insight as it is.
- Otherwise group it with other new items that say the same thing into a NEW insight (insight_id 0).

Keep every useful specific: numbers, names, steps, conditions. Don't merge items that give different advice just because they share a subject; when in doubt, keep them separate.

statement: one or two clear sentences. details: numbers, steps and conditions (empty string if none). quote: the best exact quote from the items, if any (empty string if none). contradictions: if the items in an insight disagree (e.g. one says 20 samples, another says 100), describe the disagreement; otherwise empty string.

Return only insights that received items. Every new item id must appear exactly once."""

SCHEMA = {
    "type": "object",
    "properties": {
        "insights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "insight_id": {"type": "integer"},
                    "audience": {"type": "string", "enum": ["seller", "affiliate", "both"]},
                    "statement": {"type": "string"},
                    "details": {"type": "string"},
                    "quote": {"type": "string"},
                    "contradictions": {"type": "string"},
                    "claim_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["insight_id", "audience", "statement", "details", "quote", "contradictions",
                             "claim_ids"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["insights"],
    "additionalProperties": False,
}


def keep_separate(conn) -> int:
    """Hooks/scripts and case studies: each item becomes its own insight, as is."""
    rows = conn.execute(
        """select id, topic, kind, audience, claim, details, quote from gapfinder.kb_claims
           where insight_id is null and kind = any(%s)""", (list(KEEP_SEPARATE),)).fetchall()
    for c in rows:
        insight_id = conn.execute(
            """insert into gapfinder.kb_insights (topic, kind, audience, statement, details, quote)
               values (%s, %s, %s, %s, %s, %s) returning id""",
            (c["topic"], c["kind"], c["audience"], c["claim"], c["details"], c["quote"])).fetchone()["id"]
        conn.execute("update gapfinder.kb_claims set insight_id = %s where id = %s", (insight_id, c["id"]))
    conn.commit()
    return len(rows)


def _groups(conn) -> list[dict]:
    return conn.execute(
        """select topic, kind, count(*) as n from gapfinder.kb_claims
           where insight_id is null and not (kind = any(%s))
           group by topic, kind order by n desc""", (list(KEEP_SEPARATE),)).fetchall()


def _request(conn, topic: str, kind: str) -> tuple[list, list, dict]:
    claims = conn.execute(
        """select id, audience, claim, details, quote from gapfinder.kb_claims
           where topic = %s and kind = %s and insight_id is null order by id limit %s""",
        (topic, kind, ITEMS_PER_REQUEST)).fetchall()
    existing = conn.execute(
        """select id, audience, statement from gapfinder.kb_insights
           where topic = %s and kind = %s order by id""", (topic, kind)).fetchall()
    text = (f"Topic: {topic}\nKind: {kind}\n\nEXISTING insights:\n"
            + ("\n".join(f"[{i['id']}] ({i['audience']}) {i['statement']}" for i in existing) or "(none yet)")
            + "\n\nNEW items:\n"
            + "\n".join(f"[{c['id']}] ({c['audience']}) {c['claim']}"
                        + (f" -- {c['details']}" if c["details"] else "")
                        + (f' -- quote: "{c["quote"]}"' if c["quote"] else "") for c in claims))
    params = {
        "model": settings["knowledge"]["model"], "max_tokens": 32000, "system": PROMPT,
        "messages": [{"role": "user", "content": text}],
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}, "effort": "low"},
    }
    return claims, existing, params


def _save(conn, topic, kind, insights, existing_ids, claim_ids) -> int:
    saved = 0
    for ins in insights:
        ids = [c for c in ins["claim_ids"] if c in claim_ids]
        if not ids:
            continue
        claim_ids.difference_update(ids)
        if ins["insight_id"] in existing_ids:
            insight_id = ins["insight_id"]
            if ins["statement"]:
                conn.execute(
                    """update gapfinder.kb_insights set statement = %s, details = %s,
                       quote = coalesce(nullif(%s, ''), quote),
                       contradictions = coalesce(nullif(%s, ''), contradictions), updated_at = now()
                       where id = %s""",
                    (ins["statement"], ins["details"] or None, ins["quote"], ins["contradictions"], insight_id))
            elif ins["contradictions"]:
                conn.execute("update gapfinder.kb_insights set contradictions = %s where id = %s",
                             (ins["contradictions"], insight_id))
        else:
            insight_id = conn.execute(
                """insert into gapfinder.kb_insights (topic, kind, audience, statement, details, quote, contradictions)
                   values (%s, %s, %s, %s, %s, %s, %s) returning id""",
                (topic, kind, ins["audience"], ins["statement"], ins["details"] or None,
                 ins["quote"] or None, ins["contradictions"] or None)).fetchone()["id"]
            existing_ids.add(insight_id)
        conn.execute("update gapfinder.kb_claims set insight_id = %s where id = any(%s)", (insight_id, ids))
        saved += 1
    return saved


def _leftovers(conn, topic, kind, claims, claim_ids) -> None:
    """Items Claude skipped get their own insight, so we never loop forever."""
    left = [c for c in claims if c["id"] in claim_ids]
    _save(conn, topic, kind, [{"insight_id": 0, "audience": c["audience"], "statement": c["claim"],
                               "details": c["details"] or "", "quote": c["quote"] or "", "contradictions": "",
                               "claim_ids": [c["id"]]} for c in left], set(), claim_ids)


def refresh_counts(conn) -> None:
    conn.execute(
        """
        update gapfinder.kb_insights i set
            video_count = s.videos, channel_count = s.channels, latest_upload = s.latest
        from (
            select c.insight_id, count(distinct c.video_id) as videos,
                   count(distinct v.channel_id) as channels, max(v.upload_date) as latest
            from gapfinder.kb_claims c join gapfinder.kb_videos v using (video_id)
            where c.insight_id is not null
            group by c.insight_id
        ) s
        where s.insight_id = i.id
        """
    )
    # Insights whose items all went elsewhere.
    conn.execute("delete from gapfinder.kb_insights i where not exists "
                 "(select 1 from gapfinder.kb_claims c where c.insight_id = i.id)")
    conn.commit()


def run(conn, max_cost_usd: float | None = None) -> dict:
    """Merge everything, one Batch API round at a time (half price)."""
    client = anthropic.Anthropic()
    separate = keep_separate(conn)
    log.info("Kept %d hooks/scripts and case studies as their own insights", separate)
    total_cost, merged, round_no = 0.0, 0, 0
    while groups := _groups(conn):
        if round_no >= MAX_ROUNDS:
            log.warning("Stopped after %d rounds; run again to continue", round_no)
            break
        if max_cost_usd is not None and total_cost >= max_cost_usd:
            log.warning("Reached the $%.2f cap; the rest waits for the next run", max_cost_usd)
            break
        round_no += 1
        pending, requests = {}, []
        for n, g in enumerate(groups):
            claims, existing, params = _request(conn, g["topic"], g["kind"])
            pending[f"g{n}"] = (g["topic"], g["kind"], claims, existing)
            requests.append({"custom_id": f"g{n}", "params": params})
        batch = client.messages.batches.create(requests=requests)
        log.info("Round %d: %d groups, %d items left, batch %s", round_no, len(groups),
                 sum(g["n"] for g in groups), batch.id)
        while client.messages.batches.retrieve(batch.id).processing_status != "ended":
            time.sleep(POLL_SECONDS)
        for item in client.messages.batches.results(batch.id):
            topic, kind, claims, existing = pending[item.custom_id]
            claim_ids = {c["id"] for c in claims}
            if item.result.type == "succeeded":
                message = item.result.message
                total_cost += _cost(message.model, message.usage) * BATCH_DISCOUNT
                try:
                    if message.stop_reason in ("refusal", "max_tokens"):
                        raise RuntimeError(f"stopped early ({message.stop_reason})")
                    insights = json.loads(next(b.text for b in message.content if b.type == "text"))["insights"]
                    merged += _save(conn, topic, kind, insights, {e["id"] for e in existing}, claim_ids)
                except Exception as e:
                    log.warning("%s / %s: unusable answer (%s); items stay separate", topic, kind, e)
                _leftovers(conn, topic, kind, claims, claim_ids)
            # A failed request leaves its items unmerged; the next round tries them again.
            conn.commit()
        log.info("Round %d done (running cost $%.2f)", round_no, total_cost)
    refresh_counts(conn)
    return {"records": merged, "kept_separate": separate, "rounds": round_no, "cost_usd": round(total_cost, 4)}
