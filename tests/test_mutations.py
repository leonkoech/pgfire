"""Tests for .document().set()/.update()/.delete() behavior."""


def test_set_and_get(db):
    ref = db.collection("test_users").document("u1")
    ref.set({"email": "a@b.com", "user_type": "patient"})
    snap = ref.get()
    assert snap.exists
    assert snap.to_dict()["email"] == "a@b.com"


def test_get_missing_document(db):
    snap = db.collection("test_users").document("does-not-exist").get()
    assert not snap.exists
    assert snap.to_dict() is None


def test_update_merges_fields(db):
    ref = db.collection("test_users").document("u2")
    ref.set({"email": "a@b.com", "user_type": "patient"})
    ref.update({"user_type": "therapist"})
    data = ref.get().to_dict()
    assert data["user_type"] == "therapist"
    assert data["email"] == "a@b.com"  # merge kept the old field


def test_set_without_merge_replaces(db):
    ref = db.collection("test_users").document("u3")
    ref.set({"email": "a@b.com", "user_type": "patient"})
    ref.set({"user_type": "therapist"})
    data = ref.get().to_dict()
    assert data["user_type"] == "therapist"
    assert "email" not in data


def test_delete(db):
    ref = db.collection("test_users").document("u4")
    ref.set({"email": "a@b.com"})
    ref.delete()
    assert not ref.get().exists


def test_set_populates_plain_timestamp_column(db):
    ref = db.collection("test_users").document("u5")
    ref.set({"created_at": "2026-05-01T10:00:00"})
    with db.conn.cursor() as cur:
        cur.execute("SELECT created_at FROM test_users WHERE id = 'u5'")
        row = cur.fetchone()
    assert row[0] is not None
    assert row[0].isoformat() == "2026-05-01T10:00:00"


def test_batch_commits_multiple_ops(db):
    ref1 = db.collection("test_users").document("b1")
    ref1.set({"email": "keep@me.com"})
    ref2 = db.collection("test_users").document("b2")

    batch = db.batch()
    batch.set(ref2, {"email": "new@b.com"})
    batch.delete(ref1)
    batch.commit()

    assert not ref1.get().exists
    assert ref2.get().exists


def test_transaction_commits_on_success(db):
    with db.transaction():
        db.collection("test_users").document("t1").set({"email": "a@b.com"})
    assert db.collection("test_users").document("t1").get().exists


def test_transaction_rolls_back_on_exception(db):
    try:
        with db.transaction():
            db.collection("test_users").document("t2").set({"email": "a@b.com"})
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert not db.collection("test_users").document("t2").get().exists


def test_subcollection_write_persists_parent_id(db):
    sub = db.collection("test_users").document("parent1").collection("notes")
    ref, snap = sub.add({"text": "hello"})
    assert snap.exists
    with db.conn.cursor() as cur:
        cur.execute("SELECT parent_id FROM test_users__notes WHERE id = %s", (ref.id,))
        assert cur.fetchone()[0] == "parent1"
