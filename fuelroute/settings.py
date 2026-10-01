"""Django settings for the fuel route planner.

Everything tunable is read from environment variables so the same code runs
locally, in tests and in a container without edits.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


def env(name, default=None, cast=str):
    value = os.environ.get(name)
    if value is None:
        return default
    if cast is bool:
        return value.lower() in {"1", "true", "yes", "on"}
    return cast(value)


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = env("DJANGO_DEBUG", True, bool)
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "trips",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "fuelroute.urls"
WSGI_APPLICATION = "fuelroute.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    }
]

def _database():
    """SQLite by default; DATABASE_URL=postgres://user:pass@host:5432/db for Postgres (needs psycopg)."""
    url = env("DATABASE_URL")
    if not url:
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": env("DJANGO_DB_PATH", str(BASE_DIR / "db.sqlite3"))}
    from urllib.parse import unquote, urlparse

    u = urlparse(url)
    if u.scheme not in ("postgres", "postgresql"):
        raise ValueError("DATABASE_URL must be a postgres:// URL")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": u.path.lstrip("/"),
        "USER": unquote(u.username or ""),
        "PASSWORD": unquote(u.password or ""),
        "HOST": u.hostname or "",
        "PORT": str(u.port or ""),
        "CONN_MAX_AGE": 60,
    }


DATABASES = {"default": _database()}

# Routes and stored plans (behind map_url) live here. The default file cache
# survives restarts and is shared by all workers on one machine; set REDIS_URL
# (needs the `redis` package) to share it across machines.
CACHES = {
    "default": (
        {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": env("REDIS_URL")}
        if env("REDIS_URL")
        else {
            "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
            "LOCATION": env("CACHE_DIR", str(BASE_DIR / ".cache")),
            "OPTIONS": {"MAX_ENTRIES": 2000},
        }
    )
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
# Django's default ("same-origin") sends no Referer to other sites, and the
# OpenStreetMap tile servers block tile requests without one.
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# Serve app static files (vendored Leaflet) directly, so collectstatic is optional.
WHITENOISE_USE_FINDERS = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"trips": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")}},
}

# --------------------------------------------------------------------------- #
# Fuel route planner settings
# --------------------------------------------------------------------------- #
# Routing providers are tried in order; the second is only called if the first fails.
ROUTING_PROVIDERS = env("ROUTING_PROVIDERS", "osrm,valhalla").split(",")
OSRM_BASE_URL = env("OSRM_BASE_URL", "https://router.project-osrm.org")
OSRM_PROFILE = env("OSRM_PROFILE", "driving")
VALHALLA_BASE_URL = env("VALHALLA_BASE_URL", "https://valhalla1.openstreetmap.de")
VALHALLA_COSTING = env("VALHALLA_COSTING", "auto")
ROUTING_TIMEOUT_SECONDS = env("ROUTING_TIMEOUT_SECONDS", 8.0, float)
ROUTING_USER_AGENT = env("ROUTING_USER_AGENT", "fuel-route-planner/1.0 (assessment)")

VEHICLE_RANGE_MILES = env("VEHICLE_RANGE_MILES", 500.0, float)
VEHICLE_MPG = env("VEHICLE_MPG", 10.0, float)
FUEL_CORRIDOR_MILES = env("FUEL_CORRIDOR_MILES", 10.0, float)
# Dollars an extra stop is "worth" when choosing stops (driver time). 0 = pure cheapest.
STOP_PENALTY_USD = env("STOP_PENALTY_USD", 5.0, float)

GAZETTEER_PATH = Path(env("GAZETTEER_PATH", str(DATA_DIR / "us_places_gazetteer.csv")))
SERVICE_AREA_PATH = Path(env("SERVICE_AREA_PATH", str(DATA_DIR / "lower48.geojson")))

ROUTE_CACHE_SECONDS = env("ROUTE_CACHE_SECONDS", 6 * 3600, int)
