# Blockers Log

Real problems encountered during development. Each entry records what happened, the exact error,
root cause, and fix.

---

### Blocker 1: Python environment mismatch and native wheel incompatibility
- **Date**: 2026-11-20
- **What happened**: When installing Python packages, the unsandboxed environment defaulted to Python 3.9 while the sandboxed runner used Python 3.14. Installing dependencies into a local directory produced `_pydantic_core.cpython-39-darwin.so`, causing `ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'` when pytest executed inside Python 3.14.
- **Root cause**: Host environment had micromamba Python 3.9 in PATH while framework Python 3.14 was active in the sandbox. Native C-extensions built for 3.9 were rejected by 3.14.
- **How it was fixed**: Created a self-contained virtualenv at `.venv` explicitly targeting Python 3.13 (`/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 -m venv .venv`), ensuring all native wheels match the interpreter used both inside and outside the sandbox.

---

### Blocker 2: SQLite in-memory database table loss in FastAPI test client
- **Date**: 2026-11-20
- **What happened**: All 8 endpoint tests in `tests/test_api.py` failed with `sqlalchemy.exc.OperationalError: no such table: professors`.
- **Root cause**: SQLite `:memory:` databases create a distinct, empty database for each new DB-API connection unless `StaticPool` is configured. When the FastAPI request opened a new session connection, it saw an empty in-memory database rather than the seeded tables.
- **How it was fixed**: Configured the test engine with `poolclass=StaticPool` and `connect_args={"check_same_thread": False}`, ensuring all threads and requests share the identical in-memory database connection.

---

### Blocker 3: Retry mechanism masking simulated failure in batch runner test
- **Date**: 2026-11-20
- **What happened**: In `test_simulated_crash_mid_batch`, professor 2 unexpectedly ended up in `DRAFTED` status instead of `NEEDS_HUMAN` or `FAILED_DRAFT`, failing `assert 'DRAFTED' in ('NEEDS_HUMAN', 'FAILED_DRAFT')`.
- **Root cause**: The mock LLM raised an error on call index 3 (attempt 1 for professor 2). However, `with_retry(max_attempts=2)` treated the `RuntimeError` as a transient failure and retried. Call index 4 returned a successful response, so professor 2 succeeded on the retry.
- **How it was fixed**: Updated the mock LLM to check `user_msg` for the specific professor ("Bob Jones") and raise the exception consistently across attempts, accurately validating that professor failure triggers escalation to `NEEDS_HUMAN` while other professors in the batch continue processing without interruption.
