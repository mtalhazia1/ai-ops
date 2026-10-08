from django.urls import path

from . import views

app_name = "inbox"

urlpatterns = [
    path("", views.email_list, name="email_list"),
    path("emails/<int:pk>/", views.email_detail, name="email_detail"),
    path("playbook/", views.playbook, name="playbook"),
    path("evals/", views.eval_list, name="eval_list"),
    path("evals/<int:pk>/", views.eval_report, name="eval_report"),
]
