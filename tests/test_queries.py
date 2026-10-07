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


def test_aggregate_with_raw_sql_filter(db):
    _seed(db)
    result = db.collection("test_users").aggregate(
        demo_patients="user_type = 'patient' AND is_demo = true",
    )
    assert result == {"demo_patients": 1}
