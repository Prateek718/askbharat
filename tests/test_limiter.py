"""Tests for the rate limiter and daily quota.

The daily quota is the piece that must not break: OpenRouter's free tier gives
1000 requests/day, the extraction job runs for days across restarts, and a
counter that silently resets would blow the quota and stall the pipeline.
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta

import pytest

from askbharat.llm.limiter import DailyQuota, DailyQuotaExceeded, TokenBucket


def test_token_bucket_allows_burst_up_to_rate(tmp_path):
    bucket = TokenBucket(rate_per_min=20)
    start = time.monotonic()
    for _ in range(20):
        bucket.take()
    # The first `rate_per_min` tokens are pre-filled, so a burst is immediate.
    assert time.monotonic() - start < 1.0


def test_token_bucket_throttles_beyond_rate():
    bucket = TokenBucket(rate_per_min=60)   # 1/sec
    for _ in range(60):
        bucket.take()
    start = time.monotonic()
    bucket.take()                            # must wait ~1s for a refill
    assert time.monotonic() - start >= 0.5


def test_quota_counts_and_persists(tmp_path):
    path = tmp_path / "q.json"
    q = DailyQuota(cap=5, path=path)
    for i in range(5):
        assert q.consume() == i + 1
    assert q.remaining == 0

    # A fresh instance must see the same count — this is the restart case.
    q2 = DailyQuota(cap=5, path=path)
    assert q2.used == 5
    with pytest.raises(DailyQuotaExceeded):
        q2.consume()


def test_quota_resets_on_new_day(tmp_path):
    path = tmp_path / "q.json"
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    path.write_text(json.dumps({"day": yesterday, "used": 999}))

    q = DailyQuota(cap=1000, path=path)
    assert q.used == 0, "yesterday's count must not carry over"
    assert q.remaining == 1000


def test_quota_refund_returns_unused_reservation(tmp_path):
    q = DailyQuota(cap=10, path=tmp_path / "q.json")
    q.consume(3)
    q.refund(2)
    assert q.used == 1


def test_quota_refund_never_goes_negative(tmp_path):
    q = DailyQuota(cap=10, path=tmp_path / "q.json")
    q.consume(1)
    q.refund(5)
    assert q.used == 0


def test_quota_survives_corrupt_state_file(tmp_path):
    path = tmp_path / "q.json"
    path.write_text("{not json at all")
    q = DailyQuota(cap=10, path=path)
    # Must not crash the pipeline; starting fresh is the safe failure mode.
    assert q.used == 0
    assert q.consume() == 1


def test_exceeded_error_reports_reset_window(tmp_path):
    q = DailyQuota(cap=1, path=tmp_path / "q.json")
    q.consume()
    with pytest.raises(DailyQuotaExceeded) as exc:
        q.consume()
    assert exc.value.cap == 1
    assert 0 < exc.value.resets_in_s <= 86400
