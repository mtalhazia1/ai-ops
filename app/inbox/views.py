"""Dashboard pages. M0-M2: a read-only list, detail and evaluation reports.
The review queue, editing and approvals arrive in M4/M5."""

from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render

from .models import Email, EvalRun, Status


@login_required
def email_list(request):
    emails = Email.objects.all()
    status = request.GET.get("status")
    category = request.GET.get("category")
    if status:
        emails = emails.filter(status=status)
    if category:
        emails = emails.filter(category=category)
    return render(
        request,
        "inbox/email_list.html",
        {"emails": emails[:200], "statuses": Status.choices, "status": status, "category": category},
    )


@login_required
def email_detail(request, pk):
    email = get_object_or_404(Email, pk=pk)
    return render(
        request,
        "inbox/email_detail.html",
        {
            "email": email,
            "triages": email.triages.order_by("-created_at"),
            "actions": email.actions.order_by("created_at"),
            "failures": email.failures.order_by("-created_at"),
        },
    )


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
