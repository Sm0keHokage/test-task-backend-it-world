from decimal import Decimal

import pytest
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
def other_project(merchant):
    return Project.objects.create(merchant=merchant, name="Other Shop", api_key="other-key")


@pytest.fixture
def client():
    return Client()


def _day(offset=0):
    return timezone.now() + timezone.timedelta(days=offset)


def _make_invoice(project, status, created_at, amount="100.00", external_id="order"):
    invoice = Invoice.objects.create(
        project=project,
        merchant_external_id=external_id,
        amount=Decimal(amount),
        currency="USD",
        status=status,
        expires_at=created_at + timezone.timedelta(days=1),
    )
    Invoice.objects.filter(pk=invoice.pk).update(created_at=created_at)
    invoice.refresh_from_db()
    return invoice


def _settle(invoice, received="100.00", commission="1.00"):
    LedgerEntry.objects.create(
        merchant=invoice.project.merchant,
        invoice=invoice,
        entry_type=LedgerEntry.EntryType.CREDIT,
        amount=Decimal(received),
        currency=invoice.currency,
    )
    LedgerEntry.objects.create(
        merchant=invoice.project.merchant,
        invoice=invoice,
        entry_type=LedgerEntry.EntryType.COMMISSION,
        amount=-Decimal(commission),
        currency=invoice.currency,
    )


def test_report_grouped_by_day(client, project, merchant):
    day0 = _day(0)
    day1 = _day(1)

    paid = _make_invoice(project, Invoice.Status.PAID, day0, external_id="order-1")
    _settle(paid)
    _make_invoice(project, Invoice.Status.UNDERPAID, day0, external_id="order-2")
    _make_invoice(project, Invoice.Status.PAID, day1, external_id="order-3")

    response = client.get(
        reverse("merchant-report", args=[merchant.id]) + "?group_by=day", HTTP_X_API_KEY="secret-key"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["group_by"] == "day"

    by_day = {row["day"]: row for row in body["report"]}
    day0_key = day0.date().isoformat()
    day1_key = day1.date().isoformat()

    assert by_day[day0_key]["issued_count"] == 2
    assert by_day[day0_key]["paid_count"] == 1
    assert Decimal(by_day[day0_key]["issued_amount"]) == Decimal("200.00")
    assert Decimal(by_day[day0_key]["received_amount"]) == Decimal("100.00")
    assert Decimal(by_day[day0_key]["commission_amount"]) == Decimal("1.00")
    assert by_day[day0_key]["conversion"] == 0.5

    assert by_day[day1_key]["issued_count"] == 1
    assert by_day[day1_key]["paid_count"] == 1  # paid, но без settle() -> нет проводок
    assert Decimal(by_day[day1_key]["received_amount"]) == Decimal("0")


def test_report_grouped_by_project(client, project, other_project, merchant):
    today = _day(0)

    invoice_a = _make_invoice(project, Invoice.Status.PAID, today, external_id="order-a")
    _settle(invoice_a)
    _make_invoice(other_project, Invoice.Status.NEW, today, external_id="order-b")

    response = client.get(
        reverse("merchant-report", args=[merchant.id]) + "?group_by=project", HTTP_X_API_KEY="secret-key"
    )

    assert response.status_code == 200
    body = response.json()
    by_project = {row["project_id"]: row for row in body["report"]}

    assert by_project[project.id]["project_name"] == "Shop"
    assert by_project[project.id]["issued_count"] == 1
    assert by_project[project.id]["paid_count"] == 1
    assert Decimal(by_project[project.id]["received_amount"]) == Decimal("100.00")

    assert by_project[other_project.id]["issued_count"] == 1
    assert by_project[other_project.id]["paid_count"] == 0
    assert by_project[other_project.id]["conversion"] == 0.0


def test_report_filters_by_date_range(client, project, merchant):
    old = _day(-10)
    recent = _day(0)

    _make_invoice(project, Invoice.Status.PAID, old, external_id="old-order")
    _make_invoice(project, Invoice.Status.PAID, recent, external_id="recent-order")

    date_from = recent.date().isoformat()
    response = client.get(
        reverse("merchant-report", args=[merchant.id]) + f"?group_by=day&date_from={date_from}",
        HTTP_X_API_KEY="secret-key",
    )

    body = response.json()
    total_issued = sum(row["issued_count"] for row in body["report"])
    assert total_issued == 1


def test_report_rejects_invalid_group_by(client, project, merchant):
    response = client.get(
        reverse("merchant-report", args=[merchant.id]) + "?group_by=year", HTTP_X_API_KEY="secret-key"
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


def test_report_rejects_invalid_date_format(client, project, merchant):
    response = client.get(
        reverse("merchant-report", args=[merchant.id]) + "?date_from=not-a-date", HTTP_X_API_KEY="secret-key"
    )

    assert response.status_code == 400


def test_report_forbidden_for_other_merchant(client, project):
    other_merchant = Merchant.objects.create(name="Other")

    response = client.get(reverse("merchant-report", args=[other_merchant.id]), HTTP_X_API_KEY="secret-key")

    assert response.status_code == 403