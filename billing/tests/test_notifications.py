import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.utils import timezone

from billing import notifications, services
from billing.models import Invoice, Merchant, NotificationDelivery, Project

pytestmark = pytest.mark.django_db


@pytest.fixture
def merchant():
    return Merchant.objects.create(name="Acme")


@pytest.fixture
def project(merchant):
    return Project.objects.create(
        merchant=merchant,
        name="Shop",
        api_key="secret-key",
        webhook_url="https://merchant.example.com/webhook",
        webhook_secret="test-webhook-secret",
    )


def _make_invoice(project, status=Invoice.Status.NEW, external_id="order-1"):
    return Invoice.objects.create(
        project=project,
        merchant_external_id=external_id,
        amount=Decimal("100.00"),
        currency="USD",
        status=status,
        expires_at=timezone.now() + timezone.timedelta(days=1),
    )


def test_paid_invoice_enqueues_notification(project):
    invoice = _make_invoice(project)
    services.record_payment(
        invoice=invoice,
        provider_transaction_id="txn-1",
        amount=Decimal("100.00"),
        currency="USD",
        received_at=timezone.now(),
    )

    delivery = NotificationDelivery.objects.get(invoice=invoice)
    assert delivery.status == NotificationDelivery.Status.PENDING
    assert delivery.event_type == Invoice.Status.PAID
    assert delivery.payload["invoice_id"] == invoice.id
    assert delivery.payload["status"] == Invoice.Status.PAID


def test_underpaid_invoice_does_not_enqueue_notification(project):
    invoice = _make_invoice(project)
    services.record_payment(
        invoice=invoice,
        provider_transaction_id="txn-1",
        amount=Decimal("40.00"),
        currency="USD",
        received_at=timezone.now(),
    )

    assert not NotificationDelivery.objects.filter(invoice=invoice).exists()


def test_cancel_invoice_enqueues_notification(project):
    invoice = _make_invoice(project)
    services.cancel_invoice(invoice)

    delivery = NotificationDelivery.objects.get(invoice=invoice)
    assert delivery.event_type == Invoice.Status.CANCELLED


def test_expired_invoice_enqueues_notification(project):
    invoice = _make_invoice(project)
    Invoice.objects.filter(pk=invoice.pk).update(expires_at=timezone.now() - timezone.timedelta(days=1))

    services.expire_overdue_invoices()

    delivery = NotificationDelivery.objects.get(invoice=invoice)
    assert delivery.event_type == Invoice.Status.EXPIRED


def test_terminal_transition_enqueues_notification_only_once(project):
    invoice = _make_invoice(project)
    services.record_payment(
        invoice=invoice,
        provider_transaction_id="txn-1",
        amount=Decimal("100.00"),
        currency="USD",
        received_at=timezone.now(),
    )
    services.record_payment(
        invoice=invoice,
        provider_transaction_id="txn-1",
        amount=Decimal("100.00"),
        currency="USD",
        received_at=timezone.now(),
    )

    assert NotificationDelivery.objects.filter(invoice=invoice).count() == 1


def test_compute_signature_is_deterministic_hmac_sha256():
    body = b'{"a": 1}'
    secret = "my-secret"

    signature = notifications.compute_signature(body, secret)

    import hashlib
    import hmac

    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert signature == expected


def _mocked_response(status=200):
    response = MagicMock()
    response.status = status
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


@patch("billing.notifications.urllib.request.urlopen")
def test_successful_delivery_marks_delivered_and_signs_payload(mock_urlopen, project):
    invoice = _make_invoice(project, status=Invoice.Status.PAID)
    services._enqueue_terminal_notification(invoice)
    mock_urlopen.return_value = _mocked_response(200)

    stats = notifications.deliver_pending_notifications()

    assert stats == {"delivered": 1, "failed": 0, "retrying": 0}
    delivery = NotificationDelivery.objects.get(invoice=invoice)
    assert delivery.status == NotificationDelivery.Status.DELIVERED
    assert delivery.attempts == 1

    sent_request = mock_urlopen.call_args[0][0]
    assert sent_request.full_url == project.webhook_url
    body = sent_request.data
    expected_signature = notifications.compute_signature(body, project.webhook_secret)
    assert sent_request.headers["X-webhook-signature"] == expected_signature
    assert json.loads(body)["invoice_id"] == invoice.id


@patch("billing.notifications.urllib.request.urlopen")
def test_failed_delivery_schedules_retry_with_backoff(mock_urlopen, project):
    import urllib.error

    invoice = _make_invoice(project, status=Invoice.Status.PAID)
    services._enqueue_terminal_notification(invoice)
    mock_urlopen.side_effect = urllib.error.URLError("connection refused")

    before = timezone.now()
    stats = notifications.deliver_pending_notifications(now=before)

    assert stats == {"delivered": 0, "failed": 0, "retrying": 1}
    delivery = NotificationDelivery.objects.get(invoice=invoice)
    assert delivery.status == NotificationDelivery.Status.PENDING
    assert delivery.attempts == 1
    assert delivery.last_error
    assert delivery.next_attempt_at > before + timezone.timedelta(seconds=30)


@patch("billing.notifications.urllib.request.urlopen")
def test_delivery_marked_failed_after_exhausting_retries(mock_urlopen, project):
    import urllib.error

    invoice = _make_invoice(project, status=Invoice.Status.PAID)
    services._enqueue_terminal_notification(invoice)
    mock_urlopen.side_effect = urllib.error.URLError("connection refused")

    delivery = NotificationDelivery.objects.get(invoice=invoice)
    delivery.attempts = notifications.MAX_ATTEMPTS - 1
    delivery.save(update_fields=["attempts"])

    stats = notifications.deliver_pending_notifications()

    assert stats == {"delivered": 0, "failed": 1, "retrying": 0}
    delivery.refresh_from_db()
    assert delivery.status == NotificationDelivery.Status.FAILED
    assert delivery.attempts == notifications.MAX_ATTEMPTS


@patch("billing.notifications.urllib.request.urlopen")
def test_delivery_not_due_yet_is_skipped(mock_urlopen, project):
    invoice = _make_invoice(project, status=Invoice.Status.PAID)
    services._enqueue_terminal_notification(invoice)
    NotificationDelivery.objects.filter(invoice=invoice).update(
        next_attempt_at=timezone.now() + timezone.timedelta(hours=1)
    )

    stats = notifications.deliver_pending_notifications()

    assert stats == {"delivered": 0, "failed": 0, "retrying": 0}
    mock_urlopen.assert_not_called()


@patch("billing.notifications.urllib.request.urlopen")
def test_management_command_reports_stats(mock_urlopen, project, capsys):
    invoice = _make_invoice(project, status=Invoice.Status.PAID)
    services._enqueue_terminal_notification(invoice)
    mock_urlopen.return_value = _mocked_response(200)

    call_command("deliver_notifications")

    captured = capsys.readouterr()
    assert "Delivered: 1" in captured.out