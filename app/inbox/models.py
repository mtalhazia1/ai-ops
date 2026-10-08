from django.db import models


class Category(models.TextChoices):
    QUOTE_REQUEST = "quote_request"
    BOOKING = "booking"
    SHIPMENT_STATUS = "shipment_status"
    PAPERWORK = "paperwork"
    CLAIM = "claim"
    OTHER = "other"


class Urgency(models.TextChoices):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class Status(models.TextChoices):
    RECEIVED = "received"
    TRIAGED = "triaged"
    NEEDS_REVIEW = "needs_review"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    DONE = "done"
    REJECTED = "rejected"
    FAILED = "failed"
    IGNORED = "ignored"


class Email(models.Model):
    gmail_message_id = models.CharField(max_length=255, unique=True)
    gmail_thread_id = models.CharField(max_length=255, blank=True)
    # RFC 5322 headers, used to thread the reply (In-Reply-To / References).
    rfc_message_id = models.CharField(max_length=998, blank=True)
    references = models.TextField(blank=True)
    reply_to = models.CharField(max_length=320, blank=True)
    from_email = models.CharField(max_length=320)
    from_name = models.CharField(max_length=255, blank=True)
    to_email = models.CharField(max_length=320, blank=True)
    subject = models.TextField(blank=True)
    body_text = models.TextField(blank=True)
    body_clean = models.TextField(blank=True)
    attachments = models.JSONField(default=list, blank=True)
    received_at = models.DateTimeField()
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.RECEIVED, db_index=True)
    category = models.CharField(max_length=32, choices=Category.choices, null=True, blank=True)
    urgency = models.CharField(max_length=16, choices=Urgency.choices, null=True, blank=True)
    needs_review_reason = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return f"#{self.pk} {self.from_email}: {self.subject[:60]}"

    @property
    def latest_triage(self):
        return self.triages.order_by("-created_at", "-id").first()

    @property
    def latest_draft(self):
        return self.drafts.order_by("-created_at", "-id").first()


class Triage(models.Model):
    email = models.ForeignKey(Email, on_delete=models.CASCADE, related_name="triages")
    category = models.CharField(max_length=32, choices=Category.choices)
    urgency = models.CharField(max_length=16, choices=Urgency.choices)
    confidence = models.FloatField()
    reason = models.TextField(blank=True)
    fields = models.JSONField(default=dict, blank=True)
    field_confidence = models.JSONField(default=dict, blank=True)
    missing_fields = models.JSONField(default=list, blank=True)
    # Field-level warnings from code checks, e.g. an invalid container check digit.
    flags = models.JSONField(default=list, blank=True)
    injection_flag = models.BooleanField(default=False)
    route = models.CharField(max_length=16, default="review")
    review_reason = models.TextField(blank=True)
    model_classify = models.CharField(max_length=100, blank=True)
    model_extract = models.CharField(max_length=100, blank=True)
    input_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    latency_ms = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)


class Draft(models.Model):
    email = models.ForeignKey(Email, on_delete=models.CASCADE, related_name="drafts")
    subject = models.TextField(blank=True)
    body = models.TextField()
    asks_for = models.JSONField(default=list, blank=True)
    ok = models.BooleanField(default=True)
    blocked_reason = models.TextField(blank=True)
    model = models.CharField(max_length=100, blank=True)
    input_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    latency_ms = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)


class Action(models.Model):
    KINDS = [
        ("hubspot_contact", "HubSpot contact"),
        ("hubspot_deal", "HubSpot deal"),
        ("hubspot_task", "HubSpot task"),
        ("hubspot_note", "HubSpot note"),
        ("shipmatch_upload", "ShipMatch upload"),
        ("shipmatch_lookup", "ShipMatch lookup"),
        ("slack_alert", "Slack alert"),
        ("reply_sent", "Reply sent"),
    ]
    email = models.ForeignKey(Email, on_delete=models.CASCADE, related_name="actions")
    kind = models.CharField(max_length=32, choices=KINDS)
    idempotency_key = models.CharField(max_length=255, unique=True)
    request = models.JSONField(default=dict, blank=True)
    response = models.JSONField(default=dict, blank=True)
    ok = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Approval(models.Model):
    CHANNELS = [("slack", "Slack"), ("dashboard", "Dashboard")]
    DECISIONS = [("approved", "Approved"), ("edited", "Edited"), ("rejected", "Rejected")]
    email = models.ForeignKey(Email, on_delete=models.CASCADE, related_name="approvals")
    channel = models.CharField(max_length=16, choices=CHANNELS)
    reviewer = models.CharField(max_length=255, blank=True)
    decision = models.CharField(max_length=16, choices=DECISIONS)
    final_body = models.TextField(blank=True)
    decided_at = models.DateTimeField(auto_now_add=True)


class Failure(models.Model):
    email = models.ForeignKey(Email, on_delete=models.SET_NULL, null=True, blank=True, related_name="failures")
    workflow = models.CharField(max_length=255, blank=True)
    node = models.CharField(max_length=255, blank=True)
    error = models.TextField(blank=True)
    execution_id = models.CharField(max_length=100, blank=True)
    resolved = models.BooleanField(default=False)
    retried_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Playbook(models.Model):
    """Single row in v1: company facts and tone used when drafting replies."""

    company_name = models.CharField(max_length=255, default="Indus Freight")
    signature = models.TextField(default="Best regards,\nOps Team\nIndus Freight")
    tone = models.TextField(default="Friendly, brief and professional.")
    facts = models.TextField(blank=True)
    never_promise = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.CharField(max_length=255, blank=True)

    @classmethod
    def get(cls):
        obj = cls.objects.order_by("id").first()
        return obj or cls.objects.create()


class EvalRun(models.Model):
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    dataset_version = models.CharField(max_length=64, blank=True)
    model_classify = models.CharField(max_length=100, blank=True)
    model_extract = models.CharField(max_length=100, blank=True)
    metrics = models.JSONField(default=dict, blank=True)
    report_path = models.CharField(max_length=500, blank=True)
