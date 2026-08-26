from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from billing.models import ExchangeRate, Invoice, LedgerEntry, Merchant, Payment, Project

COMMISSION_RATE = Decimal("0.01")
COMMISSION_MIN = Decimal("0.50")
OVERPAID_TOLERANCE_RATE = Decimal("0.01")
MONEY_QUANTUM = Decimal("0.01")

# Счёт остаётся открытым до оплаты или истечения срока
OPEN_STATUSES = (Invoice.Status.NEW, Invoice.Status.UNDERPAID)


class InvalidTransition(Exception):
    pass


class ExchangeRateUnavailable(Exception):
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


def get_exchange_rate(currency_from: str, currency_to: str, at) -> Decimal:
    rate_row = (
        ExchangeRate.objects.filter(currency_from=currency_from, currency_to=currency_to, effective_at__lte=at)
        .order_by("-effective_at")
        .first()
    )
    if rate_row is None:
        raise ExchangeRateUnavailable(
            f"No exchange rate for {currency_from}->{currency_to} at or before {at.isoformat()}"
        )
    return rate_row.rate


def _payment_amount_in_invoice_currency(payment: Payment, invoice_currency: str) -> Decimal:
    if payment.currency == invoice_currency:
        return payment.amount
    converted = payment.amount * payment.exchange_rate_used
    return converted.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _total_received_in_invoice_currency(invoice: Invoice) -> Decimal:
    total = Decimal("0")
    for payment in invoice.payments.all():
        total += _payment_amount_in_invoice_currency(payment, invoice.currency)
    return total


def invoice_remaining_amount(invoice: Invoice) -> Decimal:
    total_received = _total_received_in_invoice_currency(invoice)
    remaining = invoice.amount - total_received
    return remaining if remaining > 0 else Decimal("0")


def record_payment(
    *, invoice: Invoice, provider_transaction_id: str, amount: Decimal, currency: str, received_at
) -> tuple[Payment, bool]:
    exchange_rate_used = None
    if currency != invoice.currency:
        exchange_rate_used = get_exchange_rate(currency, invoice.currency, received_at)

    with transaction.atomic():
        locked_invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)

        existing = Payment.objects.filter(
            invoice=locked_invoice, provider_transaction_id=provider_transaction_id
        ).first()
        if existing is not None:
            return existing, False

        try:
            payment = Payment.objects.create(
                invoice=locked_invoice,
                provider_transaction_id=provider_transaction_id,
                amount=amount,
                currency=currency,
                exchange_rate_used=exchange_rate_used,
                received_at=received_at,
            )
        except IntegrityError:
            return (
                Payment.objects.get(invoice=locked_invoice, provider_transaction_id=provider_transaction_id),
                False,
            )

        total_received = _total_received_in_invoice_currency(locked_invoice)
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