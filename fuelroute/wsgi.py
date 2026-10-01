import logging
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fuelroute.settings")
application = get_wsgi_application()

# Warm the in-memory station index and gazetteer so the first request is fast.
try:
    from trips.services.gazetteer import get_gazetteer
    from trips.services.station_index import StationIndex

    StationIndex.get()
    get_gazetteer()
except Exception:  # e.g. database not migrated yet
    logging.getLogger("trips").warning("Warm-up skipped", exc_info=True)
