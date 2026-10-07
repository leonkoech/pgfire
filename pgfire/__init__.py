"""
pgfire - a Firestore-shaped client backed by Postgres.

Keeps the chainable `.collection(x).document(y).get()` / `.where().limit().stream()`
developer experience of the Firestore Python SDK, running on plain
Postgres tables (JSONB `data` column + optional promoted GENERATED
columns for fast indexed filtering).

Built as a migration bridge for one specific, real cutover (see README's
"Scope & Intention") - not a general-purpose ORM. Supported: .collection(),
.document(), .get()/.set()/.update()/.delete(), .where()/.order_by()/.limit(),
.stream()/.get(), .count()/.aggregate(), subcollections (composable to any
depth), .batch(), transaction(). Not implemented: array_contains,
cursor-based pagination (start_after/start_at) - audit your own call sites
before assuming parity on anything not listed here.
"""

from .client import PostgresClient
from .collection import CollectionRef, Query, WriteBatch
from .document import DocumentRef, DocumentSnapshot

__all__ = [
    "PostgresClient",
    "CollectionRef",
    "Query",
    "WriteBatch",
    "DocumentRef",
    "DocumentSnapshot",
]

__version__ = "0.0.2"
