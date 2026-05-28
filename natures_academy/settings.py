import os
from pathlib import Path
from dotenv import load_dotenv


# Set the project base directory
BASE_DIR = Path(__file__).resolve().parent.parent

# Take environment variables from .env file
load_dotenv(BASE_DIR / ".env")


# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.getenv("DEBUG", "True").lower() == "true"

ALLOWED_HOSTS = ['*']

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'ragbot',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
]

ROOT_URLCONF = 'natures_academy.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, 'templates')],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'natures_academy.wsgi.application'

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME":     os.getenv("DB_NAME"),
        "USER":     os.getenv("DB_USER"),
        "PASSWORD": os.getenv("DB_PASSWORD"),
        "HOST":     os.getenv("DB_HOST"),
        "PORT":     os.getenv("DB_PORT"),
        "OPTIONS": {
            'sslmode': os.getenv("SSLMODE",     default="disable"),
        },
    }
}

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = os.path.join (BASE_DIR, 'media') # a different directory from 'static'
STATICFILES_DIRS = [
    os.path.join (BASE_DIR, 'static'), # the 'static' directory at the project root
]

# RAG settings
VECTORSTORE_PATH =  os.getenv("VECTORSTORE_PATH")
DOCSTORE_PATH =  os.getenv("DOCSTORE_PATH")
OPENAI_CHAT_MODEL =  os.getenv("OPENAI_CHAT_MODEL")
OPENAI_EMBED_MODEL =  os.getenv("OPENAI_EMBED_MODEL")

# Google Drive
GDRIVE_TOKEN_PATH =  os.getenv("GDRIVE_TOKEN_PATH")
GDRIVE_SERVICE_ACCOUNT_PATH =  os.getenv("GDRIVE_SERVICE_ACCOUNT_PATH")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# settings.py
USE_IVF_INDEX = True  # For datasets > 1000 documents
OPENAI_EMBED_MODEL = "text-embedding-3-small"  # Fastest OpenAI model
#OPENAI_EMBED_MODEL = "gpt-3.5-turbo"

GDRIVE_DEFAULT_FOLDER_ID = "1PJThx7zpjsxbFHyz1GYYGugjxi0Zd4GP"  # Replace with your default folder ID
#GDRIVE_DEFAULT_FOLDER_ID = "0BzD0CeOiG_b2TUxtRGs0SEJmWW8"

## ============================================================
# Additions to settings.py
# ============================================================

# --- Celery broker (Redis) ---
CELERY_BROKER_URL        = "redis://localhost:6379/0"
CELERY_RESULT_BACKEND    = "redis://localhost:6379/0"
CELERY_TASK_SERIALIZER   = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT    = ["json"]
CELERY_TIMEZONE          = "UTC"
DRIVE_DEBOUNCE_SECONDS = 30
# Auth: uses ADC (Application Default Credentials)
# Locally: run `gcloud auth application-default login`
# On GCP:  attach a service account to your VM/container — no config needed

CHUNK_MAX_CHARS = 1500
CHUNK_OVERLAP   = 150

# Webhook settings (only needed if using Drive push notifications)
DRIVE_WEBHOOK_URL   = "https://natures-academy.com/webhooks/drive/notify/"
DRIVE_WEBHOOK_TOKEN = "your-secret-token"   # set after running --register-watch
DRIVE_CHANNEL_ID    = ""                    # set after running --register-watch
DRIVE_RESOURCE_ID   = ""                    # set after running --register-watch

# --- Celery Beat schedule ---
# Controls when check_drive_changes_task runs automatically
from celery.schedules import crontab

CELERY_BEAT_SCHEDULE = {
    # Check Drive for changes every hour
    "check-drive-changes-hourly": {
        "task":     "ragbot.tasks.check_drive_changes_task",
        "schedule": crontab(minute=0),          # top of every hour
        "kwargs":   {"folder_id": GDRIVE_DEFAULT_FOLDER_ID},
    },
    "force-sync-weekly": {
        "task":     "ragbot.tasks.debounced_index_task",
        "schedule": crontab(hour=3, minute=0, day_of_week="sunday"),
        "kwargs":   {"folder_id": GDRIVE_DEFAULT_FOLDER_ID},
    },
}