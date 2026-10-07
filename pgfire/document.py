"""Individual document reads, writes, and the snapshot object returned by them."""

from __future__ import annotations

import json
import uuid
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
        with self._client.conn.cursor() as cur:
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
        ts_updates = {col: data[col] for col in ts_cols if col in data and data[col] is not None}

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

        with self._client.conn.cursor() as cur:
            cur.execute(
                f'INSERT INTO "{self._table}" ({", ".join(col_names)}) '
                f'VALUES ({", ".join(value_placeholders)}) '
                f'ON CONFLICT (id) DO UPDATE SET {", ".join(merge_clause_parts)}',
                col_values,
            )
        if not self._client._in_transaction:
            self._client.conn.commit()

    def update(self, data: dict) -> None:
        self.set(data, merge=True)

    def delete(self) -> None:
        with self._client.conn.cursor() as cur:
            cur.execute(f'DELETE FROM "{self._table}" WHERE id = %s', (self.id,))
        if not self._client._in_transaction:
            self._client.conn.commit()


def new_id() -> str:
    return str(uuid.uuid4())
