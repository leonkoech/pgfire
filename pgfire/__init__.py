"""
pgfire - a Firestore-shaped client backed by Postgres.

Keeps the chainable `.collection(x).document(y).get()` / `.where().limit().stream()`
developer experience of the Firestore Python SDK, running on plain
Postgres tables (JSONB `data` column + optional promoted GENERATED
columns for fast indexed filtering).

Built as a migration bridge for one specific, real cutover (see README's
"Scope & Intention") - not a general-purpose ORM. Supported: .collection(),
.document(), .get()/.set()/.update()/.delete(), .where()/.order_by()/.limit(),
.select() projections, .stream()/.get(), .count()/.aggregate(),
subcollections (composable to any depth), .batch() (atomic), transaction(),
and the field transforms Increment / ArrayUnion / ArrayRemove /
SERVER_TIMESTAMP / DELETE_FIELD (pgfire's own or google-cloud-firestore's).
Not implemented: array_contains, cursor-based pagination
(start_after/start_at), collection_group, Maximum/Minimum - audit your own
call sites before assuming parity on anything not listed here.
"""

from .client import PostgresClient
from .collection import CollectionRef, Query, WriteBatch
from .document import DocumentRef, DocumentSnapshot, NotFound
from .transforms import DELETE_FIELD, SERVER_TIMESTAMP, ArrayRemove, ArrayUnion, Increment

__all__ = [
    "PostgresClient",
    "CollectionRef",
    "Query",
    "WriteBatch",
    "DocumentRef",
    "DocumentSnapshot",
    "NotFound",
    "Increment",
    "ArrayUnion",
    "ArrayRemove",
    "SERVER_TIMESTAMP",
    "DELETE_FIELD",
]

__version__ = "0.1.4"
