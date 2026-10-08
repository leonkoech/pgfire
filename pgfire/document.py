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

    def set(self, data: dict, merge: bool = False) -> None:
        payload = json.dumps(data, default=json_default)
        has_parent_col = "parent_id" in self._client.columns.real_columns(self._table)

        # Plain timestamp columns (created_at, session_date, due_date, ...)
        # aren't GENERATED, so populate any that match a key in `data`
        # ourselves - otherwise they silently stay NULL forever.
        ts_cols = self._client.columns.writable_timestamp_columns(self._table)
        ts_updates = {}
        for col, data_type in ts_cols.items():
            if col in data and data[col] is not None:
                value = _timestamp_column_value(data[col], data_type)
                if value is not None:
                    ts_updates[col] = value

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
                col_values,
            )
        if not self._client._in_transaction:
            self._client.conn.commit()

    def update(self, data: dict) -> None:
        self.set(data, merge=True)

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
