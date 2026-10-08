from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path

from inbox.api import api


def healthz(request):
    # Unauthenticated liveness probe for Docker; reveals nothing.
    return JsonResponse({"ok": True})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("internal/", api.urls),
    path("healthz", healthz),
    path("", include("inbox.urls")),
]
