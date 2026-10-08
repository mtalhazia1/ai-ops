import os
from decimal import Decimal
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [v.strip() for v in os.environ.get(name, default).split(",") if v.strip()]


DEBUG = env_bool("DJANGO_DEBUG", False)
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or ("dev-insecure-key" if DEBUG else "")
if not SECRET_KEY:
    raise RuntimeError("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is false")
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,app")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "inbox",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": dj_database_url.config(
        default=os.environ.get("DATABASE_URL", "postgres://inbox:inbox@localhost:5432/inbox"),
        conn_max_age=60,
    )
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("GENERIC_TIMEZONE", "Asia/Karachi")
USE_I18N = True
USE_TZ = True

# Behind Caddy in production (docker-compose.prod.yml): trust its X-Forwarded-Proto and
# keep cookies HTTPS-only.
if env_bool("DJANGO_SECURE", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
LOGIN_URL = "/admin/login/"

# Email bodies live only in the database, never in application logs (Section 12).
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
}

# --- AI Ops Inbox ----------------------------------------------------------------
INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN", "")
N8N_EXECUTE_WEBHOOK_URL = os.environ.get("N8N_EXECUTE_WEBHOOK_URL", "")
N8N_PROCESS_WEBHOOK_URL = os.environ.get("N8N_PROCESS_WEBHOOK_URL", "")
N8N_WEBHOOK_SECRET = os.environ.get("N8N_WEBHOOK_SECRET", "")
DASHBOARD_BASE_URL = os.environ.get("DASHBOARD_BASE_URL", "http://localhost:8001")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL_CLASSIFY = os.environ.get("MODEL_CLASSIFY", "claude-haiku-4-5-20251001")
MODEL_EXTRACT = os.environ.get("MODEL_EXTRACT", "claude-sonnet-5-5")
MODEL_DRAFT = os.environ.get("MODEL_DRAFT", "claude-sonnet-5-5")
# Effort for models that accept output_config.effort (ignored for Haiku 4.5).
LLM_EFFORT = os.environ.get("LLM_EFFORT", "low")
LLM_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS", "30"))
LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "2"))
REVIEW_CONFIDENCE_THRESHOLD = float(os.environ.get("REVIEW_CONFIDENCE_THRESHOLD", "0.75"))
CLASSIFY_MAX_CHARS = 6000

# USD per million tokens (input, output). Checked against Anthropic's price list on
# 2026-10-08; update when prices change. Unknown models cost 0 and log a warning.
MODEL_PRICES_PER_MTOK = {
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50")),
    "claude-sonnet-5-5": (Decimal("2.00"), Decimal("10.00")),
    "claude-sonnet-5": (Decimal("2.00"), Decimal("10.00")),
    "claude-sonnet-4-6": (Decimal("3.00"), Decimal("15.00")),
    "claude-opus-5-5": (Decimal("4.00"), Decimal("20.00")),
}

SHIPMATCH_ENABLED = env_bool("SHIPMATCH_ENABLED", False)
SHIPMATCH_URL = os.environ.get("SHIPMATCH_URL", "")
SHIPMATCH_ORG = os.environ.get("SHIPMATCH_ORG", "demo")

EMAIL_BODY_RETENTION_DAYS = int(os.environ.get("EMAIL_BODY_RETENTION_DAYS", "90"))
EVALS_DIR = BASE_DIR / "evals"
