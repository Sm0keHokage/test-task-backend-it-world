import json
from decimal import Decimal

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from billing.models import Invoice, LedgerEntry, Merchant, Project

pytestmark = pytest.mark.django_db


@pytest.fixture
def merchant():
    return Merchant.objects.create(name="Acme")


@pytest.fixture
def project(merchant):
    return Project.objects.create(merchant=merchant, name="Shop", api_key="secret-key")


@pytest.fixture
def client():
    return Client()


def _headers(api_key="secret-key"):
    return {"HTTP_X_API_KEY": api_key}


def _internal_headers():
    return {"HTTP_AUTHORIZATION": f"Bearer {settings.INTERNAL_PAYMENTS_TOKEN}"}


def test_create_invoice_is_idempotent(client, project):
    payload = {"merchant_external_id": "order-1", "amount": "100.00", "currency": "USD"}

    first = client.post(
        reverse("invoice-create"), data=json.dumps(payload), content_type="application/json", **_headers()
    )
    second = client.post(
        reverse("invoice-create"), data=json.dumps(payload), content_type="application/json", **_headers()
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert Invoice.objects.count() == 1


def test_create_invoice_requires_api_key(client):
    payload = {"merchant_external_id": "order-1", "amount": "100.00", "currency": "USD"}

    response = client.post(reverse("invoice-create"), data=json.dumps(payload), content_type="application/json")

    assert response.status_code == 401


def test_create_invoice_rejects_non_positive_amount(client, project):
    payload = {"merchant_external_id": "order-1", "amount": "0", "currency": "USD"}

    response = client.post(
        reverse("invoice-create"), data=json.dumps(payload), content_type="application/json", **_headers()
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


def test_invoice_detail_shows_remaining_amount(client, project):
    invoice = Invoice.objects.create(
        project=project,
        merchant_external_id="order-1",
        amount="100.00",
        currency="USD",
        expires_at=timezone.now() + timezone.timedelta(days=1),
    )

    response = client.get(reverse("invoice-detail", args=[invoice.id]), **_headers())

    assert response.status_code == 200
    body = response.json()
    assert body["remaining_amount"] == "100.00"
    assert body["payments"] == []


def test_invoice_detail_is_scoped_to_owning_project(client, merchant):
    other_project = Project.objects.create(merchant=merchant, name="Other", api_key="other-key")
    invoice = Invoice.objects.create(
        project=other_project,
        merchant_external_id="order-1",
        amount="100.00",
        currency="USD",
        expires_at=timezone.now() + timezone.timedelta(days=1),
    )

    response = client.get(
        reverse("invoice-detail", args=[invoice.id]), HTTP_X_API_KEY="secret-key-does-not-exist"
    )

    assert response.status_code == 401


def test_cancel_new_invoice(client, project):
    invoice = Invoice.objects.create(
        project=project,
        merchant_external_id="order-1",
        amount="100.00",
        currency="USD",
        expires_at=timezone.now() + timezone.timedelta(days=1),
    )

    response = client.post(reverse("invoice-cancel", args=[invoice.id]), **_headers())

    assert response.status_code == 200
    assert response.json()["status"] == Invoice.Status.CANCELLED


def test_cancel_paid_invoice_is_rejected(client, project):
    invoice = Invoice.objects.create(
        project=project,
        merchant_external_id="order-1",
        amount="100.00",
        currency="USD",
        status=Invoice.Status.PAID,
        expires_at=timezone.now() + timezone.timedelta(days=1),
    )

    response = client.post(reverse("invoice-cancel", args=[invoice.id]), **_headers())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_transition"


def test_internal_payment_full_amount_marks_invoice_paid_and_settles_ledger(client, project):
    invoice = Invoice.objects.create(
        project=project,
        merchant_external_id="order-1",
        amount="100.00",
        currency="USD",
        expires_at=timezone.now() + timezone.timedelta(days=1),
    )
    payload = {
        "invoice_id": invoice.id,
        "provider_transaction_id": "txn-1",
        "amount": "100.00",
        "currency": "USD",
    }

    response = client.post(
        reverse("internal-payments"), data=json.dumps(payload), content_type="application/json", **_internal_headers()
    )

    assert response.status_code == 201
    assert response.json()["invoice_status"] == Invoice.Status.PAID

    credit = LedgerEntry.objects.get(invoice=invoice, entry_type=LedgerEntry.EntryType.CREDIT)
    commission = LedgerEntry.objects.get(invoice=invoice, entry_type=LedgerEntry.EntryType.COMMISSION)
    assert credit.amount == 100
    assert commission.amount == -1  # max(1% * 100, 0.50) = 1.00


def test_internal_payment_partial_amount_marks_invoice_underpaid(client, project):
    invoice = Invoice.objects.create(
        project=project,
        merchant_external_id="order-1",
        amount="100.00",
        currency="USD",
        expires_at=timezone.now() + timezone.timedelta(days=1),
    )
    payload = {
        "invoice_id": invoice.id,
        "provider_transaction_id": "txn-1",
        "amount": "40.00",
        "currency": "USD",
    }

    response = client.post(
        reverse("internal-payments"), data=json.dumps(payload), content_type="application/json", **_internal_headers()
    )

    assert response.json()["invoice_status"] == Invoice.Status.UNDERPAID
    assert not LedgerEntry.objects.filter(invoice=invoice).exists()


def test_internal_payment_duplicate_transaction_is_idempotent(client, project):
    invoice = Invoice.objects.create(
        project=project,
        merchant_external_id="order-1",
        amount="100.00",
        currency="USD",
        expires_at=timezone.now() + timezone.timedelta(days=1),
    )
    payload = {
        "invoice_id": invoice.id,
        "provider_transaction_id": "txn-1",
        "amount": "100.00",
        "currency": "USD",
    }

    first = client.post(
        reverse("internal-payments"), data=json.dumps(payload), content_type="application/json", **_internal_headers()
    )
    second = client.post(
        reverse("internal-payments"), data=json.dumps(payload), content_type="application/json", **_internal_headers()
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["payment_id"] == second.json()["payment_id"]
    assert LedgerEntry.objects.filter(invoice=invoice, entry_type=LedgerEntry.EntryType.CREDIT).count() == 1


def test_internal_payment_requires_internal_token(client, project):
    invoice = Invoice.objects.create(
        project=project,
        merchant_external_id="order-1",
        amount="100.00",
        currency="USD",
        expires_at=timezone.now() + timezone.timedelta(days=1),
    )
    payload = {"invoice_id": invoice.id, "provider_transaction_id": "txn-1", "amount": "100.00", "currency": "USD"}

    response = client.post(reverse("internal-payments"), data=json.dumps(payload), content_type="application/json")

    assert response.status_code == 401


def test_merchant_balance_reflects_ledger(client, project, merchant):
    invoice = Invoice.objects.create(
        project=project,
        merchant_external_id="order-1",
        amount="100.00",
        currency="USD",
        expires_at=timezone.now() + timezone.timedelta(days=1),
    )
    client.post(
        reverse("internal-payments"),
        data=json.dumps(
            {"invoice_id": invoice.id, "provider_transaction_id": "txn-1", "amount": "100.00", "currency": "USD"}
        ),
        content_type="application/json",
        **_internal_headers(),
    )

    response = client.get(reverse("merchant-balance", args=[merchant.id]), **_headers())

    assert response.status_code == 200
    assert Decimal(response.json()["balance"]["USD"]) == Decimal("99.00")


def test_merchant_balance_forbidden_for_other_merchant(client, project):
    other_merchant = Merchant.objects.create(name="Other")

    response = client.get(reverse("merchant-balance", args=[other_merchant.id]), **_headers())

    assert response.status_code == 403