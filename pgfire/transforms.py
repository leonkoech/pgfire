"""Firestore field transforms: Increment, ArrayUnion, ArrayRemove,
SERVER_TIMESTAMP, DELETE_FIELD.

pgfire exposes its own versions, and also recognizes google-cloud-firestore's
(by duck-typing, so there's no dependency on it). That means code written
against Firestore - `ref.set({"n": firestore.Increment(1)}, merge=True)` -
runs unchanged on pgfire.

In merge writes the transforms are compiled into the same single
INSERT ... ON CONFLICT DO UPDATE statement as the rest of the write, so,
as in Firestore, they're applied atomically server-side: two concurrent
Increment(1)s always add 2, and concurrent ArrayUnions never drop an
element. A read-modify-write in application code loses updates under
exactly that concurrency.
"""

from __future__ import annotations

from typing import Any


class Increment:
    def __init__(self, value):
        self.value = value


class ArrayUnion:
    def __init__(self, values):
        self.values = list(values)


class ArrayRemove:
    def __init__(self, values):
        self.values = list(values)


class _Sentinel:
    def __init__(self, description):
        self.description = description

    def __repr__(self):
        return f"Sentinel({self.description!r})"


SERVER_TIMESTAMP = _Sentinel("Value used to set a document field to the server timestamp.")
DELETE_FIELD = _Sentinel("Value used to delete a field in a document.")


def kind(value: Any):
    """'increment' | 'array_union' | 'array_remove' | 'server_timestamp' |
    'delete' | None. Recognizes pgfire's and google-cloud-firestore's classes."""
    name = type(value).__name__
    if name == "Increment" and hasattr(value, "value"):
        return "increment"
    if name == "ArrayUnion" and hasattr(value, "values"):
        return "array_union"
    if name == "ArrayRemove" and hasattr(value, "values"):
        return "array_remove"
    description = getattr(value, "description", None)
    if isinstance(description, str):
        if "server timestamp" in description:
            return "server_timestamp"
        if "delete a field" in description:
            return "delete"
    if name in ("Maximum", "Minimum") and hasattr(value, "value"):
        raise NotImplementedError(f"pgfire does not support the {name} transform")
    return None


def contains_nested_transform(value: Any) -> bool:
    if isinstance(value, dict):
        return any(kind(v) or contains_nested_transform(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(kind(v) or contains_nested_transform(v) for v in value)
    return False


def dedupe(values: list) -> list:
    out = []
    for v in values:
        if v not in out:
            out.append(v)
    return out
