from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from billing import rates_client
from billing.models import ExchangeRate, Invoice, LedgerEntry, Merchant, NotificationDelivery, Payment, Project

COMMISSION_RATE = Decimal("0.01")
COMMISSION_MIN = Decimal("0.50")
OVERPAID_TOLERANCE_RATE = Decimal("0.01")
MONEY_QUANTUM = Decimal("0.01")

# Счёт остаётся открытым до оплаты или истечения срока
OPEN_STATUSES = (Invoice.Status.NEW, Invoice.Status.UNDERPAID)

# Оплаченный для целей отчёта - счёт, по которому реально прошли деньги (полностью или с переплатой),
# underpaid не считается оплаченным.
PAID_LIKE_STATUSES = (Invoice.Status.PAID, Invoice.Status.OVERPAID)

REPORT_GROUP_BY_CHOICES = ("day", "project")


def _enqueue_terminal_notification(invoice: Invoice) -> None:
    """Ставит уведомление в очередь при входе счёта в терминальный статус"""
    NotificationDelivery.objects.get_or_create(
        invoice=invoice,
        defaults={
            "event_type": invoice.status,
            "payload": {
                "invoice_id": invoice.id,
                "merchant_external_id": invoice.merchant_external_id,
                "project_id": invoice.project_id,
                "status": invoice.status,
                "amount": str(invoice.amount),
                "currency": invoice.currency,
            },
            "next_attempt_at": timezone.now(),
        },
    )


class InvalidReportParameters(Exception):
    pass


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
        _enqueue_terminal_notification(locked)
        return locked


def get_exchange_rate(currency_from: str, currency_to: str, at) -> Decimal:
    """ Курс, действовавший на момент поступления, сначала смотрим в бд, если нет,
        то идем в отдельный сервис """
    rate_row = (
        ExchangeRate.objects.filter(currency_from=currency_from, currency_to=currency_to, effective_at__lte=at)
        .order_by("-effective_at")
        .first()
    )
    if rate_row is not None:
        return rate_row.rate

    try:
        rate = rates_client.fetch_rate(currency_from, currency_to)
    except rates_client.RatesServiceUnavailable as exc:
        raise ExchangeRateUnavailable(
            f"No exchange rate for {currency_from}->{currency_to} at or before {at.isoformat()} "
            f"(rates service also unavailable: {exc})"
        ) from exc

    rate_row, _ = ExchangeRate.objects.get_or_create(
        currency_from=currency_from,
        currency_to=currency_to,
        effective_at=at,
        defaults={"rate": rate},
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
    _enqueue_terminal_notification(invoice)


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


DEFAULT_EXPIRE_BATCH_SIZE = 1000


def expire_overdue_invoices(*, now=None, batch_size: int = DEFAULT_EXPIRE_BATCH_SIZE) -> int:
    """Переводит просроченные счета в expired"""
    now = now or timezone.now()
    total_expired = 0

    while True:
        batch_ids = list(
            Invoice.objects.filter(status__in=OPEN_STATUSES, expires_at__lt=now)
            .order_by("id")
            .values_list("id", flat=True)[:batch_size]
        )
        if not batch_ids:
            break

        with transaction.atomic():
            updated = Invoice.objects.filter(
                id__in=batch_ids, status__in=OPEN_STATUSES, expires_at__lt=now
            ).update(status=Invoice.Status.EXPIRED)

            if updated:
                for invoice in Invoice.objects.filter(id__in=batch_ids, status=Invoice.Status.EXPIRED):
                    _enqueue_terminal_notification(invoice)

        total_expired += updated
        if updated == 0:
            # Все кандидаты из батча оказались уже обработаны или изменены, выходим из цикла
            break

    return total_expired


def merchant_balance(merchant: Merchant) -> dict[str, Decimal]:
    rows = (
        LedgerEntry.objects.filter(merchant=merchant)
        .values("currency")
        .annotate(total=Sum("amount"))
        .order_by("currency")
    )
    return {row["currency"]: row["total"] for row in rows}


def merchant_report(*, merchant: Merchant, date_from, date_to, group_by: str) -> list[dict]:
    """Отчёт по мерчанту, сгруппированный по дню выставления счёта или по проекту"""
    if group_by not in REPORT_GROUP_BY_CHOICES:
        raise InvalidReportParameters(f"group_by must be one of {REPORT_GROUP_BY_CHOICES}")

    invoice_qs = Invoice.objects.filter(project__merchant=merchant)
    ledger_qs = LedgerEntry.objects.filter(merchant=merchant, invoice__isnull=False)

    if date_from is not None:
        invoice_qs = invoice_qs.filter(created_at__date__gte=date_from)
        ledger_qs = ledger_qs.filter(invoice__created_at__date__gte=date_from)
    if date_to is not None:
        invoice_qs = invoice_qs.filter(created_at__date__lte=date_to)
        ledger_qs = ledger_qs.filter(invoice__created_at__date__lte=date_to)

    if group_by == "day":
        group_field = "day"
        invoice_groups = invoice_qs.annotate(day=TruncDate("created_at"))
        ledger_groups = ledger_qs.annotate(day=TruncDate("invoice__created_at"))
        project_names = None
    else:
        group_field = "project_id"
        invoice_groups = invoice_qs
        ledger_groups = ledger_qs.annotate(project_id=F("invoice__project_id"))
        project_names = dict(Project.objects.filter(merchant=merchant).values_list("id", "name"))

    invoice_rows = (
        invoice_groups.values(group_field)
        .annotate(
            issued_count=Count("id"),
            paid_count=Count("id", filter=Q(status__in=PAID_LIKE_STATUSES)),
            issued_amount=Sum("amount"),
        )
        .order_by(group_field)
    )
    ledger_rows = ledger_groups.values(group_field).annotate(
        received_amount=Sum("amount", filter=Q(entry_type=LedgerEntry.EntryType.CREDIT)),
        commission_amount=Sum("amount", filter=Q(entry_type=LedgerEntry.EntryType.COMMISSION)),
    )

    ledger_by_key = {row[group_field]: row for row in ledger_rows}

    report = []
    for row in invoice_rows:
        key = row[group_field]
        ledger_row = ledger_by_key.get(key, {})
        issued_count = row["issued_count"]
        paid_count = row["paid_count"]

        entry = {
            "issued_count": issued_count,
            "paid_count": paid_count,
            "issued_amount": row["issued_amount"] or Decimal("0"),
            "received_amount": ledger_row.get("received_amount") or Decimal("0"),
            "commission_amount": abs(ledger_row.get("commission_amount") or Decimal("0")),
            "conversion": round(paid_count / issued_count, 4) if issued_count else 0.0,
        }
        if group_by == "day":
            entry["day"] = key.isoformat()
        else:
            entry["project_id"] = key
            entry["project_name"] = project_names.get(key, "")

        report.append(entry)

    return report