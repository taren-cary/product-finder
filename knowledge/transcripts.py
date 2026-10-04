"""Step 2: collect videos from approved channels and fetch their transcripts (free).

For every APPROVED channel:
  * add every video it uploaded in the past year (max_age_days)
  * look up each video's upload date and skip anything older than that
  * download the English captions with timestamps

Nothing here uses Claude. YouTube sometimes blocks lots of rapid requests;
we pause between videos and stop for the day if it starts blocking, then
pick up where we left off on the next run.
"""

import logging
import time
from datetime import date

from psycopg.types.json import Jsonb
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (CouldNotRetrieveTranscript, IpBlocked,
                                            NoTranscriptFound, RequestBlocked, TranscriptsDisabled)

from knowledge.channels import channel_videos, oldest_upload_allowed, upload_date

log = logging.getLogger(__name__)
PAUSE_S = 1.5


def add_channel_videos(conn) -> int:
    """Add every past-year video from approved channels' uploads."""
    channels = conn.execute(
        "select channel_id, name from gapfinder.kb_channels where status = 'approved'").fetchall()
    added = 0
    for ch in channels:
        try:
            videos = channel_videos(ch["channel_id"])
        except Exception as e:
            log.warning("%s: couldn't list uploads: %s", ch["name"], e)
            continue
        with conn.cursor() as cur:
            cur.executemany(
                """
                insert into gapfinder.kb_videos (video_id, channel_id, title, duration_s, view_count, found_by)
                values (%s, %s, %s, %s, %s, 'channel')
                on conflict (video_id) do nothing
                """,
                [(v["id"], ch["channel_id"], v["title"], int(v["duration"] or 0), v["view_count"]) for v in videos],
            )
            added += cur.rowcount or 0
        conn.commit()
        log.info("%s: %d videos in the past year", ch["name"], len(videos))
    return added


def fetch_transcripts(conn, limit: int | None = None) -> dict:
    rows = conn.execute(
        """
        select v.video_id, v.title from gapfinder.kb_videos v
        join gapfinder.kb_channels c on c.channel_id = v.channel_id
        where c.status = 'approved' and v.status = 'found'
        order by v.view_count desc nulls last
        """ + (f" limit {int(limit)}" if limit else "")
    ).fetchall()
    api = YouTubeTranscriptApi()
    oldest = oldest_upload_allowed()
    done = {"transcribed": 0, "no_transcript": 0, "too_old": 0, "failed": 0}
    for i, r in enumerate(rows):
        vid = r["video_id"]
        try:
            uploaded = upload_date(vid)
            if uploaded and uploaded < oldest:
                conn.execute("update gapfinder.kb_videos set status = 'failed', upload_date = %s, "
                             "error = 'older than the age limit' where video_id = %s", (uploaded, vid))
                done["too_old"] += 1
            else:
                fetched = api.fetch(vid, languages=["en", "en-US", "en-GB"])
                segments = [{"start": round(s.start), "text": s.text} for s in fetched.snippets]
                text_len = sum(len(s["text"]) for s in segments)
                conn.execute(
                    """
                    update gapfinder.kb_videos
                    set status = 'transcribed', transcript = %s, transcript_chars = %s, upload_date = %s, error = null
                    where video_id = %s
                    """,
                    (Jsonb(segments), text_len, uploaded, vid),
                )
                done["transcribed"] += 1
        except (IpBlocked, RequestBlocked) as e:
            log.warning("YouTube is blocking transcript requests; stopping for now (%s)", type(e).__name__)
            conn.commit()
            break
        except (NoTranscriptFound, TranscriptsDisabled):
            conn.execute("update gapfinder.kb_videos set status = 'no_transcript' where video_id = %s", (vid,))
            done["no_transcript"] += 1
        except (CouldNotRetrieveTranscript, Exception) as e:
            conn.execute("update gapfinder.kb_videos set status = 'failed', error = %s where video_id = %s",
                         (f"{type(e).__name__}: {str(e)[:300]}", vid))
            done["failed"] += 1
        conn.commit()
        if (i + 1) % 20 == 0:
            log.info("Transcripts: %d/%d (%s)", i + 1, len(rows), done)
        time.sleep(PAUSE_S)
    log.info("Transcripts done: %s", done)
    return done
