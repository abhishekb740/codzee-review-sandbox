"""POST /checkouts/{id}/extend/: move an open check-out's due date later."""
from datetime import timedelta

import pytest
from django.utils import timezone

from assets.models import CheckOut


def extend_url(checkout_id):
    return f"/api/v1/checkouts/{checkout_id}/extend/"


@pytest.mark.django_db
class TestExtend:
    def _open_checkout(self, make_asset, make_employee, make_checkout, due_in_days=2):
        now = timezone.now()
        return make_checkout(make_asset(), make_employee(), checked_out_at=now,
                             due_at=now + timedelta(days=due_in_days))

    def test_extends_an_open_checkout(self, api, make_asset, make_employee, make_checkout):
        co = self._open_checkout(make_asset, make_employee, make_checkout)
        new_due = timezone.now() + timedelta(days=10)
        resp = api.post(extend_url(co.id), {"due_at": new_due.isoformat()}, format="json")
        assert resp.status_code == 200, resp.data
        co.refresh_from_db()
        assert abs((co.due_at - new_due).total_seconds()) < 1

    def test_new_due_must_be_later_than_current(self, api, make_asset, make_employee, make_checkout):
        co = self._open_checkout(make_asset, make_employee, make_checkout, due_in_days=5)
        earlier = timezone.now() + timedelta(days=3)
        resp = api.post(extend_url(co.id), {"due_at": earlier.isoformat()}, format="json")
        assert resp.status_code == 400
        assert "due_at" in resp.data

    @pytest.mark.parametrize("delta", [timedelta(seconds=-1), timedelta(days=30, minutes=1)])
    def test_rule4_applies_to_the_new_due_date(self, api, make_asset, make_employee, make_checkout, delta):
        co = self._open_checkout(make_asset, make_employee, make_checkout)
        resp = api.post(extend_url(co.id), {"due_at": (timezone.now() + delta).isoformat()}, format="json")
        assert resp.status_code == 400

    def test_returned_checkout_cannot_be_extended(self, api, make_asset, make_employee, make_checkout):
        now = timezone.now()
        co = make_checkout(make_asset(), make_employee(), checked_out_at=now - timedelta(days=3),
                           due_at=now + timedelta(days=1), returned_at=now - timedelta(hours=1))
        resp = api.post(extend_url(co.id), {"due_at": (now + timedelta(days=5)).isoformat()}, format="json")
        assert resp.status_code == 409

    def test_unknown_checkout_is_404(self, api):
        resp = api.post(extend_url(999999), {"due_at": (timezone.now() + timedelta(days=5)).isoformat()},
                        format="json")
        assert resp.status_code == 404

    def test_missing_due_at_is_400(self, api, make_asset, make_employee, make_checkout):
        co = self._open_checkout(make_asset, make_employee, make_checkout)
        assert api.post(extend_url(co.id), {}, format="json").status_code == 400

    def test_requires_authentication(self, make_asset, make_employee, make_checkout):
        from rest_framework.test import APIClient
        co = self._open_checkout(make_asset, make_employee, make_checkout)
        resp = APIClient().post(extend_url(co.id), {"due_at": "2030-01-01T00:00:00Z"}, format="json")
        assert resp.status_code == 401
        assert CheckOut.objects.get(pk=co.pk).due_at == co.due_at
