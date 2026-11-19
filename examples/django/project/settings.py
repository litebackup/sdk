"""A Django project whose SQLite database Litebackup backs up from startup."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "example-only")
DEBUG = True
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "project.urls"
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "litebackup.integrations.django",
]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("DATABASE_PATH", BASE_DIR / "db.sqlite3"),
    }
}
# Registered in AppConfig.ready() on every start; repeated registration returns the saved rule.
LITEBACKUP = {
    "databases": {"default": {"name": "example/db.sqlite3"}},
    # Tests and image builds run without the daemon.
    "enabled": os.environ.get("LITEBACKUP_ENABLED", "1") == "1",
}
