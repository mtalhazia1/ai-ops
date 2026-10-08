"""Dashboard: review queue, email workspace, failures, metrics, playbook, evaluations.

Decisions made here (approve, edit, reject) go to n8n's WF3 webhook, which records them
through the internal API and sends the reply, so Slack and the dashboard share one path.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import metrics, services, state
from .forms import DecisionForm, PlaybookForm, TriageEditForm
from .llm import guard, schemas
from .models import Category, Email, EvalRun, Failure, Playbook, Status, Triage

REVIEW_STATUSES = [Status.NEEDS_REVIEW, Status.AWAITING_APPROVAL, Status.FAILED]
DECIDABLE = {Status.NEEDS_REVIEW, Status.AWAITING_APPROVAL}


def _filtered(request, qs):
    status, category, q = request.GET.get("status"), request.GET.get("category"), request.GET.get("q", "").strip()
    if status:
        qs = qs.filter(status=status)
    if category:
        qs = qs.filter(category=category)
    if q:
        qs = qs.filter(subject__icontains=q) | qs.filter(from_email__icontains=q)
    return qs, {"status": status, "category": category, "q": q}


def _list_context(request, qs, title, statuses):
    qs, filters = _filtered(request, qs)
    return {"emails": qs.order_by("-received_at")[:200], "title": title, "statuses": statuses,
            "categories": Category.choices, **filters}


@login_required
def review_queue(request):
    qs = Email.objects.filter(status__in=REVIEW_STATUSES)
    ctx = _list_context(request, qs, "Review queue", [(s.value, s.label) for s in REVIEW_STATUSES])
    template = "inbox/_email_rows.html" if request.headers.get("HX-Request") else "inbox/email_list.html"
    return render(request, template, ctx)


@login_required
def email_list(request):
    ctx = _list_context(request, Email.objects.all(), "All emails", Status.choices)
    template = "inbox/_email_rows.html" if request.headers.get("HX-Request") else "inbox/email_list.html"
    return render(request, template, ctx)


def _timeline(email: Email) -> list[dict]:
    events = [{"at": email.received_at, "kind": "received", "title": f"Received from {email.from_email}"}]
    for t in email.triages.all():
        who = t.model_classify if not t.model_classify.startswith("human:") else t.model_classify[6:]
        events.append({"at": t.created_at, "kind": "triage",
                       "title": f"Triage: {t.category} / {t.urgency} → {t.route}",
                       "detail": t.review_reason or t.reason, "meta": f"{who} · ${t.cost_usd}"})
    for d in email.drafts.all():
        events.append({"at": d.created_at, "kind": "draft",
                       "title": "Draft " + ("passed checks" if d.ok else "blocked"),
                       "detail": d.blocked_reason, "meta": f"{d.model} · ${d.cost_usd}"})
    for a in email.actions.all():
        if a.response.get("pending") or a.response.get("released"):
            continue
        events.append({"at": a.created_at, "kind": "action", "title": a.get_kind_display() + ("" if a.ok else " (failed)"),
                       "detail": a.idempotency_key})
    for ap in email.approvals.all():
        events.append({"at": ap.decided_at, "kind": "decision", "title": f"{ap.decision.capitalize()} via {ap.channel}",
                       "detail": ap.reviewer})
    for f in email.failures.all():
        events.append({"at": f.created_at, "kind": "failure", "title": f"{f.workflow} failed at {f.node}",
                       "detail": f.error[:300]})
    return sorted(events, key=lambda e: e["at"])


@login_required
def email_detail(request, pk):
    email = get_object_or_404(Email, pk=pk)
    triage_row = email.latest_triage
    draft = email.latest_draft
    decision_form = DecisionForm(initial={"final_body": draft.body if draft and draft.body else ""})
    triage_form = TriageEditForm(triage=triage_row) if triage_row else None
    return render(request, "inbox/email_detail.html", {
        "email": email,
        "triage": triage_row,
        "draft": draft,
        "decision_form": decision_form,
        "triage_form": triage_form,
        "can_decide": email.status in DECIDABLE,
        "can_edit": email.status in DECIDABLE | {Status.FAILED},
        "can_redraft": email.status in DECIDABLE and triage_row is not None,
        "timeline": _timeline(email),
    })


@login_required
@require_POST
def check_draft(request, pk):
    """HTMX: run the reply output checks on the text in the editor."""
    email = get_object_or_404(Email, pk=pk)
    problems = guard.check_reply(request.POST.get("final_body", ""),
                                 allowed_sources=[email.subject, email.body_text, email.from_email,
                                                  services.PlaybookData.from_model(Playbook.get()).text()])
    return render(request, "inbox/_checks.html", {"problems": problems})


@login_required
@require_POST
def decide(request, pk):
    email = get_object_or_404(Email, pk=pk)
    form = DecisionForm(request.POST)
    action = request.POST.get("action")
    if email.status not in DECIDABLE:
        messages.error(request, f"This email is {email.status}; nothing to decide.")
        return redirect("inbox:email_detail", pk=pk)
    if action == "ignore":
        state.transition(email, Status.IGNORED, reason=f"ignored by {request.user.get_username()}")
        messages.success(request, "Marked as ignored.")
        return redirect("inbox:review_queue")
    if not form.is_valid():
        messages.error(request, "Check the form.")
        return redirect("inbox:email_detail", pk=pk)
    body = form.cleaned_data["final_body"].strip()
    if action == "approve":
        if not body:
            messages.error(request, "Write the reply before approving.")
            return redirect("inbox:email_detail", pk=pk)
        problems = guard.check_reply(body, allowed_sources=[email.subject, email.body_text, email.from_email,
                                                           services.PlaybookData.from_model(Playbook.get()).text()])
        if problems and not form.cleaned_data["confirm"]:
            messages.error(request, "The reply fails the checks: " + "; ".join(problems)
                           + ". Fix it, or tick “Send anyway” to confirm.")
            return redirect("inbox:email_detail", pk=pk)
        decision = "approved"
    elif action == "reject":
        decision, body = "rejected", ""
    else:
        raise Http404
    try:
        services.send_decision(email, decision=decision, reviewer=request.user.get_username(), final_body=body)
    except services.N8nError as exc:
        messages.error(request, f"Could not reach n8n: {exc}")
        return redirect("inbox:email_detail", pk=pk)
    messages.success(request, "Approved: the reply is being sent." if decision == "approved" else "Rejected.")
    return redirect("inbox:email_detail", pk=pk)


@login_required
@require_POST
def edit_triage(request, pk):
    """Correct category, urgency or fields. Saved as a new Triage row (audit trail)."""
    email = get_object_or_404(Email, pk=pk)
    current = email.latest_triage
    if not current or email.status not in DECIDABLE | {Status.FAILED}:
        raise Http404
    form = TriageEditForm(request.POST, triage=current)
    if not form.is_valid():
        messages.error(request, "Check the fields: " + "; ".join(f"{k}: {v[0]}" for k, v in form.errors.items()))
        return redirect("inbox:email_detail", pk=pk)
    category, urgency, fields = form.result()
    with transaction.atomic():
        Triage.objects.create(
            email=email, category=category, urgency=urgency, confidence=1.0,
            reason=f"edited by {request.user.get_username()}", fields=fields,
            missing_fields=schemas.required_missing(category, fields), flags=current.flags,
            injection_flag=current.injection_flag, route=current.route, review_reason=current.review_reason,
            model_classify=f"human:{request.user.get_username()}",
        )
        email.category, email.urgency = category, urgency
        email.save(update_fields=["category", "urgency", "updated_at"])
    messages.success(request, "Saved. Re-draft to use the corrected details.")
    return redirect("inbox:email_detail", pk=pk)


@login_required
@require_POST
def redraft(request, pk):
    email = get_object_or_404(Email, pk=pk)
    if email.status not in DECIDABLE or not email.latest_triage:
        raise Http404
    if email.latest_triage.category == Category.OTHER:
        messages.error(request, "Set a category first; there is no reply template for “other”.")
        return redirect("inbox:email_detail", pk=pk)
    draft = services.create_draft(email)
    if draft.ok:
        messages.success(request, "New draft ready below.")
    else:
        messages.error(request, f"The new draft fails the checks ({draft.blocked_reason}). Edit it before sending.")
    return redirect("inbox:email_detail", pk=pk)


@login_required
def failures(request):
    show_all = request.GET.get("all") == "1"
    qs = Failure.objects.select_related("email").order_by("resolved", "-created_at")
    if not show_all:
        qs = qs.filter(resolved=False)
    return render(request, "inbox/failures.html", {"failures": qs[:200], "show_all": show_all})


@login_required
@require_POST
def retry_email(request, pk):
    email = get_object_or_404(Email, pk=pk)
    if email.status != Status.FAILED:
        messages.error(request, f"Only failed emails can be retried (this one is {email.status}).")
        return redirect(request.POST.get("next") or "inbox:failures")
    try:
        mode = services.retry(email)
    except (services.N8nError, state.InvalidTransition) as exc:
        messages.error(request, f"Retry failed: {exc}")
    else:
        messages.success(request, f"Retrying email #{email.id}: " + (
            "resuming at sending the approved reply." if mode == "send" else "running the process workflow again."))
    return redirect(request.POST.get("next") or "inbox:failures")


@login_required
@require_POST
def resolve_failure(request, pk):
    Failure.objects.filter(pk=pk).update(resolved=True)
    return redirect("inbox:failures")


@login_required
def metrics_page(request):
    period = request.GET.get("period", "today")
    if period not in metrics.PERIODS:
        period = "today"
    data = metrics.compute(period)
    data["since_dt"] = datetime.fromisoformat(data["since"])
    return render(request, "inbox/metrics.html", {
        "m": data, "period": period, "periods": list(metrics.PERIODS),
        "eval_run": EvalRun.objects.order_by("-started_at").first(),
    })


@login_required
def eval_list(request):
    return render(request, "inbox/eval_list.html", {"runs": EvalRun.objects.order_by("-started_at")[:50]})


@login_required
def eval_report(request, pk):
    run = get_object_or_404(EvalRun, pk=pk)
    path = Path(run.report_path)
    if not path.is_absolute():
        path = settings.BASE_DIR / path
    html_path = path.with_suffix(".html")
    reports_dir = (settings.EVALS_DIR / "reports").resolve()
    if not html_path.exists() or reports_dir not in html_path.resolve().parents:
        raise Http404("report not found")
    return HttpResponse(html_path.read_text())


@login_required
def playbook(request):
    obj = Playbook.get()
    form = PlaybookForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        saved = form.save(commit=False)
        saved.updated_by = request.user.get_username()
        saved.save()
        messages.success(request, "Playbook saved. New drafts use it straight away.")
        return redirect("inbox:playbook")
    return render(request, "inbox/playbook.html", {"form": form, "playbook": obj})
