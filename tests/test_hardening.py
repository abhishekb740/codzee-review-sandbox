"""Configurable check-out limit and API throttling."""
import pytest
from rest_framework.test import APIClient
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle

from .conftest import due_in

CHECKOUTS = "/api/v1/checkouts/"


def checkout(api, asset, employee):
    return api.post(CHECKOUTS, {"asset_tag": asset.asset_tag, "employee_code": employee.employee_code,
                                "due_at": due_in()}, format="json")


@pytest.mark.django_db
class TestConfigurableLimit:
    def test_limit_comes_from_settings(self, settings, api, make_asset, make_employee):
        settings.ASSET_MAX_OPEN_CHECKOUTS = 1
        emp = make_employee()
        assert checkout(api, make_asset(), emp).status_code == 201
        resp = checkout(api, make_asset(), emp)
        assert resp.status_code == 409
        assert "limit 1" in str(resp.data["detail"])

    def test_default_limit_is_three(self, api, make_asset, make_employee):
        emp = make_employee()
        assert [checkout(api, make_asset(), emp).status_code for _ in range(4)] == [201, 201, 201, 409]


@pytest.mark.django_db
class TestThrottling:
    def test_authenticated_user_is_throttled(self, monkeypatch, api):
        monkeypatch.setattr(UserRateThrottle, "THROTTLE_RATES", {"user": "2/min"})
        codes = [api.get("/api/v1/assets/").status_code for _ in range(3)]
        assert codes == [200, 200, 429]

    def test_token_endpoint_is_throttled_for_anonymous_clients(self, monkeypatch):
        monkeypatch.setattr(AnonRateThrottle, "THROTTLE_RATES", {"anon": "2/min"})
        client = APIClient()
        codes = [client.post("/api/v1/auth/token/", {"username": "x", "password": "wrong"}).status_code
                 for _ in range(3)]
        assert codes == [400, 400, 429]

    def test_health_is_never_throttled(self, monkeypatch):
        monkeypatch.setattr(AnonRateThrottle, "THROTTLE_RATES", {"anon": "1/min"})
        client = APIClient()
        assert {client.get("/api/v1/health/").status_code for _ in range(5)} == {200}
