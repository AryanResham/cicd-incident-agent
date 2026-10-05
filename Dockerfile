# syntax=docker/dockerfile:1
# To-do app image. Small, non-root, listens on $PORT (Render sets it; 8000 otherwise).
#   docker build --build-arg APP_VERSION=$(git rev-parse HEAD) -t todo .
#   docker run --rm -p 8000:8000 todo
FROM python:3.12-slim

# PYTHONTZPATH="": time zone rules come only from the pinned `tzdata` package in
# requirements.txt, not from the base image's OS copy, so they are versioned with the app.
# PYTHONPATH=/app: the code lives in /app while the process runs in the data dir (see WORKDIR below).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONTZPATH="" \
    PYTHONPATH=/app

WORKDIR /app

# Dependencies first, so this layer is cached until requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Unprivileged user. It owns only the data dir; the code stays read-only (root-owned).
RUN groupadd --system app \
    && useradd --system --gid app --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data \
    && chown app:app /app/data

COPY app/ ./app/

# The commit SHA, reported by /health so the pipeline can tell which version is live.
# Declared late because it changes on every build (keeps the layers above cached).
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION

USER app
# Run from the writable data dir: the SQLite file (DATABASE_PATH, default ./todos.db) is created
# here. DATABASE_PATH is deliberately not set in the image, so the app's own default is what runs.
WORKDIR /app/data
EXPOSE 8000

# Shell form so ${PORT} expands; `exec` makes uvicorn PID 1 so it receives SIGTERM directly.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}

# slim has no curl: use Python's urllib against /health on the same port.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/health', timeout=4)"]
