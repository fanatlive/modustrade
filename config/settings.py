"""Explicit local-demo defaults; PostgreSQL is required for a production pilot."""
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
# Read literal key/value pairs, never execute a user's environment file.
env_file = BASE_DIR / '.env'
if env_file.exists():
    for raw in env_file.read_text(encoding='utf-8-sig').splitlines():
        line = raw.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('\"').strip("'"))

DEBUG = os.getenv('DEBUG', '1').lower() in {'1', 'true', 'yes'}
DEMO_MODE = os.getenv('DEMO_MODE', str(int(DEBUG))).lower() in {'1', 'true', 'yes'}
SECRET_KEY = os.getenv('SECRET_KEY', 'local-demo-only-not-a-production-secret')
if not DEBUG and SECRET_KEY == 'local-demo-only-not-a-production-secret':
    raise ImproperlyConfigured('Set a private SECRET_KEY when DEBUG=0.')
if not DEBUG and (len(SECRET_KEY) < 50 or len(set(SECRET_KEY)) < 5 or SECRET_KEY.startswith(('django-insecure-', 'CHANGE_ME', 'change-'))):
    raise ImproperlyConfigured('Production SECRET_KEY must be a private random value of at least 50 characters.')
ALLOWED_HOSTS = [v.strip() for v in os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1,testserver').split(',') if v.strip()]
CSRF_TRUSTED_ORIGINS = [v for v in os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',') if v]
INSTALLED_APPS = [
    'django.contrib.admin', 'django.contrib.auth', 'django.contrib.contenttypes',
    'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles',
    'feedback.apps.FeedbackConfig',
]
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'config.middleware.MutationThrottle',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]
ROOT_URLCONF = 'config.urls'
TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [BASE_DIR / 'templates'], 'APP_DIRS': True,
    'OPTIONS': {'context_processors': [
        'django.template.context_processors.request', 'django.contrib.auth.context_processors.auth',
        'django.contrib.messages.context_processors.messages', 'feedback.context_processors.workspace',
    ]},
}]
WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'
DB_ENGINE = os.getenv('DB_ENGINE', 'sqlite')
if DB_ENGINE == 'postgres':
    DATABASES = {'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.getenv('POSTGRES_DB', 'modus'),
        'USER': os.getenv('POSTGRES_USER', 'modus'),
        'PASSWORD': os.getenv('POSTGRES_PASSWORD', ''),
        'HOST': os.getenv('POSTGRES_HOST', '127.0.0.1'),
        'PORT': os.getenv('POSTGRES_PORT', '5432'),
        'CONN_MAX_AGE': 60, 'CONN_HEALTH_CHECKS': True,
        # Short interactive queries are latency-sensitive; benchmark EXPLAIN
        # showed JIT compilation dominating correlated permission/SLA queries.
        'OPTIONS': {'connect_timeout': 5, 'options': '-c jit='+('on' if os.getenv('DB_JIT', '0') == '1' else 'off')},
    }}
elif DB_ENGINE == 'sqlite' and DEBUG:
    DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': os.getenv('SQLITE_PATH') or BASE_DIR / 'db.sqlite3', 'OPTIONS': {'timeout': 20}}}
else:
    raise ImproperlyConfigured('Use DB_ENGINE=postgres for a pilot; SQLite is allowed only with DEBUG=1.')
AUTH_USER_MODEL = 'feedback.User'
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
LANGUAGE_CODE = 'ru-ru'
BUSINESS_TIMEZONE = os.getenv('BUSINESS_TIMEZONE', 'Asia/Bishkek')
TIME_ZONE = BUSINESS_TIMEZONE
USE_I18N = True
USE_TZ = True
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
WHITENOISE_USE_FINDERS = DEBUG
STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'}}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/accounts/login/'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_SSL_REDIRECT = not DEBUG
SECURE_REDIRECT_EXEMPT = [r'^health/$', r'^health/worker/$']
TRUST_PROXY = os.getenv('TRUST_PROXY', '0').lower() in {'1', 'true', 'yes'}
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https') if TRUST_PROXY else None
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG
X_FRAME_OPTIONS = 'DENY'
DATA_UPLOAD_MAX_MEMORY_SIZE = 262144
# Attachments live outside any public URL; downloads go through an access-checked view.
ATTACHMENTS_ROOT = Path(os.getenv('ATTACHMENTS_ROOT') or BASE_DIR / 'media' / 'attachments')
ATTACHMENT_MAX_BYTES = int(os.getenv('ATTACHMENT_MAX_BYTES', str(10 * 1024 * 1024)))
ATTACHMENT_MAX_FILES = int(os.getenv('ATTACHMENT_MAX_FILES', '3'))
FILE_UPLOAD_PERMISSIONS = 0o640
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o750
WORKER_HEARTBEAT_MAX_AGE = int(os.getenv('WORKER_HEARTBEAT_MAX_AGE', '120'))
OPERATIONS_TOKEN = os.getenv('OPERATIONS_TOKEN', '')
if not DEBUG and OPERATIONS_TOKEN and (len(OPERATIONS_TOKEN) < 32 or OPERATIONS_TOKEN.startswith(('CHANGE_ME', 'change-'))):
    raise ImproperlyConfigured('OPERATIONS_TOKEN must be a private random value of at least 32 characters.')
RATE_LOGIN_IP = int(os.getenv('RATE_LOGIN_IP', '20'))
RATE_LOGIN_ACCOUNT = int(os.getenv('RATE_LOGIN_ACCOUNT', '10'))
LOGGING = {'version': 1, 'disable_existing_loggers': False, 'handlers': {'console': {'class': 'logging.StreamHandler'}}, 'root': {'handlers': ['console'], 'level': 'INFO'}}
