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
    'django_celery_beat'
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
            'sslmode': os.getenv("SSLMODE", default="disable"),
        },
    }
}

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
STATICFILES_DIRS = [
    os.path.join(BASE_DIR, 'static'),
]

# ---------------------------------------------------------------------------
# RAG / OpenAI
# ---------------------------------------------------------------------------

# IMPORTANT: these must be set in your .env file.
# OPENAI_CHAT_MODEL and OPENAI_EMBED_MODEL are both read here — if either is
# missing the app will raise AttributeError at request time, not at startup.
# Recommended .env values:
#   OPENAI_CHAT_MODEL=gpt-4o-mini
#   OPENAI_EMBED_MODEL=text-embedding-3-small

OPENAI_CHAT_MODEL  = os.getenv("OPENAI_CHAT_MODEL")
OPENAI_EMBED_MODEL = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")

# When True, uses pgvector SQL queries instead of in-process FAISS.
# Requires: pip install pgvector django-pgvector and DocumentChunk.embedding
# swapped for pgvector.VectorField.  Leave False unless you have pgvector set up.
VECTORSTORE_USE_PGVECTOR = os.getenv("VECTORSTORE_USE_PGVECTOR", "false").lower() == "true"

# Legacy paths (unused by the DB vector store; kept for any other consumers)
VECTORSTORE_PATH = os.getenv("VECTORSTORE_PATH")
DOCSTORE_PATH    = os.getenv("DOCSTORE_PATH")

# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------

GDRIVE_TOKEN_PATH            = os.getenv("GDRIVE_TOKEN_PATH")
GDRIVE_SERVICE_ACCOUNT_PATH  = os.getenv("GDRIVE_SERVICE_ACCOUNT_PATH")
GDRIVE_DEFAULT_FOLDER_ID     = os.getenv(
    "GDRIVE_DEFAULT_FOLDER_ID",
    "1PJThx7zpjsxbFHyz1GYYGugjxi0Zd4GP",   # override in .env for production
)

# ---------------------------------------------------------------------------
# Chunking defaults (used by ingest_gdrive and any auto-sync tasks)
# ---------------------------------------------------------------------------

CHUNK_MAX_CHARS = 1500
CHUNK_OVERLAP   = 150

# ---------------------------------------------------------------------------
# Web scraping
# ---------------------------------------------------------------------------

SCRAPING_URL = 'https://legislation.nsw.gov.au/view/html/inforce/current/sl-2011-0653'

# ---------------------------------------------------------------------------
# Drive webhook / sync
# ---------------------------------------------------------------------------

DRIVE_WEBHOOK_URL    = "https://natures-academy.com/webhooks/drive/notify/"
DRIVE_WEBHOOK_TOKEN  = os.getenv("DRIVE_WEBHOOK_TOKEN", "your-secret-token")
DRIVE_CHANNEL_ID     = os.getenv("DRIVE_CHANNEL_ID", "")
DRIVE_RESOURCE_ID    = os.getenv("DRIVE_RESOURCE_ID", "")
DRIVE_DEBOUNCE_SECONDS = int(os.getenv("DRIVE_DEBOUNCE_SECONDS", "30"))

# ---------------------------------------------------------------------------
# Celery / Redis
# ---------------------------------------------------------------------------

CELERY_BROKER_URL        = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND    = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")
CELERY_TASK_SERIALIZER   = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT    = ["json"]
CELERY_TIMEZONE          = "UTC"
CELERY_BEAT_SCHEDULER    = "django_celery_beat.schedulers:DatabaseScheduler"

# Uncomment and configure to enable automatic Drive sync via Celery Beat:
# from celery.schedules import crontab
# CELERY_BEAT_SCHEDULE = {
#     "check-drive-changes-hourly": {
#         "task":     "ragbot.tasks.check_drive_changes_task",
#         "schedule": crontab(minute=0),
#         "kwargs":   {"folder_id": GDRIVE_DEFAULT_FOLDER_ID},
#     },
#     "force-sync-weekly": {
#         "task":     "ragbot.tasks.debounced_index_task",
#         "schedule": crontab(hour=3, minute=0, day_of_week="sunday"),
#         "kwargs":   {"folder_id": GDRIVE_DEFAULT_FOLDER_ID},
#     },
# }

# ---------------------------------------------------------------------------
# Sessions / auth
# ---------------------------------------------------------------------------

SESSION_EXPIRE_AT_BROWSER_CLOSE = True   # anonymous sessions die with the tab
SESSION_COOKIE_AGE = 60 * 60 * 24 * 7   # 7 days for authenticated users

LOGIN_URL           = "/auth/login/"
LOGIN_REDIRECT_URL  = "/"
LOGOUT_REDIRECT_URL = "/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

APP_ENV = os.getenv("APP_ENV", "live")

hosts_string = os.environ.get('SCRAPING_URLS', '')

# Split the string by commas and remove extra whitespace
if hosts_string:
    SCRAPING_URLS = [host.strip() for host in hosts_string.split(',')]
else:
    SCRAPING_URLS = []
'''SCRAPING_URLS = [
    #"https://education.nsw.gov.au",
    "https://www.education.gov.au",
    "https://legislation.nsw.gov.au",
    #"https://nsw.gov.au",
    #"https://www.acecqa.gov.au",


    #"https://www.acecqa.gov.au/nqf/about/guide",
    #"https://www.acecqa.gov.au/nqf/national-quality-standard",
    #"https://www.acecqa.gov.au/nqf/national-law-regulations/approved-learning-frameworks",
    #"https://www.acecqa.gov.au/nqf/national-law-regulations",
    #"https://www.acecqa.gov.au/national-quality-framework",
    #"https://www.acecqa.gov.au/national-quality-framework/assessment-and-rating-resources",
    #"https://www.acecqa.gov.au/national-quality-framework/child-safety",
    #"https://www.acecqa.gov.au/resources/opening-a-new-service",
    #"https://www.acecqa.gov.au/national-quality-framework/nqf-elearning-modules",
    #"https://nsw.gov.au/departments-and-agencies/nsw-early-learning-commission",
    #"https://education.nsw.gov.au/early-childhood-education",
    #"https://legislation.nsw.gov.au/view/whole/html/inforce/current/sl-2025-601a",
    #"https://www.education.gov.au/early-childhood"
]'''