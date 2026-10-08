"""Firestore-chain-mimicking query logic: .collection(), .where(),
.order_by(), .limit(), .count(), .aggregate(), plus the write-side
CollectionRef and WriteBatch."""

from __future__ import annotations

from typing import Any, Iterator, Optional

from .document import DocumentRef, DocumentSnapshot, new_id
from .utils import compare_sql, order_expr


class Query:
    def __init__(self, client, table: str, parent_id: Optional[str] = None):
        self._client = client
        self._table = table
        self._parent_id = parent_id
        self._filters: list = []
        self._order: Optional[tuple] = None
        self._limit_n: Optional[int] = None

    def where(self, field: str, op: str, value: Any) -> "Query":
        q = self._clone()
        q._filters.append((field, op, value))
        return q

    def order_by(self, field: str, direction: str = "ASCENDING") -> "Query":
        q = self._clone()
        q._order = (field, "DESC" if direction.upper().startswith("DESC") else "ASC")
        return q

    def limit(self, n: int) -> "Query":
        q = self._clone()
        q._limit_n = n
        return q

    def _clone(self) -> "Query":
        q = Query(self._client, self._table, self._parent_id)
        q._filters = list(self._filters)
        q._order = self._order
        q._limit_n = self._limit_n
        return q

    def _where_sql(self) -> tuple:
        """The WHERE clause only (no SELECT/ORDER/LIMIT), reused by
        stream()/count()/aggregate() so the latter two can skip
        fetching+deserializing every row - a server-side capability
        Firestore's API has no equivalent for."""
        real_cols = self._client.columns.real_columns(self._table)
        where_parts = []
        params: list = []
        if self._parent_id is not None:
            where_parts.append("parent_id = %s")
            params.append(self._parent_id)
        for field, op, value in self._filters:
            sql, p = compare_sql(field, op, value, real_cols)
            where_parts.append(sql)
            params.extend(p)
        clause = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        return clause, params

    def _build_sql(self) -> tuple:
        real_cols = self._client.columns.real_columns(self._table)
        where_sql, params = self._where_sql()

        sql = f'SELECT id, data FROM "{self._table}"{where_sql}'
        if self._order:
            field, direction = self._order
            sql += f" ORDER BY {order_expr(field, real_cols)} {direction}"
        if self._limit_n is not None:
            sql += " LIMIT %s"
            params.append(self._limit_n)
        return sql, params

    def stream(self) -> Iterator[DocumentSnapshot]:
        sql, params = self._build_sql()
        with self._client.conn.cursor() as cur:
            cur.execute(sql, params)
            for doc_id, data in cur.fetchall():
                yield DocumentSnapshot(doc_id, data)

    def get(self) -> list:
        return list(self.stream())

    def count(self) -> int:
        """COUNT(*) server-side - no document fetch/deserialize at all.
        The core fix for the "stream every doc into Python just to tally
        a count" anti-pattern a Firestore-shaped API otherwise invites."""
        where_sql, params = self._where_sql()
        sql = f'SELECT COUNT(*) FROM "{self._table}"{where_sql}'
        with self._client.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()[0]

    def aggregate(self, **named_filters) -> dict:
        """Multiple COUNT(*) FILTER(WHERE ...) in a single round trip.

        Each kwarg value is (field, op, value) - same shape as .where() -
        or a raw SQL boolean expression string for anything .where() can't
        express (e.g. a cross-field comparison, or an existing index not
        modeled as a plain equality).

        Example:
            db.collection("users").where("user_type", "==", "patient").aggregate(
                total="TRUE",                      # counts every row matching the base filter
                onboarded=("status", "==", "onboarded"),
                has_company=("company_id", "!=", None),
            )
            -> {"total": 30166, "onboarded": 30114, "has_company": 30153}
        """
        base_where, base_params = self._where_sql()
        real_cols = self._client.columns.real_columns(self._table)
        select_parts = []
        # SELECT's FILTER placeholders appear BEFORE the WHERE clause's in
        # the final SQL text, so their params must be collected first -
        # psycopg2 binds %s positionally in text order, not list-append order.
        select_params: list = []
        for label, cond in named_filters.items():
            if isinstance(cond, str):
                select_parts.append(f"COUNT(*) FILTER (WHERE {cond}) AS {label}")
                continue
            field, op, value = cond
            sql, p = compare_sql(field, op, value, real_cols)
            select_parts.append(f"COUNT(*) FILTER (WHERE {sql}) AS {label}")
            select_params.extend(p)
        params = select_params + base_params
        sql = f'SELECT {", ".join(select_parts)} FROM "{self._table}"{base_where}'
        with self._client.conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            columns = [desc[0] for desc in cur.description]
            return dict(zip(columns, row))


class CollectionRef(Query):
    """A Query with no filters yet, plus the write-side methods Firestore's
    CollectionReference exposes (.document(), .add())."""

    def document(self, doc_id: Optional[str] = None) -> DocumentRef:
        return DocumentRef(self._client, self._table, doc_id or new_id(), parent_id=self._parent_id)

    def add(self, data: dict) -> tuple:
        ref = self.document()
        ref.set(data)
        return ref, ref.get()


class WriteBatch:
    def __init__(self, client):
        self._client = client
        self._ops: list = []

    def set(self, ref: DocumentRef, data: dict, merge: bool = False) -> None:
        self._ops.append(("set", ref, data, merge))

    def update(self, ref: DocumentRef, data: dict) -> None:
        self._ops.append(("set", ref, data, True))

    def delete(self, ref: DocumentRef) -> None:
        self._ops.append(("delete", ref, None, False))

    def commit(self) -> None:
        for op, ref, data, merge in self._ops:
            if op == "delete":
                ref.delete()
            else:
                ref.set(data, merge=merge)
