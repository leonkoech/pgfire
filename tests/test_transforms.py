"""Field transforms: semantics match Firestore, and merge-write transforms
are atomic under real concurrency (separate connections)."""

import os
import threading

import pytest

from pgfire import (DELETE_FIELD, SERVER_TIMESTAMP, ArrayRemove, ArrayUnion,
                    Increment, PostgresClient)
from tests.conftest import _dsn


def _ref(db, doc_id="t"):
    return db.collection("test_users").document(doc_id)


def test_increment_existing_missing_and_non_numeric(db):
    ref = _ref(db)
    ref.set({"n": 5, "s": "text"})
    ref.set({"n": Increment(2), "m": Increment(3), "s": Increment(1)}, merge=True)
    assert ref.get().to_dict() == {"n": 7, "m": 3, "s": 1}  # missing -> n, non-number -> n (Firestore)


def test_increment_float_and_on_insert(db):
    ref = _ref(db, "new")
    ref.set({"cost": Increment(0.5)}, merge=True)
    ref.update({"cost": Increment(0.25)})
    assert ref.get().to_dict() == {"cost": 0.75}


def test_array_union_dedupes_and_keeps_order(db):
    ref = _ref(db)
    ref.set({"a": [1, {"k": "v"}]})
    ref.update({"a": ArrayUnion([{"k": "v"}, 2, 2, {"k": "v", "x": 1}])})
    assert ref.get().to_dict()["a"] == [1, {"k": "v"}, 2, {"k": "v", "x": 1}]


def test_array_union_on_missing_or_non_array(db):
    ref = _ref(db)
    ref.set({"a": "not-an-array"})
    ref.update({"a": ArrayUnion([1, 1]), "b": ArrayUnion(["x"])})
    assert ref.get().to_dict() == {"a": [1], "b": ["x"]}


def test_array_remove(db):
    ref = _ref(db)
    ref.set({"a": [1, 2, 1, {"k": 1}, 3]})
    ref.update({"a": ArrayRemove([1, {"k": 1}, 99])})
    assert ref.get().to_dict()["a"] == [2, 3]


def test_delete_field_and_server_timestamp(db):
    ref = _ref(db)
    ref.set({"keep": 1, "gone": 2, "also_gone": 3})
    ref.update({"gone": DELETE_FIELD, "also_gone": DELETE_FIELD, "stamped": SERVER_TIMESTAMP, "keep": 4})
    d = ref.get().to_dict()
    assert d["keep"] == 4 and "gone" not in d and "also_gone" not in d
    assert d["stamped"].endswith("+00:00")


def test_non_merge_set_resolves_transforms_to_initial_values(db):
    ref = _ref(db)
    ref.set({"old": 1, "n": 10})
    ref.set({"n": Increment(1), "a": ArrayUnion([1, 1]), "r": ArrayRemove([1])})
    assert ref.get().to_dict() == {"n": 1, "a": [1], "r": []}  # replace semantics, like Firestore


def test_nested_transform_rejected(db):
    with pytest.raises(NotImplementedError):
        _ref(db).set({"outer": {"n": Increment(1)}}, merge=True)


def test_google_firestore_sentinels_recognized(db):
    firestore = pytest.importorskip("google.cloud.firestore")
    ref = _ref(db)
    ref.set({"n": 1, "a": [1], "x": 1})
    ref.update({"n": firestore.Increment(1), "a": firestore.ArrayUnion([2]),
                "x": firestore.DELETE_FIELD, "ts": firestore.SERVER_TIMESTAMP})
    d = ref.get().to_dict()
    assert d["n"] == 2 and d["a"] == [1, 2] and "x" not in d and d["ts"].endswith("+00:00")


def _hammer(fn, workers=8, each=10):
    errors = []

    def run():
        client = PostgresClient(_dsn())  # own connection: real concurrency
        try:
            for i in range(each):
                fn(client, i)
        except Exception as e:  # pragma: no cover - surfaced via assert below
            errors.append(e)
        finally:
            client.close()

    threads = [threading.Thread(target=run) for _ in range(workers)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors


def test_concurrent_increments_are_atomic(db):
    _hammer(lambda c, i: c.collection("test_users").document("ctr").set({"n": Increment(1)}, merge=True))
    assert _ref(db, "ctr").get().to_dict()["n"] == 80


def test_concurrent_array_unions_never_drop_elements(db):
    counter = iter(range(10_000))
    lock = threading.Lock()

    def add(c, i):
        with lock:
            v = next(counter)
        c.collection("test_users").document("arr").set({"a": ArrayUnion([v])}, merge=True)

    _hammer(add)
    assert sorted(_ref(db, "arr").get().to_dict()["a"]) == list(range(80))
