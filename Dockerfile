# Nightly thermal briefing job (cloud/job.py); see cloud/README.md.
# Only the modules the briefing needs are copied: no database, no logbook credentials.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /app
COPY cloud/requirements.txt cloud/requirements.txt
RUN pip install --no-cache-dir -r cloud/requirements.txt

COPY src/config.py src/config.py
COPY src/prediction/ src/prediction/
COPY src/briefing_site/ src/briefing_site/
COPY src/airspace/ src/airspace/
COPY src/weather/open_meteo_forecast.py src/weather/open_meteo_forecast.py
COPY src/visualization/basemap.py src/visualization/runway_constants.py src/visualization/
COPY cloud/job.py cloud/job.py

ENTRYPOINT ["python", "cloud/job.py"]
