from django.contrib import admin

from .models import Action, Approval, Draft, Email, EvalRun, Failure, Playbook, Triage


class TriageInline(admin.TabularInline):
    model = Triage
    extra = 0
    fields = ("category", "urgency", "confidence", "route", "review_reason", "injection_flag", "cost_usd", "created_at")
    readonly_fields = fields
    show_change_link = True


class ActionInline(admin.TabularInline):
    model = Action
    extra = 0
    fields = ("kind", "idempotency_key", "ok", "created_at")
    readonly_fields = fields


@admin.register(Email)
class EmailAdmin(admin.ModelAdmin):
    list_display = ("id", "received_at", "from_email", "subject", "status", "category", "urgency")
    list_filter = ("status", "category", "urgency")
    search_fields = ("from_email", "subject", "gmail_message_id")
    readonly_fields = ("created_at", "updated_at")
    inlines = [TriageInline, ActionInline]


@admin.register(Triage)
class TriageAdmin(admin.ModelAdmin):
    list_display = ("id", "email", "category", "urgency", "confidence", "route", "injection_flag", "cost_usd", "latency_ms")
    list_filter = ("category", "route", "injection_flag")


@admin.register(Draft)
class DraftAdmin(admin.ModelAdmin):
    list_display = ("id", "email", "ok", "model", "cost_usd", "created_at")


@admin.register(Action)
class ActionAdmin(admin.ModelAdmin):
    list_display = ("id", "email", "kind", "idempotency_key", "ok", "created_at")
    list_filter = ("kind", "ok")


@admin.register(Approval)
class ApprovalAdmin(admin.ModelAdmin):
    list_display = ("id", "email", "channel", "reviewer", "decision", "decided_at")


@admin.register(Failure)
class FailureAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "workflow", "node", "email", "resolved")
    list_filter = ("resolved", "workflow")


admin.site.register(Playbook)
admin.site.register(EvalRun)
admin.site.site_header = "AI Ops Inbox"
