import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from billing import services
from billing.models import ExchangeRate, Invoice, LedgerEntry, Merchant, Project

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


def _internal_headers():
    return {"HTTP_AUTHORIZATION": f"Bearer {settings.INTERNAL_PAYMENTS_TOKEN}"}


def test_payment_in_foreign_currency_is_converted_using_latest_rate_before_received_at(client, project):
    invoice = Invoice.objects.create(
        project=project,
        merchant_external_id="order-1",
        amount="100.00",
        currency="USD",
        expires_at=timezone.now() + timezone.timedelta(days=1),
    )

    old_rate_at = timezone.now() - timezone.timedelta(days=2)
    new_rate_at = timezone.now() - timezone.timedelta(hours=1)
    ExchangeRate.objects.create(currency_from="EUR", currency_to="USD", rate="1.05", effective_at=old_rate_at)
    ExchangeRate.objects.create(currency_from="EUR", currency_to="USD", rate="1.10", effective_at=new_rate_at)

    received_at = timezone.now() - timezone.timedelta(minutes=30)
    payload = {
        "invoice_id": invoice.id,
        "provider_transaction_id": "txn-eur-1",
        "amount": "100.00",
        "currency": "EUR",
        "received_at": received_at.isoformat(),
    }

    response = client.post(
        reverse("internal-payments"), data=json.dumps(payload), content_type="application/json", **_internal_headers()
    )

    assert response.status_code == 201
    # 100 EUR * 1.10 = 110 USD, что на 10% больше суммы счёта - overpaid, не paid.
    assert response.json()["invoice_status"] == Invoice.Status.OVERPAID

    credit = LedgerEntry.objects.get(invoice=invoice, entry_type=LedgerEntry.EntryType.CREDIT)
    assert credit.amount == Decimal("110.00")


def test_payment_uses_rate_effective_at_or_before_received_at_not_later_ones(client, project):
    invoice = Invoice.objects.create(
        project=project,
        merchant_external_id="order-2",
        amount="100.00",
        currency="USD",
        expires_at=timezone.now() + timezone.timedelta(days=1),
    )

    received_at = timezone.now() - timezone.timedelta(days=1)
    ExchangeRate.objects.create(
        currency_from="EUR", currency_to="USD", rate="1.20", effective_at=received_at - timezone.timedelta(hours=1)
    )
    ExchangeRate.objects.create(
        currency_from="EUR", currency_to="USD", rate="1.99", effective_at=received_at + timezone.timedelta(hours=1)
    )

    payload = {
        "invoice_id": invoice.id,
        "provider_transaction_id": "txn-eur-2",
        "amount": "100.00",
        "currency": "EUR",
        "received_at": received_at.isoformat(),
    }

    client.post(
        reverse("internal-payments"), data=json.dumps(payload), content_type="application/json", **_internal_headers()
    )

    credit = LedgerEntry.objects.get(invoice=invoice, entry_type=LedgerEntry.EntryType.CREDIT)
    assert credit.amount == Decimal("120.00")


def test_payment_without_available_exchange_rate_returns_422(client, project):
    invoice = Invoice.objects.create(
        project=project,
        merchant_external_id="order-3",
        amount="100.00",
        currency="USD",
        expires_at=timezone.now() + timezone.timedelta(days=1),
    )
    payload = {
        "invoice_id": invoice.id,
        "provider_transaction_id": "txn-gbp-1",
        "amount": "100.00",
        "currency": "GBP",
    }

    response = client.post(
        reverse("internal-payments"), data=json.dumps(payload), content_type="application/json", **_internal_headers()
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "exchange_rate_unavailable"


def test_partial_foreign_currency_payments_accumulate_towards_invoice_amount(client, project):
    invoice = Invoice.objects.create(
        project=project,
        merchant_external_id="order-4",
        amount="100.00",
        currency="USD",
        expires_at=timezone.now() + timezone.timedelta(days=1),
    )
    ExchangeRate.objects.create(
        currency_from="EUR", currency_to="USD", rate="1.00", effective_at=timezone.now() - timezone.timedelta(days=1)
    )

    first = client.post(
        reverse("internal-payments"),
        data=json.dumps(
            {"invoice_id": invoice.id, "provider_transaction_id": "txn-a", "amount": "40.00", "currency": "EUR"}
        ),
        content_type="application/json",
        **_internal_headers(),
    )
    assert first.json()["invoice_status"] == Invoice.Status.UNDERPAID

    second = client.post(
        reverse("internal-payments"),
        data=json.dumps(
            {"invoice_id": invoice.id, "provider_transaction_id": "txn-b", "amount": "60.00", "currency": "USD"}
        ),
        content_type="application/json",
        **_internal_headers(),
    )
    assert second.json()["invoice_status"] == Invoice.Status.PAID

    response = client.get(reverse("invoice-detail", args=[invoice.id]), HTTP_X_API_KEY="secret-key")
    assert response.json()["remaining_amount"] == "0"


@patch("billing.rates_client.urllib.request.urlopen")
def test_falls_back_to_rates_service_when_no_db_rate_and_persists_snapshot(mock_urlopen, settings):
    settings.RATES_SERVICE_URL = "http://rates.example.com"
    response = MagicMock()
    response.status = 200
    response.read.return_value = b'{"currency_from": "EUR", "currency_to": "USD", "rate": "1.11"}'
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    mock_urlopen.return_value = response

    at = timezone.now()
    rate = services.get_exchange_rate("EUR", "USD", at)

    assert rate == Decimal("1.11")
    assert mock_urlopen.call_count == 1
    assert ExchangeRate.objects.filter(currency_from="EUR", currency_to="USD", effective_at=at).exists()

    rate_again = services.get_exchange_rate("EUR", "USD", at + timezone.timedelta(minutes=1))
    assert rate_again == Decimal("1.11")
    assert mock_urlopen.call_count == 1


def test_raises_when_no_db_rate_and_rates_service_not_configured():
    with pytest.raises(services.ExchangeRateUnavailable):
        services.get_exchange_rate("EUR", "USD", timezone.now())


@patch("billing.rates_client.urllib.request.urlopen")
def test_raises_when_no_db_rate_and_rates_service_unreachable(mock_urlopen, settings):
    import urllib.error

    settings.RATES_SERVICE_URL = "http://rates.example.com"
    mock_urlopen.side_effect = urllib.error.URLError("connection refused")

    with pytest.raises(services.ExchangeRateUnavailable):
        services.get_exchange_rate("EUR", "USD", timezone.now())