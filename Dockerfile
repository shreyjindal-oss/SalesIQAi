# Sales Intelligence IQ — Cloud Run service (Python 3.12 + Flask + NDB/Datastore)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run injects $PORT (default 8080). One worker with threads keeps the daily
# crawl (a single long request) simple; raise --timeout for large first crawls.
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 1800 main:app
