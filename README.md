# pgfire

A minimal, ~400-line Firestore-to-PostgreSQL migration bridge for Python. `pgfire` keeps the chainable, familiar developer experience of the Google Cloud Firestore SDK (`.collection().document().where()`) running on plain PostgreSQL tables with native JSONB.

## Scope & intention

**`pgfire` is not a general-purpose ORM for new projects.** It's a hyper-targeted tool for one real problem: migrating an existing Firestore-backed codebase to PostgreSQL with minimal call-site rewrites, when hundreds of existing `.collection().document().get()` calls make a wholesale relational redesign too slow and too risky to do up front.

If you're starting something new, use [SQLAlchemy](https://sqlalchemy.org) or [Prisma](https://prisma.io) instead — they're more capable, more battle-tested, and have real tooling (type-generated clients, migrations, connection pooling) that this project deliberately does not try to reinvent. If you're doing a surgical, incremental cutover of an existing Firestore app, `pgfire` is built for exactly that.

## How it actually works

Storage model: one Postgres table per Firestore collection. Every table has `id TEXT PRIMARY KEY` and `data JSONB`. Any field you filter/order on often can be promoted to a real `GENERATED ALWAYS AS (data->>'field') STORED` column for a real index — `pgfire` detects which columns exist via `information_schema` and uses them automatically; anything not promoted falls back to a `data->>'field'` JSONB lookup, so it works against a bare `(id, data)` table too.

Subcollections (`users/{uid}/moods/{uid}`) map to a separate table named by joining the path with `__` (`users__moods`), with a `parent_id` column holding the parent document's id. This composes at any depth automatically — there's no special-casing per collection, calling `.document(id).collection(name)` again just repeats the same rule on whatever table you're currently on.

Plain (non-generated) timestamp columns — e.g. `created_at`, `session_date` — can't be `GENERATED` (Postgres rejects `text->timestamp` casts there; the cast isn't `IMMUTABLE`). `pgfire`'s `.set()` populates any such column itself from a matching key in the document you pass in, so you don't have to maintain it by hand.

## What's implemented

- `db.collection(name)` / `.document(id)` — chainable refs, matching Firestore's shape
- `.get()` / `.set(data, merge=False)` / `.update(data)` / `.delete()`
- `.where(field, op, value)` with `==`, `!=`, `<`, `<=`, `>`, `>=`, `in` — chainable, `AND`-combined
- `.order_by(field, direction)`, `.limit(n)`
- `.stream()` (generator) / `.get()` (list) on a query
- `.count()` — `COUNT(*)` server-side, no row fetch
- `.aggregate(**named_filters)` — multiple `COUNT(*) FILTER (WHERE ...)` conditions in a single round trip; each value is `(field, op, value)` or a raw SQL boolean expression string
- Subcollections, composable to any depth
- `.batch()` — multiple writes applied together
- `db.transaction()` — a `BEGIN ... COMMIT` context manager (row locking is on you — take it out yourself with `SELECT ... FOR UPDATE` inside the block)

## What's not implemented

- `array_contains` / array-membership filters
- Cursor-based pagination (`start_after` / `start_at`) — use `.limit()` with an `.order_by()` on an indexed column instead
- Connection pooling — `PostgresClient` holds one plain `psycopg2` connection. Put a pooler (AWS RDS Proxy, PgBouncer) in front of it for any real concurrent traffic; don't share one `PostgresClient` across threads/requests without one.
- Compile-time type checking / a generated client — `pgfire` coerces values for JSONB comparisons at runtime (see `pgfire/utils.py`); there's no static schema to check your calls against. Lean on integration tests.
- Foreign-key enforcement — the `data` payload isn't validated against anything; nothing stops a dangling reference the way a real FK would.

Audit your own call sites against this list before assuming parity on anything not explicitly covered above.

## Quick start

```python
from pgfire import PostgresClient

db = PostgresClient("host=localhost port=5432 dbname=mydb user=me password=secret sslmode=require")

# Document access - same shape as the Firestore SDK
ref = db.collection("users").document("usr_12345")
ref.set({"email": "a@b.com", "user_type": "patient"})
snap = ref.get()
if snap.exists:
    user = snap.to_dict()

# Queries
therapists = db.collection("users").where("user_type", "==", "therapist").where("is_demo", "==", False).get()

# Server-side aggregation - no row fetch, no Python-side counting loop
counts = db.collection("users").where("user_type", "==", "patient").aggregate(
    total="TRUE",
    onboarded=("status", "==", "onboarded"),
)
# -> {"total": 30166, "onboarded": 30114}

# Subcollections
invite_ref = db.collection("companies").document("co_1").collection("invites").document()
invite_ref.set({"email": "invitee@example.com", "used": False})
```

## Production trade-offs

| Dimension | `pgfire`'s behavior | What you need to add |
| :--- | :--- | :--- |
| Connection pooling | None — one plain connection per `PostgresClient` | AWS RDS Proxy or PgBouncer in front |
| Type safety | Runtime coercion, no generated client | Integration tests on every call site you port |
| Data integrity | No foreign-key enforcement | A monitoring/cleanup job for orphaned references, if you need one |
| Concurrency | Synchronous, one connection | Fine behind a pooler with standard sync workers (e.g. Gunicorn); don't share a client across concurrent requests without one |
| Realtime | No `onSnapshot`-equivalent | Keep any feature depending on Firestore's live listeners on Firestore, or build your own realtime layer (websockets/SSE) before migrating it |

## Testing

```bash
pip install -e ".[test]"
pytest
```

Tests run against a real Postgres instance — set `PGFIRE_TEST_DSN` to a connection string for a disposable database (CI spins one up as a service container; see `.github/workflows/test.yml`).

## License

MIT.
