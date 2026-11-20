## Tools Used
- Gemini / LLM assistant for scaffolding FastAPI routes, SQLAlchemy models, and pytest fixtures.

## Key Prompts
- "Design a multi-tenant append-only ledger schema in SQLAlchemy with idempotency constraints and point-in-time balance queries."
- "Implement partial-success bulk ingestion in FastAPI using nested database transactions."

## Corrections & Judgment Calls
- Replaced initial float fields with `Numeric(18, 4)` and Python `Decimal` to avoid floating-point rounding drift.
- Added `with db.begin_nested():` savepoints inside the bulk loop so a `UniqueConstraint` violation on a duplicate row rolls back only that row rather than aborting the entire batch session.
- Added a unique constraint on `reverses_entry_id` to prevent concurrent double-reversals at the database level.
