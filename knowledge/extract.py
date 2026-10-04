"""Step 3: pull every concrete piece of advice out of each transcript (Claude).

For each transcribed video, Claude reads the transcript (with timestamps) and
returns a list of claims: one tip, rule, number, warning or step each, with
its topic, whether it's for sellers or affiliates, and the moment in the
video it comes from. Fluff, hype and self-promotion are skipped.

Long transcripts are split into parts. Model and topics: config.yaml "knowledge".
"""

import json
import logging

import anthropic

from concepts.run import _cost
from core.config import settings
from core.steps import StepStopped

log = logging.getLogger(__name__)

CHARS_PER_PART = 90_000   # ~22k tokens of transcript per request

PROMPT = """You extract practical knowledge from YouTube videos about TikTok Shop, for a founder who will sell on TikTok Shop (and may also do affiliate marketing).

From the transcript, list every concrete, usable piece of advice: a tip, a step, a rule or policy, a number or benchmark, a tool, a warning, or a mistake to avoid. Each claim should be self-contained and specific enough to act on ("Send free samples to 50-100 creators in your first two weeks; expect about 1 in 10 to post", not "samples are important").

Skip: greetings, hype, income screenshots without a method, requests to like/subscribe, course or coaching pitches, and anything not about TikTok Shop selling, affiliate marketing or the products/content around them.

For each claim:
- topic: one of the topics listed below (pick the closest).
- audience: "seller", "affiliate", or "both".
- claim: one sentence, specific.
- details: optional extra context, numbers or conditions (empty string if none).
- start_s: the timestamp (in seconds) where it's said, from the [seconds] markers.

If the video contains no usable advice, return an empty list."""

SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "audience": {"type": "string", "enum": ["seller", "affiliate", "both"]},
                    "claim": {"type": "string"},
                    "details": {"type": "string"},
                    "start_s": {"type": "integer"},
                },
                "required": ["topic", "audience", "claim", "details", "start_s"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["claims"],
    "additionalProperties": False,
}


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


def run(conn, limit: int | None = None, max_cost_usd: float | None = None) -> dict:
    cfg = settings["knowledge"]
    topics = cfg["topics"]
    system = PROMPT + "\n\nTopics:\n" + "\n".join(f"- {t}" for t in topics)
    videos = conn.execute(
        """
        select v.video_id, v.title, v.upload_date, v.transcript, c.name as channel
        from gapfinder.kb_videos v join gapfinder.kb_channels c on c.channel_id = v.channel_id
        where c.status = 'approved' and v.status = 'transcribed'
        order by v.view_count desc nulls last
        """ + (f" limit {int(limit)}" if limit else "")
    ).fetchall()
    client = anthropic.Anthropic()
    total_cost, done, claims_saved = 0.0, 0, 0
    for v in videos:
        if max_cost_usd is not None and total_cost >= max_cost_usd:
            log.warning("Reached the $%.2f cap; the rest waits for the next run", max_cost_usd)
            break
        claims = []
        try:
            for part in _parts(v["transcript"] or []):
                response = client.messages.create(
                    model=cfg["model"], max_tokens=16000, system=system,
                    messages=[{"role": "user", "content":
                               f"Video: {v['title']}\nChannel: {v['channel']}\nUploaded: {v['upload_date']}\n\n"
                               f"Transcript:\n{part}"}],
                    output_config={"format": {"type": "json_schema", "schema": SCHEMA}, "effort": "low"},
                )
                total_cost += _cost(response.model, response.usage)
                if response.stop_reason in ("refusal", "max_tokens"):
                    raise RuntimeError(f"stopped early ({response.stop_reason})")
                claims += json.loads(next(b.text for b in response.content if b.type == "text"))["claims"]
        except anthropic.BadRequestError as e:
            if "credit balance" in str(e).lower():
                raise StepStopped("Anthropic account is out of credit; add credit at console.anthropic.com",
                                  total_cost, done)
            log.warning("%s: extraction failed: %s", v["title"][:60], e)
            continue
        except Exception as e:
            log.warning("%s: extraction failed: %s", v["title"][:60], e)
            continue

        with conn.cursor() as cur:
            cur.executemany(
                """
                insert into gapfinder.kb_claims (video_id, topic, audience, claim, details, start_s)
                values (%s, %s, %s, %s, %s, %s)
                """,
                [(v["video_id"], c["topic"] if c["topic"] in topics else "other", c["audience"],
                  c["claim"][:1000], c["details"][:2000] or None, c["start_s"]) for c in claims],
            )
        conn.execute("update gapfinder.kb_videos set status = 'extracted', extracted_at = now() where video_id = %s",
                     (v["video_id"],))
        conn.commit()
        done += 1
        claims_saved += len(claims)
        log.info("%3d claims  %s (running cost $%.2f)", len(claims), v["title"][:70], total_cost)
    return {"records": claims_saved, "videos": done, "cost_usd": round(total_cost, 4)}
