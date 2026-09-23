"""Reddit collector: product mentions with buy-intent (official API, free).

Once per run, for each subreddit in config.yaml, we search recent posts for
buy-intent phrases ("game changer", "worth every penny", "where can I
buy"...) and save the matching posts. People raving about or hunting for a
product on Reddit is often an early demand signal.

Needs a Reddit "script" app: REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET and
REDDIT_USER_AGENT in .env. Reddit's free API allows about 100 requests a
minute; we use one request per subreddit.
"""

import time

import praw

from collectors.base import BaseCollector
from core.config import require_env, settings

POST_FIELDS = ["id", "title", "selftext", "score", "num_comments", "created_utc",
               "permalink", "url", "upvote_ratio"]
MAX_TEXT_CHARS = 2000   # keep long posts from bloating the database


class RedditCollector(BaseCollector):
    name = "reddit"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cfg = settings["reddit"]
        self.reddit = praw.Reddit(
            client_id=require_env("REDDIT_CLIENT_ID"),
            client_secret=require_env("REDDIT_CLIENT_SECRET"),
            user_agent=require_env("REDDIT_USER_AGENT"),
            check_for_async=False,
        )
        self.reddit.read_only = True

    def collect(self) -> int:
        # One search per subreddit: all phrases joined with OR.
        query = " OR ".join(f'"{p}"' for p in self.cfg["buy_intent_phrases"])
        total = 0
        for sub in self.cfg["subreddits"]:
            try:
                payload = self.fetch_cached(f"search:{sub}", lambda: self._search(sub, query))
            except Exception as e:
                self.log.warning("r/%s failed, skipping: %s", sub, e)
                continue
            total += len(payload["posts"])
            self.log.info("r/%s: %d matching posts", sub, len(payload["posts"]))
        return total

    def _search(self, sub: str, query: str) -> dict:
        time.sleep(1)  # be polite to the API
        posts = []
        for post in self.reddit.subreddit(sub).search(
            query, sort="new", time_filter=self.cfg["time_filter"], limit=self.cfg["max_posts_per_subreddit"]
        ):
            row = {f: getattr(post, f, None) for f in POST_FIELDS}
            row["selftext"] = (row["selftext"] or "")[:MAX_TEXT_CHARS]
            posts.append(row)
        return {"subreddit": sub, "query": query, "posts": posts}
