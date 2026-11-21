# Decisions Log

Each entry records a judgment call made during development.

| # | Decision | Reasoning |
|---|----------|-----------|
| 1 | Use SQLAlchemy 2.x with synchronous engine for SQLite | Async SQLite adds complexity without real benefit; SQLite doesn't truly async anyway |
| 2 | Use `hashlib.sha256` for text hashing in approval gate | Cryptographic hash is overkill but cheap, and eliminates any collision concern |
| 3 | State machine transitions defined as a frozen set of (from, to) tuples | Simple, explicit, easy to test; no need for a state machine library |
| 4 | LLM judge model uses same GROQ model as drafter | Avoids needing a second API key; the judge prompt is different enough to provide value |
| 5 | Idempotency key format: `{professor_id}:{kind}:{sequence}` | Directly derived from task spec; sequence starts at 1 |
| 6 | PDF text extraction limited to first 3 pages for introduction | Balances context quality with token limits and processing time |
| 7 | JSON extraction tries: raw parse, strip markdown fences, regex extract, find first `{`...`}` | Covers common LLM output quirks without over-engineering |
| 8 | CLI built with Typer for rich terminal output | Simple, well-documented, works well with Rich for tables |
| 9 | React frontend uses Vite with minimal dependencies | Keeps frontend simple and fast to build |
| 10 | Follow-up drafts include context from original email stored in DB | Prevents contradictions between initial and follow-up emails |
| 11 | IMAP reply checker is behind an interface, off by default | Most users will manually mark replies; IMAP adds credential complexity |
| 12 | Retry module uses exponential backoff with full jitter | Standard practice, prevents thundering herd on rate limits |
| 13 | Schema version tracked in a `schema_version` table | Simple migration path without alembic dependency |
| 14 | Use Python 3.13 venv inside workspace | Solves interpreter mismatch between sandbox and host environment |
| 15 | Use `StaticPool` for in-memory SQLite in API tests | Ensures all connections share the same tables and state during tests |
| 16 | Use timezone-aware UTC datetime | Modern Python 3.11+ standard avoiding deprecated `utcnow()` |
| 17 | Use `Session.get()` instead of `Query.get()` | Follows SQLAlchemy 2.0 idiom and clears deprecation warnings |
| 18 | Build React frontend to static `dist/` | Keeps UI portable and servable directly from static folder or Vite dev server |
