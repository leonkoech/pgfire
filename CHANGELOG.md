# Changelog

## 0.1.5

New Firestore API surface, needed to run a whole app through pgfire (not just
the hand-ported hot paths). Each has regression tests.

- **`DocumentSnapshot.reference`** — snapshots from a query `stream()`/`get()`
  and from a document `get()` now carry a usable `DocumentRef`, so
  `snap.reference.update(...)` / `.delete()` / `.collection(...)` work as in
  Firestore.
- **`PostgresClient.get_all(references)`** — batched read: one query per
  distinct table, snapshots returned in the same order as the input (a missing
  document is a snapshot with `.exists == False`). Subcollection refs are
  scoped by `(parent_id, id)`. Replaces the N+1 `.get()` loop.
- **`PostgresClient.collection_group(name)`** — queries the top-level table
  `name` and every subcollection table `*__name` together (UNION ALL).
  Supports `where` / `order_by` / `limit` / `stream` / `get` / `count`;
  snapshots carry the right `.reference`. Filtering/ordering use JSONB
  expressions (the member tables are heterogeneous).
- **`where(field, "array_contains", value)`** and **`"array_contains_any"`** —
  JSONB containment (`@>`), matching Firestore's array-membership filters.

## 0.1.4

Found while porting a real ~230-function Firestore data layer onto pgfire; each
fix has regression tests that fail on 0.1.3.

### Correctness
- **Timestamps stored as UTC instants.** A `datetime` with a non-UTC offset was
  stored with that offset, so `13:00+03:00` and `10:00+00:00` (the same instant)
  compared as different strings and a UTC range query could miss a document —
  e.g. a booking-overlap check missing an overlapping booking.
- **Real timestamp columns honor offsets.** Postgres drops the offset when
  casting `'13:00+03:00'` to `timestamp without time zone`; values are now
  converted to UTC first. A non-timestamp value under such a key (e.g. `""`)
  no longer fails the whole write.
- **Datetime query parameters** are serialized the way they're stored
  (`str(datetime)` used a space separator, breaking same-day range filters).
- **Numbers compare and sort numerically** on JSONB fields (was lexical:
  `order_by` gave `0, 100, 20`; `where(n, ">", 9)` missed `20`).
- **`update()` raises `NotFound`** on a missing document instead of creating one.
- **`batch().commit()` is atomic** (was applied op by op).
- **Subcollection rows are addressed by `(parent_id, id)`**, so the same child
  id under two parents no longer collides (since 0.1.2; documented here).

### Production robustness
- **Autocommit outside `transaction()`.** Reads no longer leave a long-lived
  connection "idle in transaction" (blocks VACUUM, stalls migrations), and a
  failed statement no longer makes every later query fail with
  `InFailedSqlTransaction`.
- **Automatic reconnect** when the connection has been closed.

### Features
- **Field transforms**: `Increment`, `ArrayUnion`, `ArrayRemove`,
  `SERVER_TIMESTAMP`, `DELETE_FIELD` — pgfire's own or google-cloud-firestore's,
  applied atomically in the write's single SQL statement (verified under
  concurrent connections: no lost increments, no dropped array elements).
- **`Query.select([...])`** projection with Firestore semantics (missing fields
  absent, not `None`).
- `aggregate()` tuple filters support `in`.
