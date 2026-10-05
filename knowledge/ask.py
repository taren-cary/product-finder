"""Ask the knowledge base a question (Claude).

1. Find the most relevant insights with Postgres full-text search, favoring
   advice that many videos/channels agree on.
2. Claude answers using only those insights, citing them as [1], [2], ...
3. The videos behind the cited insights are returned as sources, linked to
   the moment the advice is given.
"""

import json
import logging

import anthropic

from concepts.run import _cost
from core.config import settings
from core.db import connect

log = logging.getLogger(__name__)

MAX_INSIGHTS = 40

PROMPT = """You are an advisor for a founder starting out on TikTok Shop (as a seller, and possibly as an affiliate). Answer the question using ONLY the numbered insights below, which were collected from YouTube creators who teach TikTok Shop.

- Be practical and direct: give steps, numbers and specifics where the insights have them.
- When insights include exact words (hooks, scripts, messages), quote them; the wording is often the most useful part.
- Cite insights like [3] after the sentences that use them.
- Mention when creators disagree, and when advice is backed by many creators (the "said by" counts).
- Advice ages fast on TikTok Shop; prefer newer insights when they conflict.
- If the insights don't answer the question, say so plainly instead of guessing."""


def _search(conn, question: str, audience: str | None) -> list[dict]:
    # Match ANY of the question's words, ranked by relevance x how widely it's said.
    return conn.execute(
        """
        with q as (
            select nullif(replace(plainto_tsquery('english', %(q)s)::text, '&', '|'), '')::tsquery as query
        )
        select i.id, i.topic, i.kind, i.audience, i.statement, i.details, i.quote, i.contradictions,
               i.video_count, i.channel_count, i.latest_upload
        from gapfinder.kb_insights i, q
        where q.query is not null and i.search_all @@ q.query
          and (%(aud)s::text is null or i.audience in (%(aud)s, 'both'))
        order by ts_rank(i.search_all, q.query) * ln(2 + i.channel_count) desc
        limit %(n)s
        """,
        {"q": question, "aud": audience, "n": MAX_INSIGHTS},
    ).fetchall()


def _sources(conn, insight_ids: list[int]) -> list[dict]:
    rows = conn.execute(
        """
        select distinct on (v.video_id) v.video_id, v.title, ch.name as channel, c.start_s
        from gapfinder.kb_claims c
        join gapfinder.kb_videos v using (video_id)
        join gapfinder.kb_channels ch on ch.channel_id = v.channel_id
        where c.insight_id = any(%s)
        order by v.video_id, c.start_s
        """,
        (insight_ids,),
    ).fetchall()
    return [{"title": r["title"], "channel": r["channel"],
             "url": f"https://www.youtube.com/watch?v={r['video_id']}&t={r['start_s'] or 0}s"} for r in rows]


def answer(question: str, audience: str | None = None) -> dict:
    with connect() as conn:
        insights = _search(conn, question, audience)
        if not insights:
            return {"answer": "Nothing in the knowledge base matches that question yet. Try different words.",
                    "insights": [], "sources": [], "cost_usd": 0.0}
        numbered = "\n".join(
            f"[{n}] ({i['kind']}; {i['topic']}; for {i['audience']}; said by {i['channel_count']} channel(s); "
            f"newest {i['latest_upload']}) {i['statement']}"
            + (f" Details: {i['details']}" if i["details"] else "")
            + (f' Exact words: "{i["quote"]}"' if i["quote"] else "")
            + (f" Disagreement: {i['contradictions']}" if i["contradictions"] else "")
            for n, i in enumerate(insights, 1))
        response = anthropic.Anthropic().messages.create(
            model=settings["knowledge"]["model"], max_tokens=4000, system=PROMPT,
            messages=[{"role": "user", "content": f"Insights:\n{numbered}\n\nQuestion: {question}"}],
            output_config={"effort": "low"},
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        # Sources: only the insights the answer actually cited.
        cited = [insights[n - 1]["id"] for n in range(1, len(insights) + 1) if f"[{n}]" in text]
        return {"answer": text, "insights": insights,
                "sources": _sources(conn, cited or [i["id"] for i in insights]),
                "cost_usd": _cost(response.model, response.usage)}


if __name__ == "__main__":
    import sys
    result = answer(" ".join(sys.argv[1:]))
    print(result["answer"])
    print(json.dumps(result["sources"], indent=1))
