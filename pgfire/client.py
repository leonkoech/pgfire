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

    def writable_timestamp_columns(self, table: str) -> dict:
        """Plain (NOT generated) date/timestamp columns - e.g. created_at,
        session_date, due_date - mapped to their data_type. These can't be
        GENERATED (Postgres rejects text->timestamp casts there, not
        IMMUTABLE), so .set() must populate them itself from a matching key
        in the JSON payload or they silently stay NULL forever."""
        key = f"{table}::ts"
        if key not in self._cache:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = %s "
                    "AND is_generated = 'NEVER' AND data_type IN "
                    "('timestamp without time zone', 'timestamp with time zone', 'date')",
                    (table,),
                )
                self._cache[key] = {row[0]: row[1] for row in cur.fetchall()}
        return self._cache[key]

    def invalidate(self, table: str) -> None:
        self._cache.pop(table, None)


class PostgresClient:
    """Entry point: mirrors `firestore.client()`. `db.collection(name)`
    returns a query/write handle for that table."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        # Set while inside a `with db.transaction():` block, so individual
        # writes (DocumentRef.set/update/delete) skip their own commit and
        # let the transaction's __exit__ decide commit vs rollback. Without
        # this, each .set() call commits itself immediately regardless of
        # the transaction wrapper, making rollback-on-exception a no-op -
        # a real bug caught by this library's own test suite.
        self._in_transaction = False
        self._connect()

    def _connect(self) -> None:
        self.conn = psycopg2.connect(self._dsn)
        # Autocommit outside explicit transactions. Without it, psycopg2
        # opens a transaction on the first statement and keeps it open:
        #   - every read leaves the connection "idle in transaction",
        #     which on a long-lived connection blocks VACUUM and holds
        #     locks that stall migrations/DDL;
        #   - any failed statement aborts that transaction, and every
        #     later query on the connection then fails with
        #     InFailedSqlTransaction until someone rolls back - i.e. one
        #     bad query permanently breaks a shared per-process client.
        # With autocommit each statement is its own transaction, so
        # neither can happen. transaction() turns it off for its block.
        self.conn.autocommit = True
        self.columns = _ColumnCache(self.conn)

    def ensure_connected(self) -> None:
        """Reconnect if the connection has been closed (server restart,
        RDS failover, idle disconnect). psycopg2 marks a connection closed
        once a query on it hits a dead socket, so the request that hit the
        drop fails, and the next one transparently reconnects instead of
        the client staying dead for the life of the process. Never
        reconnects mid-transaction - that would silently drop the
        transaction's work."""
        if self.conn.closed and not self._in_transaction:
            self._connect()

    def collection(self, name: str):
        from .collection import CollectionRef
        self.ensure_connected()
        return CollectionRef(self, name)

    def get_all(self, references):
        """Firestore's batched get: one query per distinct table, returned as
        DocumentSnapshots in the SAME ORDER as `references` (a missing doc is
        a snapshot with .exists == False). Replaces an N+1 loop of .get()."""
        from .document import DocumentSnapshot
        refs = list(references)
        self.ensure_connected()
        by_table = {}
        for i, ref in enumerate(refs):
            by_table.setdefault(ref._table, []).append((i, ref))
        out = [None] * len(refs)
        for table, items in by_table.items():
            has_parent = "parent_id" in self.columns.real_columns(table)
            scoped = [(i, r) for i, r in items if has_parent and r._parent_id is not None]
            flat = [(i, r) for i, r in items if not (has_parent and r._parent_id is not None)]
            rows = {}
            with self.conn.cursor() as cur:
                if flat:
                    cur.execute(f'SELECT id, data FROM "{table}" WHERE id = ANY(%s)',
                                ([r.id for _, r in flat],))
                    rows_flat = {row[0]: row[1] for row in cur.fetchall()}
                    for i, r in flat:
                        out[i] = DocumentSnapshot(r.id, rows_flat.get(r.id), client=self,
                                                  table=table, parent_id=r._parent_id)
                if scoped:
                    # subcollection rows are keyed by (parent_id, id)
                    pairs = [(r._parent_id, r.id) for _, r in scoped]
                    from psycopg2.extras import execute_values
                    cur.execute(
                        f'SELECT parent_id, id, data FROM "{table}" WHERE (parent_id, id) IN %s',
                        (tuple(pairs),))
                    got = {(row[0], row[1]): row[2] for row in cur.fetchall()}
                    for i, r in scoped:
                        out[i] = DocumentSnapshot(r.id, got.get((r._parent_id, r.id)), client=self,
                                                  table=table, parent_id=r._parent_id)
        return out

    def collection_group(self, name: str):
        """Query every collection with id `name` at any depth: the top-level
        table `name` (if it exists) and every subcollection table `*__name`.
        Mirrors Firestore's collection_group. Read-only (stream/get/where/
        order_by/limit); writes aren't defined on a group."""
        from .collection import CollectionGroupQuery
        self.ensure_connected()
        return CollectionGroupQuery(self, name)

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
        if not was_already_in_transaction:
            self.ensure_connected()
            self.conn.autocommit = False
        self._in_transaction = True
        try:
            yield self
            if not was_already_in_transaction:
                self.conn.commit()
        except Exception:
            if not was_already_in_transaction and not self.conn.closed:
                self.conn.rollback()
            raise
        finally:
            self._in_transaction = was_already_in_transaction
            if not was_already_in_transaction and not self.conn.closed:
                self.conn.autocommit = True

    def close(self) -> None:
        self.conn.close()
