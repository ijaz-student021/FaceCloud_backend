# Local staging

A production-shaped stack on one machine: PostgreSQL 16, Redis 7, Django
behind gunicorn, a Celery worker, Celery Beat and a one-shot migration step.
It uses its own Docker volumes and never touches a developer's
`db.sqlite3`, local PostgreSQL or local Redis. **Prepared, not yet run**: the
configuration was validated with `docker compose config` and Django's
`manage.py check`, but no image has been built and no container started.

Files: `Dockerfile`, `docker-compose.staging.yml`, `.env.staging.example`,
`.dockerignore`.

## Start

```sh
cp .env.staging.example .env.staging     # replace every CHANGE value; never commit it
# Docker Desktop must be running
docker compose -f docker-compose.staging.yml --env-file .env.staging up --build
```

* API on <http://127.0.0.1:8000>, with Swagger at `/api/docs/`.
* `migrate` runs once against the staging database, then `web`, `worker` and
  `beat` start.
* Create an administrator:
  `docker compose -f docker-compose.staging.yml --env-file .env.staging run --rm web python manage.py createsuperuser`
* Stop with `docker compose ... down`. Add `-v` to delete the staging data volumes.

## What is production-like and what is not

| Aspect | Staging | Production requirement |
|---|---|---|
| Database | PostgreSQL 16 (own volume) | Managed or backed-up PostgreSQL |
| Broker and cache | Redis 7 (`CACHE_URL` shared, so rate limits are global) | Same, with persistence and auth |
| Tasks | Separate worker and Beat; nothing runs in-process | Same |
| `DJANGO_DEBUG` | **True**, because the production guard requires HTTPS and real SMTP | False; the guard must pass |
| Email | Console (printed in the web and worker logs) | SMTP |
| TLS | none (localhost only) | Required |
| Media | Docker volume shared by web and worker; encrypted at rest | Shared, backed-up storage |
| Models | YuNet and SFace, mounted read-only from `.runtime/models`; CPU only | Pinned checkpoints (`docs/models/model_manifest.json`) |

## Models in Docker

Only two models are used, both for faces and both run by OpenCV DNN in the base
image: YuNet (detection) and SFace (identity features). Enhancement is OpenCV and
Pillow. There is no PyTorch, no GPU worker and no separate model queue; the
learned enhancement models were retired
(`docs/enhancement/retired-capabilities.md`). The worker serves the default queue.

## Checks after start

```sh
docker compose -f docker-compose.staging.yml --env-file .env.staging ps
docker compose -f docker-compose.staging.yml --env-file .env.staging run --rm web python manage.py check
docker compose -f docker-compose.staging.yml --env-file .env.staging run --rm web python manage.py showmigrations | grep "\[ \]"   # expect nothing
```

The end-to-end queue checks in `scripts/check_queue_integration.py` run
outside Docker (they start their own Redis and database). Run them on the host
before relying on staging.
