"""Core Postgres connection management and the top-level client."""

from __future__ import annotations

from contextlib import contextmanager

import psycopg2


class _ColumnCache:
    """Caches which fields are real (GENERATED) columns per table, so
    .where()/.order_by() can use the fast indexed path when available and
    fall back to a JSONB expression otherwise. Also tracks plain (NOT
    generated) timestamp columns, which .set() must populate itself."""

    def __init__(self, conn):
        self._conn = conn
        self._cache: dict = {}

    def real_columns(self, table: str) -> set:
        if table not in self._cache:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = %s",
                    (table,),
                )
                self._cache[table] = {row[0] for row in cur.fetchall()}
        return self._cache[table]

    def writable_timestamp_columns(self, table: str) -> set:
        """Plain (NOT generated) date/timestamp columns - e.g. created_at,
        session_date, due_date. These can't be GENERATED (Postgres rejects
        text->timestamp casts there, not IMMUTABLE), so .set() must
        populate them itself from a matching key in the JSON payload or
        they silently stay NULL forever."""
        key = f"{table}::ts"
        if key not in self._cache:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = %s "
                    "AND is_generated = 'NEVER' AND data_type IN "
                    "('timestamp without time zone', 'timestamp with time zone', 'date')",
                    (table,),
                )
                self._cache[key] = {row[0] for row in cur.fetchall()}
        return self._cache[key]

    def invalidate(self, table: str) -> None:
        self._cache.pop(table, None)


class PostgresClient:
    """Entry point: mirrors `firestore.client()`. `db.collection(name)`
    returns a query/write handle for that table."""

    def __init__(self, dsn: str):
        self.conn = psycopg2.connect(dsn)
        self.columns = _ColumnCache(self.conn)
        # Set while inside a `with db.transaction():` block, so individual
        # writes (DocumentRef.set/update/delete) skip their own commit and
        # let the transaction's __exit__ decide commit vs rollback. Without
        # this, each .set() call commits itself immediately regardless of
        # the transaction wrapper, making rollback-on-exception a no-op -
        # a real bug caught by this library's own test suite.
        self._in_transaction = False

    def collection(self, name: str):
        from .collection import CollectionRef
        return CollectionRef(self, name)

    def batch(self):
        from .collection import WriteBatch
        return WriteBatch(self)

    @contextmanager
    def transaction(self):
        """Mirrors Firestore's transaction context: BEGIN ... COMMIT, with
        row locks acquired by the caller's own SELECT ... FOR UPDATE reads
        inside the `with` block - this does not take out any lock itself.

        Nested `with db.transaction():` blocks are flattened onto the
        outermost one (only it commits/rolls back) rather than trying to
        model real savepoints - good enough for this library's scope."""
        was_already_in_transaction = self._in_transaction
        self._in_transaction = True
        try:
            yield self
            if not was_already_in_transaction:
                self.conn.commit()
        except Exception:
            if not was_already_in_transaction:
                self.conn.rollback()
            raise
        finally:
            self._in_transaction = was_already_in_transaction

    def close(self) -> None:
        self.conn.close()
