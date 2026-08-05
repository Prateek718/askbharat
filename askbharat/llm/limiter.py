"""Rate limiting for OpenRouter's free tier.

Two independent limits apply, and they fail differently:

  - **20 requests/minute** — a burst limit. Exceeding it returns 429; we simply
    wait. A token bucket handles this.
  - **1000 requests/day** (50 without a credit purchase) — a hard daily quota.
    Exceeding it means no further work is possible until the window rolls over,
    so the correct behaviour is to *park the job*, not to spin on retries.

The daily counter is persisted to disk rather than held in memory, because the
extraction job runs for days across restarts. A counter that resets on restart
would silently blow through the quota.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from askbharat.config import DATA_DIR


class DailyQuotaExceeded(RuntimeError):
    """Raised when the daily cap is spent. The caller should park, not retry."""

    def __init__(self, used: int, cap: int, resets_in_s: float):
        self.used, self.cap, self.resets_in_s = used, cap, resets_in_s
        hours = resets_in_s / 3600
        super().__init__(
            f"daily quota spent ({used}/{cap}); resets in {hours:.1f}h"
        )


@dataclass
class TokenBucket:
    """Classic token bucket. Blocks until a token is available."""

    rate_per_min: int = 20
    _tokens: float = 0.0
    _last: float = 0.0

    def __post_init__(self):
        self._tokens = float(self.rate_per_min)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def take(self) -> float:
        """Consume one token, sleeping if necessary. Returns seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    float(self.rate_per_min),
                    self._tokens + (now - self._last) * self.rate_per_min / 60.0,
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return waited
                deficit = 1.0 - self._tokens
                sleep_for = deficit * 60.0 / self.rate_per_min
            time.sleep(min(sleep_for, 5.0))
            waited += min(sleep_for, 5.0)


class DailyQuota:
    """Persistent per-day request counter."""

    def __init__(self, cap: int = 1000, path: Path | None = None):
        self.cap = cap
        self.path = path or (DATA_DIR / "llm_quota.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _read(self) -> dict:
        if not self.path.exists():
            return {"day": date.today().isoformat(), "used": 0}
        try:
            d = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return {"day": date.today().isoformat(), "used": 0}
        if d.get("day") != date.today().isoformat():
            return {"day": date.today().isoformat(), "used": 0}
        return d

    @property
    def used(self) -> int:
        return self._read().get("used", 0)

    @property
    def remaining(self) -> int:
        return max(0, self.cap - self.used)

    @staticmethod
    def _seconds_to_reset() -> float:
        now = time.time()
        # OpenRouter's daily window rolls at UTC midnight.
        return 86400 - (now % 86400)

    def consume(self, n: int = 1) -> int:
        """Reserve n requests. Raises DailyQuotaExceeded if the cap is spent."""
        with self._lock:
            d = self._read()
            if d["used"] + n > self.cap:
                raise DailyQuotaExceeded(d["used"], self.cap, self._seconds_to_reset())
            d["used"] += n
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(d))
            tmp.replace(self.path)   # atomic — survives a crash mid-write
            return d["used"]

    def refund(self, n: int = 1) -> None:
        """Return unused reservations (e.g. the request never left the client)."""
        with self._lock:
            d = self._read()
            d["used"] = max(0, d["used"] - n)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(d))
            tmp.replace(self.path)
