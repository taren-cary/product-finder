"""Build the knowledge base from the approved channels.

    .venv\\Scripts\\python -m knowledge.run transcripts   # free: list videos + fetch transcripts
    .venv\\Scripts\\python -m knowledge.run extract       # Claude: pull claims from transcripts
    .venv\\Scripts\\python -m knowledge.run merge         # Claude: dedupe claims into insights
    .venv\\Scripts\\python -m knowledge.run estimate      # free: what extraction would cost

Each step only works on what's new, so re-running is cheap.
"""

import logging
import sys

from concepts.run import PRICES
from core.config import settings
from core.db import connect
from core.logging_setup import setup_logging
from knowledge import extract, merge, transcripts

log = logging.getLogger(__name__)


def estimate(conn) -> dict:
    """Rough Claude cost to extract + merge everything transcribed but not yet extracted."""
    row = conn.execute(
        """
        select count(*) as videos, coalesce(sum(v.transcript_chars), 0) as chars
        from gapfinder.kb_videos v join gapfinder.kb_channels c using (channel_id)
        where c.status = 'approved' and v.status = 'transcribed'
        """
    ).fetchone()
    price_in, price_out = PRICES[settings["knowledge"]["model"]]
    tokens_in = row["chars"] / 4 * 1.15 + row["videos"] * 900       # transcript + timestamps + prompt
    tokens_out = row["videos"] * 2500                                 # ~40 claims per video
    extract_cost = (tokens_in * price_in + tokens_out * price_out) / 1e6
    return {"videos": row["videos"], "transcript_chars": int(row["chars"]),
            "extract_usd": round(extract_cost, 2), "merge_usd": round(extract_cost * 0.35, 2)}


def main(step: str) -> None:
    setup_logging()
    with connect() as conn:
        if step == "transcripts":
            log.info("Added %d videos from approved channels", transcripts.add_channel_videos(conn))
            print(transcripts.fetch_transcripts(conn))
            print(estimate(conn))
        elif step == "estimate":
            print(estimate(conn))
        elif step == "extract":
            print(extract.run(conn, max_cost_usd=settings["knowledge"]["extract_max_cost_usd"]))
        elif step == "merge":
            print(merge.run(conn))
        else:
            raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
