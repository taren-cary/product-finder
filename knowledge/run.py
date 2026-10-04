"""Build the knowledge base from the approved channels.

    .venv\\Scripts\\python -m knowledge.run transcripts   # free: list videos + fetch transcripts
    .venv\\Scripts\\python -m knowledge.run estimate      # free: what extraction would cost
    .venv\\Scripts\\python -m knowledge.run batch 20      # Claude Batch API (half price): send up to $20 of videos
    .venv\\Scripts\\python -m knowledge.run collect       # save finished batch results
    .venv\\Scripts\\python -m knowledge.run extract       # Claude, normal price, one video at a time
    .venv\\Scripts\\python -m knowledge.run merge         # Claude: dedupe claims into insights

Each step only works on what's new, so re-running is cheap.
"""

import logging
import sys

from core.config import settings
from core.db import connect
from core.logging_setup import setup_logging
from knowledge import extract, merge, transcripts

log = logging.getLogger(__name__)


def estimate(conn) -> dict:
    """Claude cost to extract everything transcribed but not yet extracted."""
    row = conn.execute(
        """
        select count(*) as videos, coalesce(sum(v.transcript_chars), 0) as chars
        from gapfinder.kb_videos v join gapfinder.kb_channels c using (channel_id)
        where c.status = 'approved' and v.status = 'transcribed' and v.extract_batch_id is null
        """
    ).fetchone()
    extract_cost = extract.estimated_cost(row["chars"], row["videos"])     # measured rates
    return {"videos": row["videos"], "transcript_chars": int(row["chars"]),
            "extract_usd": round(extract_cost, 2),
            "extract_batch_usd": round(extract_cost * extract.BATCH_DISCOUNT, 2)}


def main(step: str, arg: str | None = None) -> None:
    setup_logging()
    with connect() as conn:
        if step == "transcripts":
            log.info("Added %d videos from approved channels", transcripts.add_channel_videos(conn))
            print(transcripts.fetch_transcripts(conn))
            print(estimate(conn))
        elif step == "estimate":
            print(estimate(conn))
        elif step == "batch":
            print(extract.submit_batch(conn, budget_usd=float(arg or 20)))
        elif step == "collect":
            print(extract.collect_batch(conn))
        elif step == "extract":
            print(extract.run(conn, max_cost_usd=settings["knowledge"]["extract_max_cost_usd"]))
        elif step == "merge":
            print(merge.run(conn))
        else:
            raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "", sys.argv[2] if len(sys.argv) > 2 else None)
