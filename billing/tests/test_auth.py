import json

import pytest
from django.conf import settings
from django.http import JsonResponse
from django.test import RequestFactory

from billing.auth import require_internal_token, require_project_api_key
from billing.models import Merchant, Project

pytestmark = pytest.mark.django_db

rf = RequestFactory()


def _json(response):
    return json.loads(response.content)


@require_project_api_key
def _dummy_public_view(request):
    return JsonResponse({"project_id": request.project.id})


@require_internal_token
def _dummy_internal_view(request):
    return JsonResponse({"ok": True})


def _make_project(merchant_status=Merchant.Status.ACTIVE, api_key="secret-key"):
    merchant = Merchant.objects.create(name="Acme", status=merchant_status)
    return Project.objects.create(merchant=merchant, name="Shop", api_key=api_key)


def test_valid_api_key_passes_and_attaches_project():
    project = _make_project()

    response = _dummy_public_view(rf.get("/", HTTP_X_API_KEY="secret-key"))

    assert response.status_code == 200
    assert _json(response) == {"project_id": project.id}


def test_missing_api_key_returns_401():
    response = _dummy_public_view(rf.get("/"))

    assert response.status_code == 401
    assert _json(response)["error"]["code"] == "missing_api_key"


def test_unknown_api_key_returns_401():
    _make_project()

    response = _dummy_public_view(rf.get("/", HTTP_X_API_KEY="wrong-key"))

    assert response.status_code == 401
    assert _json(response)["error"]["code"] == "invalid_api_key"


def test_blocked_merchant_returns_403():
    _make_project(merchant_status=Merchant.Status.BLOCKED)

    response = _dummy_public_view(rf.get("/", HTTP_X_API_KEY="secret-key"))

    assert response.status_code == 403
    assert _json(response)["error"]["code"] == "merchant_blocked"


def test_valid_internal_token_passes():
    response = _dummy_internal_view(
        rf.post("/", HTTP_AUTHORIZATION=f"Bearer {settings.INTERNAL_PAYMENTS_TOKEN}")
    )

    assert response.status_code == 200


def test_missing_internal_token_returns_401():
    response = _dummy_internal_view(rf.post("/"))

    assert response.status_code == 401
    assert _json(response)["error"]["code"] == "invalid_internal_token"


def test_wrong_internal_token_returns_401():
    response = _dummy_internal_view(
        rf.post("/", HTTP_AUTHORIZATION="Bearer wrong-token")
    )

    assert response.status_code == 401
    assert _json(response)["error"]["code"] == "invalid_internal_token"