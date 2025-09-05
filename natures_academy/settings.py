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
    'django.contrib.messages.middleware.MessageMiddleware'
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
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.path.join(BASE_DIR, 'db.sqlite3'),
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