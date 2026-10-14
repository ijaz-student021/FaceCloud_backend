# FaceCloud backend image for local staging (web, Celery worker, Celery Beat).
# Model checkpoints are NOT baked in: mount .runtime/models read-only.
# Target "gfpgan" adds the PyTorch/GFPGAN worker environment (CPU in Docker).
FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 facecloud
WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY --chown=facecloud:facecloud . .
RUN mkdir -p /app/media /app/.runtime/models && chown -R facecloud:facecloud /app/media /app/.runtime
USER facecloud
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120"]


FROM base AS gfpgan
USER root
COPY requirements-gfpgan.txt requirements-gfpgan-constraints.txt ./
RUN pip install -r requirements-gfpgan.txt -c requirements-gfpgan-constraints.txt
USER facecloud
