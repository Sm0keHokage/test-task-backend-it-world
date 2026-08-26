from decimal import Decimal

import pytest
from django.core.management import call_command
from django.utils import timezone

from billing import services
from billing.models import Invoice, Merchant, Project

pytestmark = pytest.mark.django_db


@pytest.fixture
def project():
    merchant = Merchant.objects.create(name="Acme")
    return Project.objects.create(merchant=merchant, name="Shop", api_key="secret-key")


def _make_invoice(project, status, expires_at, external_id):
    return Invoice.objects.create(
        project=project,
        merchant_external_id=external_id,
        amount=Decimal("100.00"),
        currency="USD",
        status=status,
        expires_at=expires_at,
    )


def test_expires_overdue_new_and_underpaid_invoices(project):
    past = timezone.now() - timezone.timedelta(days=1)
    overdue_new = _make_invoice(project, Invoice.Status.NEW, past, "order-1")
    overdue_underpaid = _make_invoice(project, Invoice.Status.UNDERPAID, past, "order-2")

    total = services.expire_overdue_invoices()

    assert total == 2
    overdue_new.refresh_from_db()
    overdue_underpaid.refresh_from_db()
    assert overdue_new.status == Invoice.Status.EXPIRED
    assert overdue_underpaid.status == Invoice.Status.EXPIRED


def test_does_not_touch_invoices_not_yet_expired(project):
    future = timezone.now() + timezone.timedelta(days=1)
    invoice = _make_invoice(project, Invoice.Status.NEW, future, "order-1")

    total = services.expire_overdue_invoices()

    assert total == 0
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.NEW


@pytest.mark.parametrize(
    "status",
    [Invoice.Status.PAID, Invoice.Status.OVERPAID, Invoice.Status.CANCELLED, Invoice.Status.EXPIRED],
)
def test_does_not_touch_already_terminal_invoices(project, status):
    past = timezone.now() - timezone.timedelta(days=1)
    invoice = _make_invoice(project, status, past, "order-1")

    total = services.expire_overdue_invoices()

    assert total == 0
    invoice.refresh_from_db()
    assert invoice.status == status


def test_is_idempotent_on_repeated_runs(project):
    past = timezone.now() - timezone.timedelta(days=1)
    _make_invoice(project, Invoice.Status.NEW, past, "order-1")

    first_run = services.expire_overdue_invoices()
    second_run = services.expire_overdue_invoices()

    assert first_run == 1
    assert second_run == 0


def test_processes_more_records_than_a_single_batch(project):
    past = timezone.now() - timezone.timedelta(days=1)
    for i in range(5):
        _make_invoice(project, Invoice.Status.NEW, past, f"order-{i}")

    total = services.expire_overdue_invoices(batch_size=2)

    assert total == 5
    assert Invoice.objects.filter(status=Invoice.Status.EXPIRED).count() == 5


def test_management_command_runs_and_reports_count(project):
    past = timezone.now() - timezone.timedelta(days=1)
    _make_invoice(project, Invoice.Status.NEW, past, "order-1")

    call_command("expire_invoices")

    assert Invoice.objects.get(merchant_external_id="order-1").status == Invoice.Status.EXPIRED