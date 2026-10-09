"""Overdue calculation, overdue report, employee summary, asset list/detail, health."""
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from assets.models import Asset
from assets.selectors import days_overdue, employee_summary, overdue_checkouts


@pytest.mark.django_db
class TestOverdueCalculation:
    def test_due_exactly_now_is_not_overdue_but_one_microsecond_earlier_is(
        self, make_asset, make_employee, make_checkout
    ):
        now = timezone.now()
        emp = make_employee()
        due_now = make_checkout(make_asset(), emp, checked_out_at=now - timedelta(days=1), due_at=now)
        just_past = make_checkout(
            make_asset(), emp, checked_out_at=now - timedelta(days=1), due_at=now - timedelta(microseconds=1)
        )
        ids = set(overdue_checkouts(now).values_list("id", flat=True))
        assert due_now.id not in ids
        assert just_past.id in ids

    def test_returned_items_are_never_overdue(self, make_asset, make_employee, make_checkout):
        now = timezone.now()
        make_checkout(make_asset(), make_employee(), checked_out_at=now - timedelta(days=10),
                      due_at=now - timedelta(days=5), returned_at=now - timedelta(days=1))
        assert not overdue_checkouts(now).exists()

    @pytest.mark.parametrize(
        "late_by,expected",
        [(timedelta(minutes=5), 0), (timedelta(days=1), 1), (timedelta(days=2, hours=23), 2)],
    )
    def test_days_overdue_is_whole_days_floored(self, late_by, expected):
        now = timezone.now()
        assert days_overdue(now - late_by, now) == expected


@pytest.mark.django_db
class TestOverdueReport:
    def test_rows_fields_and_order(self, api, make_asset, make_employee, make_checkout):
        now = timezone.now()
        emp = make_employee(full_name="Asha Rao")
        a1, a2, a3 = make_asset(name="Drone"), make_asset(name="Laptop"), make_asset(name="Camera")
        make_checkout(a1, emp, checked_out_at=now - timedelta(days=10), due_at=now - timedelta(days=2))
        make_checkout(a2, emp, checked_out_at=now - timedelta(days=10), due_at=now - timedelta(days=5))
        make_checkout(a3, emp, checked_out_at=now, due_at=now + timedelta(days=5))  # not overdue

        resp = api.get("/api/v1/reports/overdue/")
        assert resp.status_code == 200
        rows = resp.data["results"]
        assert [r["asset_name"] for r in rows] == ["Laptop", "Drone"]  # most overdue first
        assert rows[0] == {
            "checkout_id": rows[0]["checkout_id"],
            "asset_name": "Laptop",
            "asset_tag": a2.asset_tag,
            "employee_code": emp.employee_code,
            "employee_name": "Asha Rao",
            "due_at": rows[0]["due_at"],
            "days_overdue": 5,
        }

    def test_query_count_does_not_grow_with_rows(
        self, api, make_asset, make_employee, make_checkout, django_assert_max_num_queries
    ):
        now = timezone.now()
        for i in range(15):
            emp = make_employee()
            make_checkout(make_asset(), emp, checked_out_at=now - timedelta(days=20),
                          due_at=now - timedelta(days=i + 1))
        # auth + page count + page rows; must not be one query per row
        with django_assert_max_num_queries(3):
            resp = api.get("/api/v1/reports/overdue/")
        assert resp.data["count"] == 15

    def test_paginated_at_20(self, api, make_asset, make_employee, make_checkout):
        now = timezone.now()
        for _ in range(21):
            make_checkout(make_asset(), make_employee(), checked_out_at=now - timedelta(days=3),
                          due_at=now - timedelta(days=1))
        resp = api.get("/api/v1/reports/overdue/")
        assert resp.data["count"] == 21
        assert len(resp.data["results"]) == 20
        assert resp.data["next"] is not None


@pytest.mark.django_db
class TestEmployeeSummary:
    def test_four_numbers_against_known_data(self, make_asset, make_employee, make_checkout):
        now = timezone.now()
        emp, other = make_employee(), make_employee()
        # returned, held 2 days
        make_checkout(make_asset(), emp, checked_out_at=now - timedelta(days=10),
                      due_at=now - timedelta(days=5), returned_at=now - timedelta(days=8))
        # returned, held 4 days
        make_checkout(make_asset(), emp, checked_out_at=now - timedelta(days=6),
                      due_at=now - timedelta(days=1), returned_at=now - timedelta(days=2))
        # open and overdue
        make_checkout(make_asset(), emp, checked_out_at=now - timedelta(days=3), due_at=now - timedelta(days=1))
        # open, not overdue
        make_checkout(make_asset(), emp, checked_out_at=now - timedelta(days=1), due_at=now + timedelta(days=3))
        # noise from another employee
        make_checkout(make_asset(), other, checked_out_at=now - timedelta(days=9), due_at=now - timedelta(days=4))

        summary = employee_summary(emp.employee_code, now)
        assert summary["lifetime_checkouts"] == 4
        assert summary["currently_held"] == 2
        assert summary["currently_overdue"] == 1
        assert summary["mean_hold_days"] == 3.0   # (2 + 4) / 2

    def test_computed_in_a_single_query(self, make_asset, make_employee, make_checkout,
                                        django_assert_num_queries):
        now = timezone.now()
        emp = make_employee()
        make_checkout(make_asset(), emp, checked_out_at=now - timedelta(days=2), due_at=now + timedelta(days=1))
        with django_assert_num_queries(1):
            employee_summary(emp.employee_code, now)

    def test_employee_with_no_history(self, api, make_employee):
        emp = make_employee()
        resp = api.get(f"/api/v1/employees/{emp.employee_code}/summary/")
        assert resp.status_code == 200
        assert resp.data["lifetime_checkouts"] == 0
        assert resp.data["currently_held"] == 0
        assert resp.data["currently_overdue"] == 0
        assert resp.data["mean_hold_days"] is None

    def test_unknown_employee_is_404(self, api):
        assert api.get("/api/v1/employees/NOPE/summary/").status_code == 404


@pytest.mark.django_db
class TestAssets:
    def test_create_list_filter_search(self, api):
        payload = {"asset_tag": "CAM-1", "name": "Sony A7", "category": "CAMERA", "purchase_date": "2025-02-01"}
        assert api.post("/api/v1/assets/", payload, format="json").status_code == 201
        api.post("/api/v1/assets/", {**payload, "asset_tag": "LAP-1", "name": "ThinkPad", "category": "LAPTOP"},
                 format="json")
        assert api.get("/api/v1/assets/?category=CAMERA").data["count"] == 1
        assert api.get("/api/v1/assets/?status=AVAILABLE").data["count"] == 2
        assert api.get("/api/v1/assets/?search=think").data["results"][0]["asset_tag"] == "LAP-1"
        assert api.get("/api/v1/assets/?search=CAM-").data["count"] == 1

    def test_cannot_create_asset_as_checked_out(self, api):
        payload = {"asset_tag": "X-1", "name": "x", "category": "SENSOR", "purchase_date": "2025-02-01",
                   "status": "CHECKED_OUT"}
        assert api.post("/api/v1/assets/", payload, format="json").status_code == 400

    def test_duplicate_asset_tag_is_400(self, api, make_asset):
        make_asset(asset_tag="DUP")
        payload = {"asset_tag": "DUP", "name": "x", "category": "SENSOR", "purchase_date": "2025-02-01"}
        assert api.post("/api/v1/assets/", payload, format="json").status_code == 400

    def test_current_holder(self, api, make_asset, make_employee, make_checkout):
        now = timezone.now()
        free, held = make_asset(), make_asset()
        emp = make_employee(full_name="Ravi K")
        make_checkout(held, emp, checked_out_at=now, due_at=now + timedelta(days=1))
        assert api.get(f"/api/v1/assets/{free.id}/").data["current_holder"] is None
        assert api.get(f"/api/v1/assets/{held.id}/").data["current_holder"] == {
            "employee_code": emp.employee_code, "full_name": "Ravi K",
        }

    def test_unknown_asset_id_is_404(self, api):
        assert api.get("/api/v1/assets/999999/").status_code == 404


@pytest.mark.django_db
def test_health_is_public_and_reports_database():
    resp = APIClient().get("/api/v1/health/")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok"}


@pytest.mark.django_db
def test_list_endpoints_require_auth():
    assert APIClient().get("/api/v1/assets/").status_code == 401
    assert Asset.objects.count() == 0



@pytest.mark.django_db
class TestOverdueFilters:
    URL = "/api/v1/reports/overdue/"

    def _overdue(self, make_checkout, asset, employee):
        now = timezone.now()
        return make_checkout(asset, employee, checked_out_at=now - timedelta(days=5),
                             due_at=now - timedelta(days=1))

    def test_filter_by_category(self, api, make_asset, make_employee, make_checkout):
        emp = make_employee()
        cam = self._overdue(make_checkout, make_asset(category=Asset.Category.CAMERA), emp)
        self._overdue(make_checkout, make_asset(category=Asset.Category.LAPTOP), emp)
        resp = api.get(self.URL, {"category": "CAMERA"})
        assert resp.status_code == 200
        assert [r["checkout_id"] for r in resp.data["results"]] == [cam.id]

    def test_filter_by_employee_code(self, api, make_asset, make_employee, make_checkout):
        e1, e2 = make_employee(), make_employee()
        mine = self._overdue(make_checkout, make_asset(), e1)
        self._overdue(make_checkout, make_asset(), e2)
        resp = api.get(self.URL, {"employee_code": e1.employee_code})
        assert [r["checkout_id"] for r in resp.data["results"]] == [mine.id]

    def test_unknown_category_is_400(self, api):
        assert api.get(self.URL, {"category": "SPACESHIP"}).status_code == 400

    def test_filtered_report_is_still_constant_queries(
        self, api, make_asset, make_employee, make_checkout, django_assert_max_num_queries
    ):
        for _ in range(10):
            self._overdue(make_checkout, make_asset(category=Asset.Category.SENSOR), make_employee())
        with django_assert_max_num_queries(3):
            resp = api.get(self.URL, {"category": "SENSOR"})
        assert resp.data["count"] == 10


@pytest.mark.django_db
def test_asset_list_with_holders_is_constant_queries(api, make_asset, make_employee, make_checkout,
                                                    django_assert_max_num_queries):
    now = timezone.now()
    expected = {}
    for _ in range(8):
        asset, emp = make_asset(), make_employee()
        make_checkout(asset, emp, checked_out_at=now, due_at=now + timedelta(days=2))
        expected[asset.asset_tag] = {"employee_code": emp.employee_code, "full_name": emp.full_name}
    free = make_asset()  # one asset with no holder
    expected[free.asset_tag] = None
    # A returned check-out must not show up as the current holder.
    returned = make_asset()
    make_checkout(returned, make_employee(), checked_out_at=now - timedelta(days=3),
                  due_at=now + timedelta(days=1), returned_at=now - timedelta(days=1))
    expected[returned.asset_tag] = None
    with django_assert_max_num_queries(2):  # page count + page rows
        resp = api.get("/api/v1/assets/")
    actual = {a["asset_tag"]: a["current_holder"] for a in resp.data["results"]}
    assert actual == expected


@pytest.mark.django_db
def test_health_failure_does_not_leak_exception_details(monkeypatch, caplog):
    from django.db import OperationalError

    from assets import views

    class BrokenCursor:
        def __enter__(self):
            raise OperationalError("could not connect to server: secret-host:5432")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(views.connection, "cursor", lambda: BrokenCursor())
    with caplog.at_level("ERROR", logger="assets.views"):
        resp = APIClient().get("/api/v1/health/")
    assert resp.status_code == 503
    assert resp.json() == {"status": "error", "database": "unreachable"}
    # The cause is not returned to the caller, but operators still get it.
    assert "secret-host:5432" in caplog.text
