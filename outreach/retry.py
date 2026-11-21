"""Retry module with timeout, exponential backoff, jitter, and error classification."""

from __future__ import annotations

import functools
import logging
import random
import time
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class PermanentError(Exception):
    """An error that should NOT be retried (e.g., 4xx other than 429)."""
    pass


class TransientError(Exception):
    """An error that SHOULD be retried (e.g., 429, 5xx, timeout)."""
    pass


def classify_http_error(status_code: int) -> type[Exception]:
    """Classify HTTP status codes into permanent or transient errors."""
    if status_code == 429:
        return TransientError
    if 400 <= status_code < 500:
        return PermanentError
    if status_code >= 500:
        return TransientError
    return PermanentError


def with_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    timeout: float = 60.0,
    step_name: str = "unknown",
    professor_id: int | None = None,
    record_attempt: Callable | None = None,
) -> Callable:
    """Decorator/wrapper for retrying operations with exponential backoff and jitter.

    Args:
        max_attempts: Maximum number of attempts (including the first).
        base_delay: Base delay in seconds for exponential backoff.
        max_delay: Maximum delay between retries.
        timeout: Timeout per attempt in seconds.
        step_name: Name of the step for logging.
        professor_id: Professor ID for logging.
        record_attempt: Callback(professor_id, step, attempt, ok, error, duration_ms)
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            last_error = None
            for attempt in range(1, max_attempts + 1):
                start = time.monotonic()
                try:
                    # Apply timeout using a simple approach for sync code
                    result = _run_with_timeout(func, timeout, *args, **kwargs)
                    duration_ms = int((time.monotonic() - start) * 1000)
                    if record_attempt:
                        record_attempt(
                            professor_id=professor_id,
                            step=step_name,
                            attempt=attempt,
                            ok=True,
                            error=None,
                            duration_ms=duration_ms,
                        )
                    return result

                except PermanentError as e:
                    duration_ms = int((time.monotonic() - start) * 1000)
                    logger.error(
                        f"[{step_name}] Permanent error on attempt {attempt}: {e}"
                    )
                    if record_attempt:
                        record_attempt(
                            professor_id=professor_id,
                            step=step_name,
                            attempt=attempt,
                            ok=False,
                            error=str(e),
                            duration_ms=duration_ms,
                        )
                    raise  # No retry for permanent errors

                except (TransientError, TimeoutError, OSError, ConnectionError) as e:
                    duration_ms = int((time.monotonic() - start) * 1000)
                    last_error = e
                    logger.warning(
                        f"[{step_name}] Transient error on attempt {attempt}/{max_attempts}: {e}"
                    )
                    if record_attempt:
                        record_attempt(
                            professor_id=professor_id,
                            step=step_name,
                            attempt=attempt,
                            ok=False,
                            error=str(e),
                            duration_ms=duration_ms,
                        )
                    if attempt < max_attempts:
                        delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                        # Full jitter: random between 0 and calculated delay
                        jitter_delay = random.uniform(0, delay)
                        logger.info(f"[{step_name}] Retrying in {jitter_delay:.1f}s...")
                        time.sleep(jitter_delay)

                except Exception as e:
                    duration_ms = int((time.monotonic() - start) * 1000)
                    last_error = e
                    logger.error(
                        f"[{step_name}] Unexpected error on attempt {attempt}: {e}"
                    )
                    if record_attempt:
                        record_attempt(
                            professor_id=professor_id,
                            step=step_name,
                            attempt=attempt,
                            ok=False,
                            error=str(e),
                            duration_ms=duration_ms,
                        )
                    if attempt < max_attempts:
                        delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                        jitter_delay = random.uniform(0, delay)
                        time.sleep(jitter_delay)

            raise last_error  # type: ignore[misc]

        return wrapper

    return decorator


def _run_with_timeout(func: Callable, timeout: float, *args: Any, **kwargs: Any) -> Any:
    """Run a synchronous function with a timeout using threading."""
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(func, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"Operation timed out after {timeout}s")


def retry_sync(
    func: Callable[..., T],
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    timeout: float = 60.0,
    step_name: str = "unknown",
    professor_id: int | None = None,
    record_attempt: Callable | None = None,
    **kwargs: Any,
) -> T:
    """Convenience function: retry a callable without decorator syntax."""
    wrapped = with_retry(
        max_attempts=max_attempts,
        base_delay=base_delay,
        max_delay=max_delay,
        timeout=timeout,
        step_name=step_name,
        professor_id=professor_id,
        record_attempt=record_attempt,
    )(func)
    return wrapped(*args, **kwargs)
