# Same Python as the tests run on.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first: this layer is reused while only the code changes, so rebuilds are fast.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini .
COPY migrations ./migrations
COPY app ./app

# Never run as root inside the container.
RUN useradd --create-home appuser
USER appuser

# Migrations run on start, before the server accepts traffic. Safe here because Cloud Run is
# limited to one instance (max-instances=1); with more instances this moves to a separate job.
CMD alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}
