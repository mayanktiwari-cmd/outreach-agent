"""Tests for the retry module."""

from __future__ import annotations

import time

import pytest

from outreach.retry import (
    PermanentError,
    TransientError,
    retry_sync,
)


class TestRetry:
    def test_success_first_try(self):
        call_count = 0

        def succeeds():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = retry_sync(succeeds, max_attempts=3, base_delay=0.01, timeout=5)
        assert result == "ok"
        assert call_count == 1

    def test_transient_then_success(self):
        call_count = 0

        def fails_once():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TransientError("temporary")
            return "ok"

        result = retry_sync(fails_once, max_attempts=3, base_delay=0.01, timeout=5)
        assert result == "ok"
        assert call_count == 2

    def test_permanent_error_no_retry(self):
        call_count = 0

        def fails_permanently():
            nonlocal call_count
            call_count += 1
            raise PermanentError("bad request")

        with pytest.raises(PermanentError):
            retry_sync(fails_permanently, max_attempts=3, base_delay=0.01, timeout=5)
        assert call_count == 1  # No retry

    def test_all_retries_exhausted(self):
        call_count = 0

        def always_fails():
            nonlocal call_count
            call_count += 1
            raise TransientError("always fails")

        with pytest.raises(TransientError):
            retry_sync(always_fails, max_attempts=3, base_delay=0.01, timeout=5)
        assert call_count == 3

    def test_timeout(self):
        def slow_func():
            time.sleep(10)
            return "done"

        with pytest.raises(TimeoutError):
            retry_sync(slow_func, max_attempts=1, base_delay=0.01, timeout=0.5)

    def test_record_attempt_callback(self):
        attempts = []

        def record(professor_id, step, attempt, ok, error, duration_ms):
            attempts.append({"attempt": attempt, "ok": ok, "error": error})

        def fails_then_succeeds():
            if len(attempts) == 0 or not attempts[-1]["ok"]:
                if len(attempts) < 1:
                    raise TransientError("first fail")
            return "ok"

        call_count = 0

        def tracked_func():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TransientError("fail")
            return "ok"

        result = retry_sync(
            tracked_func,
            max_attempts=3,
            base_delay=0.01,
            timeout=5,
            step_name="test_step",
            professor_id=1,
            record_attempt=record,
        )
        assert result == "ok"
        assert len(attempts) == 2
        assert attempts[0]["ok"] is False
        assert attempts[1]["ok"] is True
