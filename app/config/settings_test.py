import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only")
os.environ.setdefault("DATABASE_URL", "postgres://inbox:inbox@127.0.0.1:5432/inbox")

from .settings import *  # noqa: E402,F403

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
STORAGES = {"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}
