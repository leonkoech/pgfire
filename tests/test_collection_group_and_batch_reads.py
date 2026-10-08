"""Tests for the 0.1.5 additions: DocumentSnapshot.reference, client.get_all,
array_contains / array_contains_any, and collection_group."""


def test_snapshot_reference_round_trips(db):
    db.collection("test_users").document("u1").set({"user_type": "patient", "tags": ["a", "b"]})
    snap = db.collection("test_users").where("user_type", "==", "patient").get()[0]
    ref = snap.reference
    assert ref.id == "u1"
    # the reference is usable: update through it, read it back
    ref.update({"tags": ["a", "b", "c"]})
    assert db.collection("test_users").document("u1").get().to_dict()["tags"] == ["a", "b", "c"]
    # a document get() also carries a reference
    assert db.collection("test_users").document("u1").get().reference.id == "u1"


def test_reference_on_subcollection_snapshot(db):
    note = db.collection("test_users").document("u1").collection("notes").document("n1")
    note.set({"body": "hi"})
    snap = db.collection("test_users").document("u1").collection("notes").where("body", "==", "hi").get()[0]
    snap.reference.update({"body": "bye"})
    assert note.get().to_dict()["body"] == "bye"


def test_get_all_returns_input_order_with_missing(db):
    for i in (1, 2, 3):
        db.collection("test_users").document(f"u{i}").set({"n": i})
    refs = [db.collection("test_users").document(x) for x in ("u3", "missing", "u1")]
    snaps = db.get_all(refs)
    assert [s.id for s in snaps] == ["u3", "missing", "u1"]
    assert [s.exists for s in snaps] == [True, False, True]
    assert snaps[0].to_dict()["n"] == 3 and snaps[2].to_dict()["n"] == 1


def test_get_all_subcollection_scoped_by_parent(db):
    # same child id under two different parents must not collide
    db.collection("test_users").document("p1").collection("notes").document("x").set({"owner": "p1"})
    db.collection("test_users").document("p2").collection("notes").document("x").set({"owner": "p2"})
    r1 = db.collection("test_users").document("p1").collection("notes").document("x")
    r2 = db.collection("test_users").document("p2").collection("notes").document("x")
    s1, s2 = db.get_all([r1, r2])
    assert s1.to_dict()["owner"] == "p1"
    assert s2.to_dict()["owner"] == "p2"


def test_array_contains(db):
    db.collection("test_users").document("u1").set({"tags": ["red", "blue"]})
    db.collection("test_users").document("u2").set({"tags": ["green"]})
    got = {s.id for s in db.collection("test_users").where("tags", "array_contains", "red").stream()}
    assert got == {"u1"}
    got = {s.id for s in db.collection("test_users").where("tags", "array_contains", "green").stream()}
    assert got == {"u2"}


def test_array_contains_any(db):
    db.collection("test_users").document("u1").set({"tags": ["red", "blue"]})
    db.collection("test_users").document("u2").set({"tags": ["green"]})
    db.collection("test_users").document("u3").set({"tags": ["yellow"]})
    got = {s.id for s in db.collection("test_users").where("tags", "array_contains_any", ["red", "green"]).stream()}
    assert got == {"u1", "u2"}


def test_collection_group_spans_subcollection_tables(db):
    db.collection("test_users").document("p1").collection("notes").document("n1").set({"kind": "k", "owner": "p1"})
    db.collection("test_users").document("p2").collection("notes").document("n2").set({"kind": "k", "owner": "p2"})
    db.collection("test_users").document("p1").collection("notes").document("n3").set({"kind": "other", "owner": "p1"})
    owners = sorted(s.to_dict()["owner"]
                    for s in db.collection_group("notes").where("kind", "==", "k").stream())
    assert owners == ["p1", "p2"]
    assert db.collection_group("notes").count() == 3
    assert db.collection_group("notes").where("kind", "==", "k").count() == 2
    # a group snapshot's reference points at the right subcollection table
    snap = db.collection_group("notes").where("owner", "==", "p2").get()[0]
    snap.reference.update({"kind": "updated"})
    assert db.collection("test_users").document("p2").collection("notes").document("n2").get().to_dict()["kind"] == "updated"
