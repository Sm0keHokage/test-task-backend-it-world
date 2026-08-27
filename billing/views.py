import json
from decimal import Decimal, InvalidOperation

from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.http import require_http_methods

from billing import services
from billing.auth import require_internal_token, require_project_api_key
from billing.errors import error_response
from billing.models import Invoice

DEFAULT_TTL_SECONDS = 24 * 60 * 60


def _parse_json_body(request):
    try:
        return json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return None


def _serialize_invoice(invoice: Invoice, with_payments: bool = False) -> dict:
    payload = {
        "id": invoice.id,
        "merchant_external_id": invoice.merchant_external_id,
        "amount": str(invoice.amount),
        "currency": invoice.currency,
        "status": invoice.status,
        "created_at": invoice.created_at.isoformat(),
        "expires_at": invoice.expires_at.isoformat(),
    }
    if with_payments:
        payload["payments"] = [
            {
                "id": payment.id,
                "amount": str(payment.amount),
                "currency": payment.currency,
                "received_at": payment.received_at.isoformat(),
            }
            for payment in invoice.payments.all()
        ]
        payload["remaining_amount"] = str(services.invoice_remaining_amount(invoice))
    return payload


@require_http_methods(["POST"])
@require_project_api_key
def create_invoice(request):
    data = _parse_json_body(request)
    if data is None:
        return error_response(400, "invalid_json", "Request body must be valid JSON")

    merchant_external_id = data.get("merchant_external_id")
    amount_raw = data.get("amount")
    currency = data.get("currency")

    if not merchant_external_id or amount_raw is None or not currency:
        return error_response(
            400, "validation_error", "merchant_external_id, amount and currency are required"
        )

    try:
        amount = Decimal(str(amount_raw))
    except InvalidOperation:
        return error_response(400, "validation_error", "amount must be a valid decimal number")

    if amount <= 0:
        return error_response(400, "validation_error", "amount must be positive")

    ttl_seconds = data.get("ttl_seconds", DEFAULT_TTL_SECONDS)

    invoice, created = services.create_invoice(
        project=request.project,
        merchant_external_id=merchant_external_id,
        amount=amount,
        currency=currency,
        ttl_seconds=ttl_seconds,
    )

    return JsonResponse(_serialize_invoice(invoice), status=201 if created else 200)


@require_http_methods(["GET"])
@require_project_api_key
def invoice_detail(request, invoice_id):
    invoice = Invoice.objects.filter(pk=invoice_id, project=request.project).first()
    if invoice is None:
        return error_response(404, "not_found", "Invoice not found")

    return JsonResponse(_serialize_invoice(invoice, with_payments=True))


@require_http_methods(["POST"])
@require_project_api_key
def cancel_invoice(request, invoice_id):
    invoice = Invoice.objects.filter(pk=invoice_id, project=request.project).first()
    if invoice is None:
        return error_response(404, "not_found", "Invoice not found")

    try:
        invoice = services.cancel_invoice(invoice)
    except services.InvalidTransition as exc:
        return error_response(409, "invalid_transition", str(exc))

    return JsonResponse(_serialize_invoice(invoice))


@require_http_methods(["POST"])
@require_internal_token
def internal_record_payment(request):
    data = _parse_json_body(request)
    if data is None:
        return error_response(400, "invalid_json", "Request body must be valid JSON")

    invoice_id = data.get("invoice_id")
    provider_transaction_id = data.get("provider_transaction_id")
    amount_raw = data.get("amount")
    currency = data.get("currency")
    received_at_raw = data.get("received_at")

    if not all([invoice_id, provider_transaction_id, amount_raw, currency]):
        return error_response(
            400,
            "validation_error",
            "invoice_id, provider_transaction_id, amount and currency are required",
        )

    invoice = Invoice.objects.filter(pk=invoice_id).first()
    if invoice is None:
        return error_response(404, "not_found", "Invoice not found")

    try:
        amount = Decimal(str(amount_raw))
    except InvalidOperation:
        return error_response(400, "validation_error", "amount must be a valid decimal number")

    if amount <= 0:
        return error_response(400, "validation_error", "amount must be positive")

    received_at = parse_datetime(received_at_raw) if received_at_raw else timezone.now()

    try:
        payment, created = services.record_payment(
            invoice=invoice,
            provider_transaction_id=provider_transaction_id,
            amount=amount,
            currency=currency,
            received_at=received_at,
        )
    except services.ExchangeRateUnavailable as exc:
        return error_response(422, "exchange_rate_unavailable", str(exc))

    return JsonResponse(
        {
            "payment_id": payment.id,
            "invoice_id": payment.invoice_id,
            "invoice_status": payment.invoice.status,
        },
        status=201 if created else 200,
    )


@require_http_methods(["GET"])
@require_project_api_key
def merchant_balance(request, merchant_id):
    if request.merchant.id != merchant_id:
        return error_response(403, "forbidden", "API key does not grant access to this merchant")

    balance = services.merchant_balance(request.merchant)
    return JsonResponse(
        {
            "merchant_id": request.merchant.id,
            "balance": {currency: str(total) for currency, total in balance.items()},
        }
    )


def _serialize_report_row(row: dict) -> dict:
    serialized = dict(row)
    for key in ("issued_amount", "received_amount", "commission_amount"):
        serialized[key] = str(serialized[key])
    return serialized


@require_http_methods(["GET"])
@require_project_api_key
def merchant_report(request, merchant_id):
    if request.merchant.id != merchant_id:
        return error_response(403, "forbidden", "API key does not grant access to this merchant")

    group_by = request.GET.get("group_by", "day")
    date_from_raw = request.GET.get("date_from")
    date_to_raw = request.GET.get("date_to")

    date_from = parse_date(date_from_raw) if date_from_raw else None
    if date_from_raw and date_from is None:
        return error_response(400, "validation_error", "date_from must be in YYYY-MM-DD format")

    date_to = parse_date(date_to_raw) if date_to_raw else None
    if date_to_raw and date_to is None:
        return error_response(400, "validation_error", "date_to must be in YYYY-MM-DD format")

    try:
        report = services.merchant_report(
            merchant=request.merchant, date_from=date_from, date_to=date_to, group_by=group_by
        )
    except services.InvalidReportParameters as exc:
        return error_response(400, "validation_error", str(exc))

    return JsonResponse(
        {
            "merchant_id": request.merchant.id,
            "group_by": group_by,
            "report": [_serialize_report_row(row) for row in report],
        }
    )