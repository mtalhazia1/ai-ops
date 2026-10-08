from django.urls import path

from . import views

app_name = "inbox"

urlpatterns = [
    path("", views.review_queue, name="review_queue"),
    path("emails/", views.email_list, name="email_list"),
    path("emails/<int:pk>/", views.email_detail, name="email_detail"),
    path("emails/<int:pk>/check/", views.check_draft, name="check_draft"),
    path("emails/<int:pk>/decide/", views.decide, name="decide"),
    path("emails/<int:pk>/triage/", views.edit_triage, name="edit_triage"),
    path("emails/<int:pk>/redraft/", views.redraft, name="redraft"),
    path("emails/<int:pk>/retry/", views.retry_email, name="retry"),
    path("failures/", views.failures, name="failures"),
    path("failures/<int:pk>/resolve/", views.resolve_failure, name="resolve_failure"),
    path("metrics/", views.metrics_page, name="metrics"),
    path("playbook/", views.playbook, name="playbook"),
    path("evals/", views.eval_list, name="eval_list"),
    path("evals/<int:pk>/", views.eval_report, name="eval_report"),
]
