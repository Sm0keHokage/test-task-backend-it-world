import threading
from decimal import Decimal

import pytest
from django.db import connection, connections
from django.utils import timezone

from billing import services
from billing.models import Invoice, LedgerEntry, Merchant, Project

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="race-condition test requires real row-level locking (run against Postgres)",
    ),
]


def test_concurrent_payments_on_same_invoice_do_not_lose_updates():
    merchant = Merchant.objects.create(name="Acme")
    project = Project.objects.create(merchant=merchant, name="Shop", api_key="race-key")
    invoice = Invoice.objects.create(
        project=project,
        merchant_external_id="order-race-1",
        amount=Decimal("100.00"),
        currency="USD",
        expires_at=timezone.now() + timezone.timedelta(days=1),
    )

    barrier = threading.Barrier(2)
    errors = []

    def worker(provider_transaction_id, amount):
        try:
            barrier.wait(timeout=5)
            services.record_payment(
                invoice=Invoice.objects.get(pk=invoice.pk),
                provider_transaction_id=provider_transaction_id,
                amount=Decimal(amount),
                currency="USD",
                received_at=timezone.now(),
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [
        threading.Thread(target=worker, args=("txn-a", "60.00")),
        threading.Thread(target=worker, args=("txn-b", "60.00")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors

    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.OVERPAID  # 120 > 100 * 1.01

    credit_entries = LedgerEntry.objects.filter(invoice=invoice, entry_type=LedgerEntry.EntryType.CREDIT)
    commission_entries = LedgerEntry.objects.filter(invoice=invoice, entry_type=LedgerEntry.EntryType.COMMISSION)

    assert credit_entries.count() == 1
    assert commission_entries.count() == 1
    assert credit_entries.first().amount == Decimal("120.00")
    assert commission_entries.first().amount == Decimal("-1.20")