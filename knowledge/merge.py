"""Step 4: merge duplicate claims into insights (Claude).

Many creators say the same thing in different words. Topic by topic, Claude
gets the new (unmerged) claims plus the insights we already have for that
topic, and puts each claim either under an existing insight or into a new
one. Where creators disagree, it notes the disagreement on the insight.

Afterwards each insight's counts (how many videos and channels said it) and
newest upload date are recalculated, so well-supported advice ranks higher.
"""

import json
import logging

import anthropic

from concepts.run import _cost
from core.config import settings
from core.steps import StepStopped

log = logging.getLogger(__name__)

CLAIMS_PER_REQUEST = 250

PROMPT = """You maintain a knowledge base of practical advice about selling and affiliate marketing on TikTok Shop. All items below are about one topic.

You get EXISTING insights (with ids) and NEW claims extracted from YouTube videos (with ids). Assign every new claim to exactly one insight:
- If it says the same thing as an existing insight (same advice, even in different words), put it under that insight_id. You may rewrite that insight's statement/details to include useful specifics from the claim.
- Otherwise group it with other new claims that say the same thing into a NEW insight (insight_id 0).

Each insight is one specific, actionable piece of advice written as a clear sentence; put numbers, conditions and steps in details. Don't merge claims that give different advice just because they share a subject.

If claims in the same insight disagree (e.g. one says 20 samples, another says 100), keep the insight and describe the disagreement in contradictions; otherwise contradictions is an empty string.

Return only insights that received claims. Every new claim id must appear exactly once."""

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
                    "contradictions": {"type": "string"},
                    "claim_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["insight_id", "audience", "statement", "details", "contradictions", "claim_ids"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["insights"],
    "additionalProperties": False,
}


def _ask(client, model, existing, claims) -> tuple[list, float]:
    text = ("EXISTING insights:\n"
            + ("\n".join(f"[{i['id']}] ({i['audience']}) {i['statement']}" for i in existing) or "(none yet)")
            + "\n\nNEW claims:\n"
            + "\n".join(f"[{c['id']}] ({c['audience']}) {c['claim']}"
                        + (f" -- {c['details']}" if c["details"] else "") for c in claims))
    response = client.messages.create(
        model=model, max_tokens=32000, system=PROMPT,
        messages=[{"role": "user", "content": text}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}, "effort": "low"},
    )
    cost = _cost(response.model, response.usage)
    if response.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"stopped early ({response.stop_reason})")
    return json.loads(next(b.text for b in response.content if b.type == "text"))["insights"], cost


def _save(conn, topic, insights, existing_ids, claim_ids) -> int:
    saved = 0
    for ins in insights:
        ids = [c for c in ins["claim_ids"] if c in claim_ids]
        if not ids:
            continue
        claim_ids.difference_update(ids)
        if ins["insight_id"] in existing_ids:
            insight_id = ins["insight_id"]
            conn.execute(
                """update gapfinder.kb_insights set statement = %s, details = %s, contradictions = %s,
                   updated_at = now() where id = %s""",
                (ins["statement"], ins["details"] or None, ins["contradictions"] or None, insight_id))
        else:
            insight_id = conn.execute(
                """insert into gapfinder.kb_insights (topic, audience, statement, details, contradictions)
                   values (%s, %s, %s, %s, %s) returning id""",
                (topic, ins["audience"], ins["statement"], ins["details"] or None,
                 ins["contradictions"] or None)).fetchone()["id"]
            existing_ids.add(insight_id)
        conn.execute("update gapfinder.kb_claims set insight_id = %s where id = any(%s)", (insight_id, ids))
        saved += 1
    return saved


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
    # Insights whose claims all went elsewhere.
    conn.execute("delete from gapfinder.kb_insights i where not exists "
                 "(select 1 from gapfinder.kb_claims c where c.insight_id = i.id)")


def run(conn, max_cost_usd: float | None = None) -> dict:
    model = settings["knowledge"]["model"]
    client = anthropic.Anthropic()
    total_cost, merged = 0.0, 0
    topics = [r["topic"] for r in conn.execute(
        "select topic from gapfinder.kb_claims where insight_id is null group by topic order by count(*) desc")]
    for topic in topics:
        while True:
            if max_cost_usd is not None and total_cost >= max_cost_usd:
                log.warning("Reached the $%.2f cap; the rest waits for the next run", max_cost_usd)
                refresh_counts(conn)
                conn.commit()
                return {"records": merged, "cost_usd": round(total_cost, 4)}
            claims = conn.execute(
                """select id, audience, claim, details from gapfinder.kb_claims
                   where topic = %s and insight_id is null order by id limit %s""",
                (topic, CLAIMS_PER_REQUEST)).fetchall()
            if not claims:
                break
            existing = conn.execute(
                "select id, audience, statement from gapfinder.kb_insights where topic = %s order by id",
                (topic,)).fetchall()
            try:
                insights, cost = _ask(client, model, existing, claims)
            except anthropic.BadRequestError as e:
                if "credit balance" in str(e).lower():
                    raise StepStopped("Anthropic account is out of credit; add credit at console.anthropic.com",
                                      total_cost, merged)
                raise
            total_cost += cost
            claim_ids = {c["id"] for c in claims}
            merged += _save(conn, topic, insights, {e["id"] for e in existing}, claim_ids)
            if claim_ids:
                # Claude skipped some claims; give each its own insight so we never loop forever.
                left = [c for c in claims if c["id"] in claim_ids]
                merged += _save(conn, topic, [{"insight_id": 0, "audience": c["audience"], "statement": c["claim"],
                                              "details": c["details"] or "", "contradictions": "",
                                              "claim_ids": [c["id"]]} for c in left], set(), claim_ids)
            conn.commit()
            log.info("%-35s %3d claims merged (running cost $%.2f)", topic, len(claims), total_cost)
    refresh_counts(conn)
    conn.commit()
    return {"records": merged, "cost_usd": round(total_cost, 4)}
