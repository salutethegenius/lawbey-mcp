"""Per-partner in-memory rate limiting.

MVP uses in-process counters — acceptable for a single-instance deploy.
If we scale horizontally later, swap this for a Redis-backed limiter.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Tuple

from fastapi import HTTPException, status

from .config import Settings


class RateLimiter:
    """Sliding-window-ish counter: tracks request timestamps per partner.

    Limits:
      - ``rate_limit_per_hour`` requests in the last 3600s
      - ``rate_limit_per_day`` requests in the last 86400s
    """

    def __init__(self, settings: Settings):
        self._per_hour = settings.rate_limit_per_hour
        self._per_day = settings.rate_limit_per_day
        self._hour_hits: dict[str, list[float]] = defaultdict(list)
        self._day_hits: dict[str, list[float]] = defaultdict(list)

    def check(self, partner: str) -> Tuple[bool, int]:
        """Return (allowed, retry_after_seconds). Raises 429 if exceeded."""
        now = time.monotonic()

        hour_hits = self._hour_hits[partner]
        day_hits = self._day_hits[partner]

        # Drop expired timestamps.
        self._prune(hour_hits, now, 3600)
        self._prune(day_hits, now, 86400)

        if len(hour_hits) >= self._per_hour or len(day_hits) >= self._per_day:
            # Retry-After = seconds until the oldest hit in the violated window expires.
            retry = 3600
            if len(day_hits) >= self._per_day and len(day_hits) >= len(hour_hits):
                retry = max(1, int(86400 - (now - day_hits[0])))
            elif hour_hits:
                retry = max(1, int(3600 - (now - hour_hits[0])))
            return False, retry

        hour_hits.append(now)
        day_hits.append(now)
        return True, 0

    @staticmethod
    def _prune(hits: list[float], now: float, window: float) -> None:
        """In-place drop timestamps older than ``window`` seconds."""
        cutoff = now - window
        # hits are appended in time order, so we can pop from the front.
        while hits and hits[0] < cutoff:
            hits.pop(0)


def enforce_rate_limit(
    settings: Settings,
    limiter: RateLimiter,
    partner: str,
) -> None:
    """FastAPI-friendly guard: raise 429 if the partner is over limit."""
    allowed, retry_after = limiter.check(partner)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )
