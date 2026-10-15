"""Django settings for the B4H admin portal."""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# RTSP over TCP is far more reliable than UDP. Set here (before anything imports cv2) so the live view,
# the movement alarm workers and `manage.py check_live` all behave the same, whatever the import order.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = os.getenv("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = [h.strip() for h in os.getenv("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if h.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "boxapp",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

if not DEBUG:
    # Static files in production (runserver serves them itself when DEBUG=1, so dev doesn't need whitenoise).
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")

ROOT_URLCONF = "config.urls"

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

WSGI_APPLICATION = "config.wsgi.application"

# The database only holds Django users/sessions. All camera data is read live from the box.
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.getenv("TIME_ZONE", "Asia/Dhaka")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- B4H box ---------------------------------------------------------------
B4H_BASE_URL = os.getenv("B4H_BASE_URL", "https://192.168.90.200")
B4H_USER = os.getenv("B4H_USER", "admin")
B4H_PASS = os.getenv("B4H_PASS", "")
RECOG_MAJOR = os.getenv("RECOG_MAJOR", "face_basic_business")
BOX_MAX_PAGE_SIZE = 30  # the box refuses more than 30 records per request

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"b4h": {"handlers": ["console"], "level": "INFO"}},
}

# --- Live video: RTSP URLs come from the box itself (device_config -> rtsp_param.url).
# Hikvision-style URLs end in /channels/<N>01 (main stream) or <N>02 (sub stream). The sub stream is
# much lighter to decode; set LIVE_SUBSTREAM=1 to use it (the NVR must have its sub stream enabled).
LIVE_SUBSTREAM = os.getenv("LIVE_SUBSTREAM", "0") == "1"

# --- Box logs: GET/POST /device_maintenance/log. The box validates the request body strictly
# (invalid_param), so the body is configurable without touching code: set B4H_LOG_BODY in .env to the
# JSON the box's own Logs page sends. Tokens: {offset} {size} {page} {start_s} {end_s} {start_ms}
# {end_ms}. A bare token keeps its number type; "{start_s:str}" turns it into a string.
B4H_LOG_BODY = json.loads(os.getenv("B4H_LOG_BODY") or json.dumps({
    "offset": "{offset}",
    "size": "{size}",
    "query_condition": {"start_time": "{start_s:str}", "end_time": "{end_s:str}"},
}))
B4H_LOG_PATH = os.getenv("B4H_LOG_PATH", "/device_maintenance/log")
B4H_LOG_FILE_PATH = os.getenv("B4H_LOG_FILE_PATH", "/device_maintenance/logFile")  # unverified: check the box UI's export button

# --- Algorithm packages: POST /intelli_manager/alg_warehouse/packet_list. Body is configurable like the
# log body (B4H_ALG_BODY in .env) in case the box rejects it with invalid_param.
B4H_ALG_PATH = os.getenv("B4H_ALG_PATH", "/intelli_manager/alg_warehouse/packet_list")
B4H_ALG_BODY = json.loads(os.getenv("B4H_ALG_BODY") or json.dumps({"offset": 0, "size": 100}))

# --- Live video timeouts (milliseconds / seconds). A camera that is unreachable or silent must fail fast
# instead of blocking a worker thread for OpenCV's default ~30 s.
LIVE_OPEN_TIMEOUT_MS = int(os.getenv("LIVE_OPEN_TIMEOUT_MS", "8000"))
LIVE_READ_TIMEOUT_MS = int(os.getenv("LIVE_READ_TIMEOUT_MS", "8000"))
LIVE_FIRST_FRAME_WAIT = int(os.getenv("LIVE_FIRST_FRAME_WAIT", "25"))   # a browser waits this long for the 1st frame
# Every open camera tile holds one server thread for as long as it streams. If they all do, the whole portal
# (even the login page) stops answering. Cap the streams so that cannot happen: keep this at least 8 below the
# server's thread count (waitress-serve --threads=32 with the default of 12 leaves plenty of room).
LIVE_MAX_STREAMS = int(os.getenv("LIVE_MAX_STREAMS", "12"))

# --- Event forecast agent (next-hour event count). See boxapp/forecast.py.
FORECAST_AGENT = os.getenv("FORECAST_AGENT", "1") == "1"            # start the background agent with the server
FORECAST_BACKFILL_DAYS = int(os.getenv("FORECAST_BACKFILL_DAYS", "28"))  # history pulled from the box on first run
FORECAST_MIN_TRAIN_ROWS = int(os.getenv("FORECAST_MIN_TRAIN_ROWS", "200"))
FORECAST_RUN_MINUTE = int(os.getenv("FORECAST_RUN_MINUTE", "2"))    # run this many minutes after each full hour
# "Investigate" when predicted >= normal + Z*sd AND >= normal*RATIO AND >= normal + MIN_DELTA.
FORECAST_Z = float(os.getenv("FORECAST_Z", "2.0"))
FORECAST_RATIO = float(os.getenv("FORECAST_RATIO", "1.5"))
FORECAST_MIN_DELTA = float(os.getenv("FORECAST_MIN_DELTA", "5"))
FORECAST_MODEL_PATH = BASE_DIR / "forecast_model.pkl"
