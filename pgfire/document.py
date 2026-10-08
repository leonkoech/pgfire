"""Individual document reads, writes, and the snapshot object returned by them."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from typing import Optional

from .utils import json_default


class DocumentSnapshot:
    """Mirrors Firestore's DocumentSnapshot: `.exists`, `.id`, `.to_dict()`."""

    def __init__(self, doc_id: Optional[str], data: Optional[dict]):
        self.id = doc_id
        self._data = data
        self.exists = data is not None

    def to_dict(self) -> Optional[dict]:
        return dict(self._data) if self._data is not None else None


class DocumentRef:
    def __init__(self, client, table: str, doc_id: str, parent_id: Optional[str] = None):
        self._client = client
        self._table = table
        self.id = doc_id
        self._parent_id = parent_id

    def collection(self, name: str):
        """Subcollection: users/{uid}/moods -> table `users__moods`, scoped
        to parent_id = uid. Composes at any depth - calling .collection()
        again on a document under this one just repeats the same rule."""
        from .collection import CollectionRef
        return CollectionRef(self._client, f"{self._table}__{name}", parent_id=self.id)

    def get(self) -> DocumentSnapshot:
        # Scope by parent_id too when it's actually known - but a DocumentRef
        # built via a flat collection path (e.g. db.collection("companies__invites")
        # with no parent chain) has parent_id=None on purpose, to do a global
        # by-id lookup across every parent. Filtering on "parent_id = NULL"
        # there would never match anything (SQL NULL semantics), so this
        # mirrors Query._where_sql's own "only filter when parent_id is not
        # None" rule rather than always filtering whenever the column exists.
        has_parent_col = "parent_id" in self._client.columns.real_columns(self._table)
        scope_to_parent = has_parent_col and self._parent_id is not None
        with self._client.conn.cursor() as cur:
            if scope_to_parent:
                cur.execute(
                    f'SELECT data FROM "{self._table}" WHERE id = %s AND parent_id = %s',
                    (self.id, self._parent_id),
                )
            else:
                cur.execute(f'SELECT data FROM "{self._table}" WHERE id = %s', (self.id,))
            row = cur.fetchone()
            return DocumentSnapshot(self.id, row[0] if row else None)

    def update(self, data: dict) -> None:
        """Like Firestore's DocumentReference.update(): merges fields into an
        EXISTING document and raises NotFound if there isn't one. (Upserting
        here would e.g. turn an update to a deleted calendar event into a
        brand-new ghost event holding only the updated fields.) One atomic
        UPDATE statement, transforms included."""
        plain, ops = _split_fields(data)
        if ops:
            data_expr, params = _merge_with_transforms_sql(self._table, plain, ops)
        else:
            data_expr, params = f'"{self._table}".data || %s::jsonb', [json.dumps(plain, default=json_default)]
        sets = [f"data = {data_expr}"]
        for col, value in self._timestamp_updates(plain).items():
            sets.append(f"{col} = %s")
            params.append(value)
        where, where_params = self._where_self()
        with self._client.conn.cursor() as cur:
            cur.execute(f'UPDATE "{self._table}" SET {", ".join(sets)} WHERE {where}', params + where_params)
            if cur.rowcount == 0:
                raise NotFound(f"No document to update: {self._table}/{self.id}")
        if not self._client._in_transaction:
            self._client.conn.commit()

    def _where_self(self) -> tuple:
        has_parent_col = "parent_id" in self._client.columns.real_columns(self._table)
        if has_parent_col and self._parent_id is not None:
            return "id = %s AND parent_id = %s", [self.id, self._parent_id]
        return "id = %s", [self.id]

    def _timestamp_updates(self, plain: dict) -> dict:
        # Plain timestamp columns (created_at, session_date, due_date, ...)
        # aren't GENERATED, so populate any that match a key in `data`
        # ourselves - otherwise they silently stay NULL forever.
        out = {}
        for col, data_type in self._client.columns.writable_timestamp_columns(self._table).items():
            if col in plain and plain[col] is not None:
                value = _timestamp_column_value(plain[col], data_type)
                if value is not None:
                    out[col] = value
        return out

    def set(self, data: dict, merge: bool = False) -> None:
        from . import transforms as tf

        plain, ops = _split_fields(data)

        # The document as it lands when no row exists yet (and, for a
        # non-merge set, the whole replacement document): transforms take
        # their "no prior value" result, as in Firestore.
        initial = dict(plain)
        for key, (k, value) in ops.items():
            if k == "increment":
                initial[key] = value.value
            elif k == "array_union":
                initial[key] = tf.dedupe(list(value.values))
            elif k == "array_remove":
                initial[key] = []
        payload = json.dumps(initial, default=json_default)
        has_parent_col = "parent_id" in self._client.columns.real_columns(self._table)
        ts_updates = self._timestamp_updates(plain)

        col_names = ["id"]
        col_values = [self.id]
        value_placeholders = ["%s"]
        if has_parent_col:
            col_names.append("parent_id")
            col_values.append(self._parent_id)
            value_placeholders.append("%s")
        col_names.append("data")
        col_values.append(payload)
        value_placeholders.append("%s::jsonb")
        for col in ts_updates:
            col_names.append(col)
            col_values.append(ts_updates[col])
            value_placeholders.append("%s")

        update_params: list = []
        if merge and ops:
            data_expr, update_params = _merge_with_transforms_sql(self._table, plain, ops)
            merge_clause_parts = [f"data = {data_expr}"]
        else:
            merge_clause_parts = [
                f'data = "{self._table}".data || EXCLUDED.data' if merge else "data = EXCLUDED.data"
            ]
        for col in ts_updates:
            merge_clause_parts.append(f"{col} = EXCLUDED.{col}")

        # Child doc ids are only unique WITHIN their parent (same as a
        # Firestore subcollection) - conflict on (parent_id, id) when this
        # table has one, not on `id` alone, or two parents writing the same
        # child id (e.g. the same calendar event id under both a patient's
        # and a therapist's users/{uid}/calendar) collide instead of
        # producing two independent rows.
        conflict_cols = "(parent_id, id)" if has_parent_col else "(id)"

        with self._client.conn.cursor() as cur:
            cur.execute(
                f'INSERT INTO "{self._table}" ({", ".join(col_names)}) '
                f'VALUES ({", ".join(value_placeholders)}) '
                f'ON CONFLICT {conflict_cols} DO UPDATE SET {", ".join(merge_clause_parts)}',
                col_values + update_params,
            )
        if not self._client._in_transaction:
            self._client.conn.commit()

    def delete(self) -> None:
        # Same "only scope to parent_id when it's actually known" rule as get().
        has_parent_col = "parent_id" in self._client.columns.real_columns(self._table)
        scope_to_parent = has_parent_col and self._parent_id is not None
        with self._client.conn.cursor() as cur:
            if scope_to_parent:
                cur.execute(
                    f'DELETE FROM "{self._table}" WHERE id = %s AND parent_id = %s',
                    (self.id, self._parent_id),
                )
            else:
                cur.execute(f'DELETE FROM "{self._table}" WHERE id = %s', (self.id,))
        if not self._client._in_transaction:
            self._client.conn.commit()


def new_id() -> str:
    return str(uuid.uuid4())


class NotFound(LookupError):
    """Raised by DocumentRef.update() when the document doesn't exist
    (Firestore raises google.api_core.exceptions.NotFound there)."""


def _split_fields(data: dict) -> tuple:
    """Split a write's fields into plain values and field transforms.
    SERVER_TIMESTAMP simply becomes "now"; the rest are applied server-side."""
    from . import transforms as tf

    now = datetime.now(timezone.utc)
    plain, ops = {}, {}
    for key, value in data.items():
        k = tf.kind(value)
        if k == "server_timestamp":
            plain[key] = now
        elif k is None:
            if tf.contains_nested_transform(value):
                raise NotImplementedError(
                    f"Field transforms nested inside '{key}' aren't supported; use a top-level field")
            plain[key] = value
        else:
            ops[key] = (k, value)
    return plain, ops


def _merge_with_transforms_sql(table: str, plain: dict, ops: dict) -> tuple:
    """SQL expression (+ params, in text order) for the existing row's new
    `data` in a merge write that carries field transforms. Everything is
    one expression inside the upsert, so it's atomic like Firestore's
    server-side transforms."""
    from .transforms import dedupe

    old = f'"{table}".data'
    # (sql_fragment, params) pieces, concatenated in order, so params always
    # line up with the %s placeholders in the final text.
    frags: list = []

    def emit(sql: str, *ps):
        frags.append((sql, list(ps)))

    def existing_array(key):
        # A missing or non-array prior value counts as an empty array.
        emit(f"(CASE WHEN jsonb_typeof({old}->%s) = 'array' THEN {old}->%s ELSE '[]'::jsonb END)", key, key)

    pair_count, deletes = 0, []
    emit(f"(({old} || %s::jsonb)", json.dumps(plain, default=json_default))
    for key, (k, value) in ops.items():
        if k == "delete":
            deletes.append(key)
            continue
        emit(" || jsonb_build_object(%s, ", key)
        if k == "increment":
            emit(f"CASE WHEN jsonb_typeof({old}->%s) = 'number' "
                 f"THEN to_jsonb(({old}->>%s)::numeric + %s::numeric) ELSE to_jsonb(%s::numeric) END",
                 key, key, value.value, value.value)
        elif k == "array_union":
            existing_array(key)
            emit(" || (SELECT COALESCE(jsonb_agg(x.m ORDER BY x.o), '[]'::jsonb) "
                 "FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY AS x(m, o) "
                 "WHERE NOT EXISTS (SELECT 1 FROM jsonb_array_elements(",
                 json.dumps(dedupe(list(value.values)), default=json_default))
            existing_array(key)
            emit(") AS e(v) WHERE e.v = x.m))")
        elif k == "array_remove":
            emit("(SELECT COALESCE(jsonb_agg(e.v ORDER BY e.o), '[]'::jsonb) FROM jsonb_array_elements(")
            existing_array(key)
            emit(") WITH ORDINALITY AS e(v, o) WHERE NOT EXISTS ("
                 "SELECT 1 FROM jsonb_array_elements(%s::jsonb) AS r(v) WHERE r.v = e.v))",
                 json.dumps(list(value.values), default=json_default))
        emit(")")
        pair_count += 1
    emit(")")
    if deletes:
        emit(" - %s::text[]", deletes)
    sql = "".join(f for f, _ in frags)
    params = [p for _, ps in frags for p in ps]
    assert sql.count("%s") == len(params), "transform SQL/param mismatch"
    return sql, params


def _timestamp_column_value(value, data_type: str):
    """Value for a real date/timestamp column, or None to leave it NULL.

    - Offsets are honored: Postgres silently DROPS the offset when a string
      like '13:00+03:00' is cast to `timestamp without time zone` (stores
      13:00, not the 10:00 UTC instant), so conversion happens here, to UTC.
    - Anything that isn't a datetime/date or an ISO-8601 string is skipped
      instead of failing the whole write on a cast error - Firestore accepts
      e.g. created_at: "" without complaint, and the value stays in `data`.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        return value if data_type == "date" else datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    if data_type == "timestamp with time zone":
        return dt
    if data_type == "date":
        return dt.date()
    return dt.replace(tzinfo=None)  # timestamp without time zone holds the UTC wall-clock time
