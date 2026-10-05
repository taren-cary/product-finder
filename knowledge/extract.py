"""Step 3: pull every concrete piece of advice out of each transcript (Claude).

For each transcribed video, Claude reads the transcript (with timestamps) and
returns a list of claims: one tip, rule, number, warning or step each, with
its topic, whether it's for sellers or affiliates, and the moment in the
video it comes from. Fluff, hype and self-promotion are skipped.

Long transcripts are split into parts. Model and topics: config.yaml "knowledge".

Two ways to run it:
  run()                     one request at a time, answers right away
  submit_batch() + collect_batch()
                            Anthropic's Batch API: half price, answers within
                            a few hours (usually much sooner)
"""

import json
import logging

import anthropic

from concepts.run import _cost
from core.config import settings
from core.steps import StepStopped

log = logging.getLogger(__name__)

CHARS_PER_PART = 40_000   # ~10k tokens (~10 minutes of talking) per request, so nothing gets skimmed

# Measured with the comprehensive instructions (version 2) on 4 videos
# (2026-10-05), normal price: about 1.5 cents per video plus $2.40 per million
# transcript characters. The Batch API charges half.
USD_PER_VIDEO = 0.015
USD_PER_CHAR = 2.4e-6
BATCH_DISCOUNT = 0.5


def estimated_cost(transcript_chars: int, videos: int = 1) -> float:
    return videos * USD_PER_VIDEO + (transcript_chars or 0) * USD_PER_CHAR

# Bump when the instructions change; each item and video records which version made it.
PROMPT_VERSION = 2

PROMPT = """You are building a comprehensive knowledge base for a founder who is about to start selling on TikTok Shop (and may also do TikTok Shop affiliate marketing). Your job is to capture EVERYTHING in this YouTube video that could possibly be valuable to them. Missing something useful is much worse than including something minor.

Go through the transcript from start to finish and record every item of value, including:
- tips, tactics, rules of thumb, and step-by-step processes (keep every step)
- TikTok Shop rules, policies, fees, requirements and account-health issues
- numbers and benchmarks: commission rates, conversion rates, prices, margins, costs, posting volume, sample counts, timelines, revenue figures, follower counts, with their context
- hooks, scripts, captions, video structures, phrases and outreach messages: put the speaker's EXACT words in quote
- case studies and real examples: who did what, with which product/brand, and what result
- specific products, brands, niches, suppliers, agencies and tools mentioned, and what was said about them
- warnings, mistakes, things that stopped working, scams and myths
- strategies, frameworks and ways of thinking about the business
- trends and predictions about where TikTok Shop is heading
- opinions and debates, especially where the speaker disagrees with common advice
- general e-commerce, marketing, branding, sourcing, money or business advice that a TikTok Shop seller could use, even if TikTok Shop isn't mentioned

Rules:
- One item per distinct piece of knowledge. Don't merge separate points into one item; don't split one point into many.
- Make each item self-contained and specific: someone reading it alone should understand and be able to use it. Include names, numbers, and conditions.
- Use quote for the speaker's exact words whenever the wording itself is useful (hooks, scripts, memorable rules, claims with numbers). Otherwise leave quote empty.
- Only skip things with no value at all: greetings, sponsor reads, requests to like/subscribe, pitches for the creator's own course, and small talk with no information in it.
- If the video truly contains nothing of value, return an empty list.

For each item:
- kind: one of the kinds listed below.
- topic: one of the topics listed below (pick the closest).
- audience: "seller", "affiliate", or "both".
- claim: the knowledge, as one or two clear sentences.
- details: extra context, numbers, steps or conditions (empty string if none).
- quote: the speaker's exact words when useful (empty string if not).
- start_s: the timestamp (in seconds) where it's said, from the [seconds] markers."""

SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "topic": {"type": "string"},
                    "audience": {"type": "string", "enum": ["seller", "affiliate", "both"]},
                    "claim": {"type": "string"},
                    "details": {"type": "string"},
                    "quote": {"type": "string"},
                    "start_s": {"type": "integer"},
                },
                "required": ["kind", "topic", "audience", "claim", "details", "quote", "start_s"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["claims"],
    "additionalProperties": False,
}

VIDEOS_SQL = """
    select v.video_id, v.title, v.upload_date, v.transcript, v.transcript_chars, c.name as channel
    from gapfinder.kb_videos v join gapfinder.kb_channels c on c.channel_id = v.channel_id
    where c.status = 'approved' and v.status = 'transcribed' and v.extract_batch_id is null
    order by v.view_count desc nulls last
"""


def _parts(segments: list[dict]) -> list[str]:
    """Transcript as '[seconds] text' lines, ~30s per line, split into parts."""
    lines, cur, cur_start = [], [], None
    for s in segments:
        if cur_start is None:
            cur_start = s["start"]
        cur.append(s["text"].replace("\n", " "))
        if s["start"] - cur_start >= 30:
            lines.append(f"[{cur_start}] " + " ".join(cur))
            cur, cur_start = [], None
    if cur:
        lines.append(f"[{cur_start}] " + " ".join(cur))
    parts, buf = [], ""
    for line in lines:
        if len(buf) + len(line) > CHARS_PER_PART and buf:
            parts.append(buf)
            buf = ""
        buf += line + "\n"
    if buf:
        parts.append(buf)
    return parts


def _system() -> str:
    return PROMPT + "\n\nTopics:\n" + "\n".join(f"- {t}" for t in settings["knowledge"]["topics"])


def _schema() -> dict:
    """SCHEMA, with kind and topic limited to the lists in config.yaml."""
    cfg = settings["knowledge"]
    item = dict(SCHEMA["properties"]["claims"]["items"])
    item["properties"] = {**item["properties"],
                          "kind": {"type": "string", "enum": cfg["kinds"]},
                          "topic": {"type": "string", "enum": cfg["topics"]}}
    return {**SCHEMA, "properties": {"claims": {"type": "array", "items": item}}}


def _params(v: dict, part: str, system: str) -> dict:
    return {
        "model": settings["knowledge"]["model"], "max_tokens": 20000, "system": system,
        "messages": [{"role": "user", "content":
                      f"Video: {v['title']}\nChannel: {v['channel']}\nUploaded: {v['upload_date']}\n\n"
                      f"Transcript:\n{part}"}],
        "output_config": {"format": {"type": "json_schema", "schema": _schema()}, "effort": "medium"},
    }


def _claims_from(message) -> list[dict]:
    if message.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"stopped early ({message.stop_reason})")
    return json.loads(next(b.text for b in message.content if b.type == "text"))["claims"]


def _save_claims(conn, video_id: str, claims: list[dict]) -> None:
    topics, kinds = settings["knowledge"]["topics"], settings["knowledge"]["kinds"]
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into gapfinder.kb_claims
                (video_id, kind, topic, audience, claim, details, quote, start_s, prompt_version)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [(video_id, c["kind"] if c["kind"] in kinds else "tip",
              c["topic"] if c["topic"] in topics else "other", c["audience"],
              c["claim"][:2000], c["details"][:4000] or None, c["quote"][:4000] or None, c["start_s"],
              PROMPT_VERSION) for c in claims],
        )
    conn.execute("""update gapfinder.kb_videos set status = 'extracted', extracted_at = now(),
                    extract_batch_id = null, prompt_version = %s where video_id = %s""",
                 (PROMPT_VERSION, video_id))


def redo_old_versions(conn) -> int:
    """Queue videos extracted with older instructions to be extracted again
    (their old items are deleted). Run only when we've decided to pay for it."""
    rows = conn.execute(
        """update gapfinder.kb_videos set status = 'transcribed', extracted_at = null
           where status = 'extracted' and coalesce(prompt_version, 1) < %s returning video_id""",
        (PROMPT_VERSION,)).fetchall()
    ids = [r["video_id"] for r in rows]
    conn.execute("delete from gapfinder.kb_claims where video_id = any(%s)", (ids,))
    conn.commit()
    return len(ids)


# --- One request at a time (normal price) -------------------------------------

def run(conn, limit: int | None = None, max_cost_usd: float | None = None) -> dict:
    system = _system()
    videos = conn.execute(VIDEOS_SQL + (f" limit {int(limit)}" if limit else "")).fetchall()
    client = anthropic.Anthropic()
    total_cost, done, claims_saved = 0.0, 0, 0
    for v in videos:
        if max_cost_usd is not None and total_cost >= max_cost_usd:
            log.warning("Reached the $%.2f cap; the rest waits for the next run", max_cost_usd)
            break
        claims = []
        try:
            for part in _parts(v["transcript"] or []):
                response = client.messages.create(**_params(v, part, system))
                total_cost += _cost(response.model, response.usage)
                claims += _claims_from(response)
        except anthropic.BadRequestError as e:
            if "credit balance" in str(e).lower():
                raise StepStopped("Anthropic account is out of credit; add credit at console.anthropic.com",
                                  total_cost, done)
            log.warning("%s: extraction failed: %s", v["title"][:60], e)
            continue
        except Exception as e:
            log.warning("%s: extraction failed: %s", v["title"][:60], e)
            continue
        _save_claims(conn, v["video_id"], claims)
        conn.commit()
        done += 1
        claims_saved += len(claims)
        log.info("%3d claims  %s (running cost $%.2f)", len(claims), v["title"][:70], total_cost)
    return {"records": claims_saved, "videos": done, "cost_usd": round(total_cost, 4)}


# --- Batch API (half price) ---------------------------------------------------

def submit_batch(conn, budget_usd: float) -> dict:
    """Send the most-viewed unprocessed videos in one batch, as many as fit
    the budget (estimated from transcript length)."""
    system = _system()
    requests, video_ids, estimate = [], [], 0.0
    for v in conn.execute(VIDEOS_SQL).fetchall():
        cost = estimated_cost(v["transcript_chars"]) * BATCH_DISCOUNT
        if estimate + cost > budget_usd:
            break
        estimate += cost
        video_ids.append(v["video_id"])
        for n, part in enumerate(_parts(v["transcript"] or [])):
            # custom_id: the video id plus which part of the transcript ("-p0", "-p1", ...).
            requests.append({"custom_id": f"{v['video_id']}-p{n}", "params": _params(v, part, system)})
    if not requests:
        return {"videos": 0, "estimated_usd": 0.0}
    batch = anthropic.Anthropic().messages.batches.create(requests=requests)
    conn.execute("update gapfinder.kb_videos set extract_batch_id = %s where video_id = any(%s)",
                 (batch.id, video_ids))
    conn.commit()
    log.info("Sent batch %s: %d videos, %d requests, estimated $%.2f", batch.id, len(video_ids),
             len(requests), estimate)
    return {"batch_id": batch.id, "videos": len(video_ids), "estimated_usd": round(estimate, 2)}


def collect_batch(conn) -> dict:
    """Save the results of any finished batches. Videos whose request failed
    are released so the next batch picks them up again."""
    client = anthropic.Anthropic()
    batch_ids = [r["extract_batch_id"] for r in conn.execute(
        "select distinct extract_batch_id from gapfinder.kb_videos where extract_batch_id is not null")]
    summary = {"finished": 0, "still_running": 0, "videos": 0, "claims": 0, "retry_later": 0, "cost_usd": 0.0}
    for batch_id in batch_ids:
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status != "ended":
            summary["still_running"] += 1
            log.info("Batch %s still running: %s", batch_id, batch.request_counts)
            continue
        claims, failed = {}, set()
        for item in client.messages.batches.results(batch_id):
            video_id = item.custom_id.rsplit("-p", 1)[0]
            if item.result.type != "succeeded":
                failed.add(video_id)
                continue
            message = item.result.message
            summary["cost_usd"] += _cost(message.model, message.usage) * BATCH_DISCOUNT
            try:
                claims.setdefault(video_id, []).extend(_claims_from(message))
            except Exception as e:
                log.warning("%s: unusable answer: %s", video_id, e)
                failed.add(video_id)
        for video_id, video_claims in claims.items():
            if video_id not in failed:
                _save_claims(conn, video_id, video_claims)
                summary["videos"] += 1
                summary["claims"] += len(video_claims)
        # Anything not saved (failed, or missing from the results) goes back in the queue.
        released = conn.execute(
            "update gapfinder.kb_videos set extract_batch_id = null where extract_batch_id = %s", (batch_id,))
        summary["retry_later"] += released.rowcount or 0
        conn.commit()
        summary["finished"] += 1
    summary["cost_usd"] = round(summary["cost_usd"], 4)
    log.info("Batch results: %s", summary)
    return summary
