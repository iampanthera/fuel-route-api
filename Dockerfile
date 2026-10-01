FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DJANGO_DEBUG=false
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn "psycopg[binary]" redis

COPY . .
RUN python manage.py collectstatic --noinput

EXPOSE 8000
# The station table is small and read-only: migrate and (re)import on start, then serve.
CMD python manage.py migrate --noinput \
 && python manage.py import_stations data/fuel-prices-for-be-assessment.csv \
 && gunicorn fuelroute.wsgi --bind 0.0.0.0:${PORT:-8000} --workers ${WEB_CONCURRENCY:-2}
