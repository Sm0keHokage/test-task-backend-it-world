import hashlib
import hmac
import json
import urllib.error
import urllib.request

from django.db import transaction
from django.utils import timezone

from billing.models import NotificationDelivery

WEBHOOK_TIMEOUT_SECONDS = 5

# backoff: 1мин 5мин 30мин 2ч 6ч
# После этого попыток больше нет, статус фиксируется как failed для ручного разбора
RETRY_SCHEDULE_SECONDS = [60, 300, 1800, 7200, 21600]
MAX_ATTEMPTS = len(RETRY_SCHEDULE_SECONDS) + 1

DEFAULT_DELIVERY_BATCH_SIZE = 100


def compute_signature(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _next_retry_delay_seconds(attempts_made: int) -> int | None:
    index = attempts_made - 1
    if index < len(RETRY_SCHEDULE_SECONDS):
        return RETRY_SCHEDULE_SECONDS[index]
    return None


def _send_webhook(delivery: NotificationDelivery) -> tuple[bool, str]:
    project = delivery.invoice.project

    if not project.webhook_url:
        return False, "project has no webhook_url configured"

    body = json.dumps(delivery.payload, sort_keys=True).encode("utf-8")
    signature = compute_signature(body, project.webhook_secret)

    request = urllib.request.Request(
        project.webhook_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": signature,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT_SECONDS) as response:
            if 200 <= response.status < 300:
                return True, ""
            return False, f"unexpected status code {response.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        return False, f"connection error: {exc.reason}"
    except TimeoutError:
        return False, "timeout"


def deliver_pending_notifications(
    *, now=None, batch_size: int = DEFAULT_DELIVERY_BATCH_SIZE
) -> dict[str, int]:
    """Отправляет вебхуки по всем due-уведомлениям"""
    now = now or timezone.now()

    candidate_ids = list(
        NotificationDelivery.objects.filter(
            status=NotificationDelivery.Status.PENDING, next_attempt_at__lte=now
        )
        .order_by("next_attempt_at")
        .values_list("id", flat=True)[:batch_size]
    )

    stats = {"delivered": 0, "failed": 0, "retrying": 0}

    for delivery_id in candidate_ids:
        with transaction.atomic():
            delivery = (
                NotificationDelivery.objects.select_for_update(skip_locked=True)
                .select_related("invoice__project")
                .filter(pk=delivery_id, status=NotificationDelivery.Status.PENDING)
                .first()
            )
            if delivery is None:
                continue

            ok, error = _send_webhook(delivery)
            delivery.attempts += 1

            if ok:
                delivery.status = NotificationDelivery.Status.DELIVERED
                delivery.last_error = ""
                stats["delivered"] += 1
            else:
                delivery.last_error = error
                delay = _next_retry_delay_seconds(delivery.attempts)
                if delay is None:
                    delivery.status = NotificationDelivery.Status.FAILED
                    stats["failed"] += 1
                else:
                    delivery.next_attempt_at = timezone.now() + timezone.timedelta(seconds=delay)
                    stats["retrying"] += 1

            delivery.save(update_fields=["attempts", "status", "last_error", "next_attempt_at", "updated_at"])

    return stats