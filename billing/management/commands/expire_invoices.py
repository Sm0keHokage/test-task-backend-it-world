from django.core.management.base import BaseCommand

from billing import services


class Command(BaseCommand):
    help = "Переводит неоплаченные счета с истёкшим сроком действия в статус expired"

    def handle(self, *args, **options):
        total_expired = services.expire_overdue_invoices()
        self.stdout.write(self.style.SUCCESS(f"Expired {total_expired} invoice"))