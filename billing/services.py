from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from billing.models import Invoice, LedgerEntry, Merchant, Payment, Project

COMMISSION_RATE = Decimal("0.01")
COMMISSION_MIN = Decimal("0.50")
OVERPAID_TOLERANCE_RATE = Decimal("0.01")

# Счёт остаётся открытым до оплаты или истечения срока
OPEN_STATUSES = (Invoice.Status.NEW, Invoice.Status.UNDERPAID)


class InvalidTransition(Exception):
    pass


class UnsupportedCurrencyConversion(Exception):
    pass


def create_invoice(
    *, project: Project, merchant_external_id: str, amount: Decimal, currency: str, ttl_seconds: int
) -> tuple[Invoice, bool]:
    existing = Invoice.objects.filter(project=project, merchant_external_id=merchant_external_id).first()
    if existing is not None:
        return existing, False

    try:
        with transaction.atomic():
            invoice = Invoice.objects.create(
                project=project,
                merchant_external_id=merchant_external_id,
                amount=amount,
                currency=currency,
                expires_at=timezone.now() + timezone.timedelta(seconds=ttl_seconds),
            )
        return invoice, True
    except IntegrityError:
        return Invoice.objects.get(project=project, merchant_external_id=merchant_external_id), False


def cancel_invoice(invoice: Invoice) -> Invoice:
    with transaction.atomic():
        locked = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if locked.status not in OPEN_STATUSES:
            raise InvalidTransition(f"Cannot cancel invoice in status '{locked.status}'")
        locked.status = Invoice.Status.CANCELLED
        locked.save(update_fields=["status"])
        return locked


def invoice_remaining_amount(invoice: Invoice) -> Decimal:
    received = invoice.payments.aggregate(total=Sum("amount"))["total"] or Decimal("0")
    remaining = invoice.amount - received
    return remaining if remaining > 0 else Decimal("0")


def record_payment(
    *, invoice: Invoice, provider_transaction_id: str, amount: Decimal, currency: str, received_at
) -> tuple[Payment, bool]:
    """Обработка колбэка о поступлении"""
    with transaction.atomic():
        locked_invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)

        existing = Payment.objects.filter(
            invoice=locked_invoice, provider_transaction_id=provider_transaction_id
        ).first()
        if existing is not None:
            return existing, False

        if currency != locked_invoice.currency:
            raise UnsupportedCurrencyConversion(
                "Payment currency differs from invoice currency; conversion is not implemented yet"
            )

        try:
            payment = Payment.objects.create(
                invoice=locked_invoice,
                provider_transaction_id=provider_transaction_id,
                amount=amount,
                currency=currency,
                received_at=received_at,
            )
        except IntegrityError:
            return (
                Payment.objects.get(invoice=locked_invoice, provider_transaction_id=provider_transaction_id),
                False,
            )

        total_received = locked_invoice.payments.aggregate(total=Sum("amount"))["total"] or Decimal("0")
        _apply_invoice_status(locked_invoice, total_received)

        return payment, True


def _apply_invoice_status(invoice: Invoice, total_received: Decimal) -> None:
    if total_received < invoice.amount:
        invoice.status = Invoice.Status.UNDERPAID
        invoice.save(update_fields=["status"])
        return

    overpaid = (total_received - invoice.amount) > invoice.amount * OVERPAID_TOLERANCE_RATE
    invoice.status = Invoice.Status.OVERPAID if overpaid else Invoice.Status.PAID
    invoice.save(update_fields=["status"])

    _settle_ledger(invoice, total_received)


def _settle_ledger(invoice: Invoice, total_received: Decimal) -> None:
    commission = max(total_received * COMMISSION_RATE, COMMISSION_MIN)

    LedgerEntry.objects.get_or_create(
        invoice=invoice,
        entry_type=LedgerEntry.EntryType.CREDIT,
        defaults={
            "merchant": invoice.project.merchant,
            "amount": total_received,
            "currency": invoice.currency,
        },
    )
    LedgerEntry.objects.get_or_create(
        invoice=invoice,
        entry_type=LedgerEntry.EntryType.COMMISSION,
        defaults={
            "merchant": invoice.project.merchant,
            "amount": -commission,
            "currency": invoice.currency,
        },
    )


def merchant_balance(merchant: Merchant) -> dict[str, Decimal]:
    rows = (
        LedgerEntry.objects.filter(merchant=merchant)
        .values("currency")
        .annotate(total=Sum("amount"))
        .order_by("currency")
    )
    return {row["currency"]: row["total"] for row in rows}