# ============================================================
# celery.py  (place next to settings.py / manage.py)
# ============================================================

import os
from celery import Celery
from django.conf import settings

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "natures_academy.settings")

app = Celery("natures_academy")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


# ============================================================
# Running the workers
# ============================================================

# Terminal 1 — Celery worker
# celery -A your_project worker --loglevel=info

# Terminal 2 — Celery Beat scheduler
# celery -A your_project beat --loglevel=info

# One-liner for dev (worker + beat together):
# celery -A your_project worker --beat --loglevel=info


# ============================================================
# Management command usage
# ============================================================

# First-time setup (initialises page token):
#   python manage.py sync_drive --folder-id <ID>

# Check current state:
#   python manage.py sync_drive --status --folder-id <ID>

# Manual force re-index:
#   python manage.py sync_drive --force --folder-id <ID>

# Check only (no indexing):
#   python manage.py sync_drive --check-only --folder-id <ID>

# Register Drive push webhook (optional — for near real-time):
#   python manage.py sync_drive --register-watch --folder-id <ID>

# Renew webhook (Drive channels expire after 7 days):
#   python manage.py sync_drive --renew-watch --folder-id <ID>