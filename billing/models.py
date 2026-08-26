from django.db import models


class Merchant(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        BLOCKED = "blocked", "Blocked"

    name = models.CharField(max_length=255)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "merchant"

    def __str__(self):
        return self.name


class Project(models.Model):
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name="projects")
    name = models.CharField(max_length=255)
    api_key = models.CharField(max_length=64, unique=True)
    webhook_url = models.URLField(max_length=2048, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "project"
        indexes = [
            models.Index(fields=["merchant"], name="ix_project_merchant_id"),
        ]

    def __str__(self):
        return self.name


class Invoice(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "New"
        PAID = "paid", "Paid"
        UNDERPAID = "underpaid", "Underpaid"
        OVERPAID = "overpaid", "Overpaid"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"

    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="invoices")
    merchant_external_id = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.NEW)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "invoice"
        constraints = [
            models.UniqueConstraint(
                fields=["project", "merchant_external_id"],
                name="uq_invoice_project_external",
            ),
            models.CheckConstraint(
                check=models.Q(amount__gt=0),
                name="ck_invoice_amount_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["project", "status"], name="ix_invoice_project_status"),
            models.Index(fields=["status", "expires_at"], name="ix_invoice_status_expires"),
            models.Index(fields=["project", "created_at"], name="ix_invoice_project_created"),
        ]

    def __str__(self):
        return f"Invoice#{self.pk} {self.amount} {self.currency} ({self.status})"


class Payment(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="payments")
    provider_transaction_id = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=3)
    exchange_rate_used = models.DecimalField(
        max_digits=18, decimal_places=8, null=True, blank=True
    )
    received_at = models.DateTimeField()

    class Meta:
        db_table = "payment"
        constraints = [
            models.UniqueConstraint(
                fields=["invoice", "provider_transaction_id"],
                name="uq_payment_invoice_transaction",
            ),
            models.CheckConstraint(
                check=models.Q(amount__gt=0),
                name="ck_payment_amount_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["invoice"], name="ix_payment_invoice_id"),
        ]

    def __str__(self):
        return f"Payment#{self.pk} {self.amount} {self.currency}"


class LedgerEntry(models.Model):
    class EntryType(models.TextChoices):
        CREDIT = "credit", "Credit"
        COMMISSION = "commission", "Commission"

    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name="ledger_entries")
    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, related_name="ledger_entries", null=True, blank=True
    )
    payment = models.ForeignKey(
        Payment, on_delete=models.PROTECT, related_name="ledger_entries", null=True, blank=True
    )
    entry_type = models.CharField(max_length=16, choices=EntryType.choices)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=3)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ledger_entry"
        constraints = [
            models.UniqueConstraint(
                fields=["invoice", "entry_type"],
                name="uq_ledger_invoice_type",
            ),
        ]
        indexes = [
            models.Index(fields=["merchant", "currency"], name="ix_ledger_merchant_currency"),
        ]

    def __str__(self):
        return f"LedgerEntry#{self.pk} {self.entry_type} {self.amount} {self.currency}"


class ExchangeRate(models.Model):
    currency_from = models.CharField(max_length=3)
    currency_to = models.CharField(max_length=3)
    rate = models.DecimalField(max_digits=18, decimal_places=8)
    effective_at = models.DateTimeField()

    class Meta:
        db_table = "exchange_rate"
        constraints = [
            models.UniqueConstraint(
                fields=["currency_from", "currency_to", "effective_at"],
                name="uq_rate_pair_time",
            ),
            models.CheckConstraint(
                check=models.Q(rate__gt=0),
                name="ck_rate_positive",
            ),
        ]
        indexes = [
            models.Index(
                fields=["currency_from", "currency_to", "-effective_at"],
                name="ix_rate_pair_time",
            ),
        ]

    def __str__(self):
        return f"{self.currency_from}->{self.currency_to} @ {self.effective_at}: {self.rate}"