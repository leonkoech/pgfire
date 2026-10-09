"""Tests for chainable .where() operators, JSONB fallback parsing,
.order_by()/.limit(), .count(), and .aggregate()."""


def _seed(db):
    db.collection("test_users").document("p1").set(
        {"email": "p1@x.com", "user_type": "patient", "is_demo": True, "company_data": {"company_id": "c1"}}
    )
    db.collection("test_users").document("p2").set(
        {"email": "p2@x.com", "user_type": "patient", "is_demo": False, "company_data": {"company_id": "c1"}}
    )
    db.collection("test_users").document("t1").set(
        {"email": "t1@x.com", "user_type": "therapist", "is_demo": True}
    )


def test_where_on_promoted_column(db):
    _seed(db)
    results = list(db.collection("test_users").where("user_type", "==", "patient").stream())
    ids = {r.id for r in results}
    assert ids == {"p1", "p2"}


def test_where_combines_with_and(db):
    _seed(db)
    results = list(
        db.collection("test_users").where("user_type", "==", "patient").where("is_demo", "==", True).stream()
    )
    assert [r.id for r in results] == ["p1"]


def test_where_jsonb_fallback_dotted_path(db):
    _seed(db)
    results = list(db.collection("test_users").where("company_data.company_id", "==", "c1").stream())
    ids = {r.id for r in results}
    assert ids == {"p1", "p2"}


def test_where_not_equal_none_uses_is_not_null(db):
    _seed(db)
    # company_data.company_id is absent for t1
    results = list(db.collection("test_users").where("company_data.company_id", "!=", None).stream())
    ids = {r.id for r in results}
    assert ids == {"p1", "p2"}


def test_order_by_and_limit(db):
    _seed(db)
    results = list(db.collection("test_users").order_by("email").limit(1).stream())
    assert len(results) == 1
    assert results[0].to_dict()["email"] == "p1@x.com"


def test_count(db):
    _seed(db)
    assert db.collection("test_users").where("user_type", "==", "patient").count() == 2


def test_aggregate_with_tuple_filters(db):
    _seed(db)
    result = db.collection("test_users").aggregate(
        total="TRUE",
        patients=("user_type", "==", "patient"),
        therapists=("user_type", "==", "therapist"),
    )
    assert result == {"total": 3, "patients": 2, "therapists": 1}


def test_jsonb_datetime_filter_matches_same_day_boundary(db):
    """A datetime compared against a JSONB (non-promoted) timestamp must be
    serialized the same way it was written (isoformat, "T" separator).
    str(datetime) uses a space, and " " < "T", so a same-day >= filter
    would wrongly exclude a doc stamped later that same day."""
    from datetime import datetime, timezone

    stamped = datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc)
    db.collection("test_users").document("d1").set({"joined_at": stamped})
    since = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)
    until = datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc)
    hits = list(
        db.collection("test_users").where("joined_at", ">=", since).where("joined_at", "<=", until).stream()
    )
    assert [h.id for h in hits] == ["d1"]


def _seed_numbers(db):
    for doc_id, n in (("n0", 0), ("n100", 100), ("n20", 20), ("n5", 5.5)):
        db.collection("test_users").document(doc_id).set({"start_index": n, "label": doc_id})


def test_order_by_jsonb_number_is_numeric_not_lexical(db):
    """Text ordering would give 0, 100, 20, 5.5."""
    _seed_numbers(db)
    rows = list(db.collection("test_users").order_by("start_index").stream())
    assert [r.to_dict()["start_index"] for r in rows] == [0, 5.5, 20, 100]


def test_numeric_range_filter_on_jsonb_is_numeric(db):
    """Lexically "100" < "9" and "20" < "9"; numerically both are > 9."""
    _seed_numbers(db)
    hits = {r.id for r in db.collection("test_users").where("start_index", ">", 9).stream()}
    assert hits == {"n20", "n100"}


def test_numeric_equality_matches_int_and_float(db):
    db.collection("test_users").document("f1").set({"score": 5.0})
    assert [r.id for r in db.collection("test_users").where("score", "==", 5).stream()] == ["f1"]


def test_order_by_jsonb_string_unchanged(db):
    for doc_id, ts in (("a", "2026-10-08T15:00:00+00:00"), ("b", "2026-01-01T00:00:00+00:00"), ("c", "2026-10-08T09:00:00+00:00")):
        db.collection("test_users").document(doc_id).set({"ts": ts})
    assert [r.id for r in db.collection("test_users").order_by("ts").stream()] == ["b", "c", "a"]


def test_aggregate_numeric_and_in_filters(db):
    _seed_numbers(db)
    result = db.collection("test_users").aggregate(
        big=("start_index", ">=", 20),
        named=("label", "in", ["n0", "n5"]),
    )
    assert result == {"big": 2, "named": 2}


def test_aggregate_with_raw_sql_filter(db):
    _seed(db)
    result = db.collection("test_users").aggregate(
        demo_patients="user_type = 'patient' AND is_demo = true",
    )
    assert result == {"demo_patients": 1}


def test_select_projects_fields_and_omits_missing(db):
    db.collection("test_users").document("s1").set({"user_type": "patient", "email": "s1@x.com", "amount": 5, "bio": "x" * 500})
    db.collection("test_users").document("s2").set({"user_type": "patient", "email": "s2@x.com"})
    db.collection("test_users").document("s3").set({"user_type": "therapist", "email": "s3@x.com", "amount": 9})
    rows = list(
        db.collection("test_users").where("user_type", "==", "patient")
        .select(["email", "amount"]).order_by("email").limit(5).stream()
    )
    assert [(r.id, r.to_dict()) for r in rows] == [
        ("s1", {"email": "s1@x.com", "amount": 5}),
        ("s2", {"email": "s2@x.com"}),  # missing field absent, not None (Firestore semantics)
    ]
    assert rows[1].to_dict().get("amount", 0) == 0


def test_select_rejects_dotted_paths(db):
    import pytest
    with pytest.raises(NotImplementedError):
        db.collection("test_users").select(["company_data.company_id"])


def test_where_accepts_fieldfilter_kwarg(db):
    """Firestore's newer .where(filter=FieldFilter(...)) form (used across the
    app's storage.py modules) must work, not just positional field/op/value."""
    from google.cloud.firestore_v1.base_query import FieldFilter
    db.collection("test_users").document("a").set({"user_type": "patient", "n": 1})
    db.collection("test_users").document("b").set({"user_type": "admin", "n": 2})
    got = {s.id for s in db.collection("test_users")
           .where(filter=FieldFilter("user_type", "==", "patient")).stream()}
    assert got == {"a"}
    # chained with positional still works
    got2 = {s.id for s in db.collection("test_users")
            .where(filter=FieldFilter("n", ">", 0)).where("user_type", "==", "admin").stream()}
    assert got2 == {"b"}
