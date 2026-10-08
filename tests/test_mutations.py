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


def test_same_child_id_under_different_parents_does_not_collide(db):
    """A child doc id is only unique WITHIN its parent (same as a Firestore
    subcollection) - e.g. the same calendar event id legitimately exists
    once under a patient's calendar and once under a therapist's. Before
    DocumentRef scoped by (parent_id, id) instead of `id` alone, the second
    parent's write would silently overwrite the first parent's row."""
    note_a = db.collection("test_users").document("parentA").collection("notes").document("shared-id")
    note_b = db.collection("test_users").document("parentB").collection("notes").document("shared-id")

    note_a.set({"text": "from A"})
    note_b.set({"text": "from B"})

    assert note_a.get().to_dict()["text"] == "from A"
    assert note_b.get().to_dict()["text"] == "from B"

    with db.conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM test_users__notes WHERE id = %s", ("shared-id",))
        assert cur.fetchone()[0] == 2

    note_a.delete()
    assert not note_a.get().exists
    assert note_b.get().exists  # deleting A's row must not touch B's


def test_flat_collection_lookup_with_no_parent_still_works(db):
    """A DocumentRef built from a flat collection path (no parent chain, so
    parent_id=None) must keep doing a global by-id lookup - filtering on
    "parent_id = NULL" would never match anything under normal SQL NULL
    semantics, which would silently break every existing flat-table lookup
    (e.g. an invite-code table addressed directly by code, parent company
    unknown until after the read). parent_id is NOT NULL on this table, so
    the row is seeded via raw SQL rather than ref.set() - matching how real
    callers only ever .get()/.delete() through the flat path, never .set()."""
    with db.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO test_users__notes (id, parent_id, data) VALUES (%s, %s, %s::jsonb)",
            ("flat-id", "some-real-parent", '{"text": "flat"}'),
        )
    db.conn.commit()

    ref = db.collection("test_users__notes").document("flat-id")
    assert ref.get().to_dict()["text"] == "flat"
    ref.delete()
    assert not ref.get().exists


def test_read_does_not_leave_connection_idle_in_transaction(db):
    """A long-lived client sitting 'idle in transaction' after every read
    blocks VACUUM and holds locks that stall migrations."""
    import psycopg2.extensions as ext

    db.collection("test_users").document("missing").get()
    assert db.conn.info.transaction_status == ext.TRANSACTION_STATUS_IDLE


def test_failed_query_does_not_poison_later_queries(db):
    """Without autocommit, one failed statement aborts the open transaction
    and every later query fails with InFailedSqlTransaction."""
    import psycopg2

    try:
        db.collection("no_such_table_pgfire_test").document("x").get()
    except psycopg2.Error:
        pass
    db.collection("test_users").document("ok").set({"email": "ok@x.com"})
    assert db.collection("test_users").document("ok").get().exists


def test_batch_commit_is_all_or_nothing(db):
    """Firestore WriteBatch is atomic; a failure midway must not leave the
    earlier ops applied."""
    batch = db.batch()
    batch.set(db.collection("test_users").document("b1"), {"email": "b1@x.com"})
    batch.set(db.collection("no_such_table_pgfire_test").document("b2"), {"x": 1})
    try:
        batch.commit()
    except Exception:
        pass
    assert not db.collection("test_users").document("b1").get().exists


def test_reconnects_after_connection_closed(db):
    db.conn.close()
    db.collection("test_users").document("r1").set({"email": "r1@x.com"})
    assert db.collection("test_users").document("r1").get().exists


def test_transaction_still_rolls_back_with_autocommit_default(db):
    try:
        with db.transaction():
            db.collection("test_users").document("tx1").set({"email": "tx@x.com"})
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert not db.collection("test_users").document("tx1").get().exists
    assert db.conn.autocommit is True


def test_offset_datetime_stored_as_utc_and_range_query_finds_it(db):
    """13:00+03:00 is 10:00Z. Stored with its own offset, a UTC range query
    (as a booking-overlap check builds it) missed it - a double-booking risk."""
    from datetime import datetime, timedelta, timezone

    eat = timezone(timedelta(hours=3))
    ref = db.collection("test_users").document("tz1")
    ref.set({"start_time": datetime(2026, 10, 20, 13, 0, tzinfo=eat)})
    assert ref.get().to_dict()["start_time"] == "2026-10-20T10:00:00+00:00"
    lo = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)
    hi = datetime(2026, 10, 20, 11, 0, tzinfo=timezone.utc)
    hits = list(db.collection("test_users").where("start_time", ">=", lo).where("start_time", "<=", hi).stream())
    assert [h.id for h in hits] == ["tz1"]
    # and querying with a non-UTC param works too
    hits = list(db.collection("test_users").where("start_time", "==", datetime(2026, 10, 20, 13, 0, tzinfo=eat)).stream())
    assert [h.id for h in hits] == ["tz1"]


def test_real_timestamp_column_honors_string_offset(db):
    """Postgres drops the offset when casting '13:00+03:00' to a timestamp
    without time zone; the column must hold the 10:00 UTC instant."""
    db.collection("test_users").document("tz2").set({"created_at": "2026-10-20T13:00:00+03:00"})
    with db.conn.cursor() as cur:
        cur.execute("SELECT created_at FROM test_users WHERE id = 'tz2'")
        assert str(cur.fetchone()[0]) == "2026-10-20 10:00:00"


def test_unparseable_timestamp_value_does_not_fail_write(db):
    db.collection("test_users").document("tz3").set({"created_at": "", "email": "tz3@x.com"})
    snap = db.collection("test_users").document("tz3").get()
    assert snap.exists and snap.to_dict()["created_at"] == ""
