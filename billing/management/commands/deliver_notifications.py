from django.core.management.base import BaseCommand

from billing import notifications


class Command(BaseCommand):
    help = (
        "Отправляет вебхуки мерчантам по счетам, перешедшим в терминальный статус,"
        "с ретраями по backoff"
    )

    def handle(self, *args, **options):
        stats = notifications.deliver_pending_notifications()
        self.stdout.write(
            self.style.SUCCESS(
                f"Delivered: {stats['delivered']}, retrying: {stats['retrying']}, failed: {stats['failed']}"
            )
        )