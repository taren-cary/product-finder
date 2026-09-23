"""Retry a flaky call (network hiccup, rate limit) with increasing waits."""

import logging
import time

from core.config import settings

log = logging.getLogger(__name__)


class PermanentError(Exception):
    """An error that retrying won't fix (bad request, wrong API key...).
    Raise this from a fetch function to skip the retries."""


def with_retries(fn, description: str = "request"):
    """Call fn(). If it raises, wait and try again: 2s, 4s, 8s...

    Gives up after the number of attempts in config.yaml and re-raises
    the last error, so the caller can log it and move on.
    PermanentError is re-raised immediately without retrying.
    """
    attempts = settings["retries"]["attempts"]
    base_delay = settings["retries"]["base_delay_seconds"]

    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except PermanentError:
            raise
        except Exception as e:
            if attempt == attempts:
                raise
            wait = base_delay * 2 ** (attempt - 1)
            log.warning(
                "%s failed (attempt %d/%d): %s. Retrying in %ds.",
                description, attempt, attempts, e, wait,
            )
            time.sleep(wait)
