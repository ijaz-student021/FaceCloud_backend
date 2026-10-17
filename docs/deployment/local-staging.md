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
| Models | Mounted read-only from `.runtime/models`; **CPU only** | Pinned checkpoints on the target hardware |

## Models in Docker

Docker on macOS runs Linux containers, so they get **no MPS and no CUDA**.
The base image has no PyTorch.

* **Run in the base image:** Zero-DCE (OpenCV DNN), EDSR (OpenCV DNN, checksum
  verified at load), YuNet and SFace.
* **Off:** HD restoration (NAFNet, Real-ESRGAN), PureVision, PureScale,
  GFPGAN and colourisation.
* **GFPGAN:** the `gfpgan` profile builds the PyTorch worker and listens on the
  `gfpgan` queue on CPU. Enable it with `GFPGAN_ENABLED=True` and
  `GFPGAN_QUEUE_ENABLED=True`, then
  `docker compose ... --profile gfpgan up --build`. It is slow on CPU.
* **Colourisation stays disabled.**

## Checks after start

```sh
docker compose -f docker-compose.staging.yml --env-file .env.staging ps
docker compose -f docker-compose.staging.yml --env-file .env.staging run --rm web python manage.py check
docker compose -f docker-compose.staging.yml --env-file .env.staging run --rm web python manage.py showmigrations | grep "\[ \]"   # expect nothing
```

The end-to-end queue checks in `scripts/check_queue_integration.py` run
outside Docker (they start their own Redis and database). Run them on the host
before relying on staging.

## Emails while developing (copyable verification and reset links)

With `EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend` (the development default) Django prints the raw message, which is
quoted-printable: `=` becomes `=3D` and long lines are cut with a trailing `=`. A verification link copied from that output has a
mangled `registration_id` and `token`, and verification fails.

When `DJANGO_DEBUG=True` the console backend is replaced by `config.dev_email.DevConsoleEmailBackend`, which prints the decoded text and one
clearly marked line per link, for sign-up verification, password reset and both kinds of invitation:

```text
[dev email] To: ada@example.com
[dev email] Subject: Verify your FaceCloud email address
[dev email] verify link: http://localhost:5173/verify-email?registration_id=...&token=...
```

Copy the line after `verify link:` (or `password reset link:`, `invitation link:`) into the browser.

* `DEV_PRINT_EMAIL_LINKS=False` keeps Django's plain console output even in development.
* It never applies when `DJANGO_DEBUG` is off, the production guard refuses it as an `EMAIL_BACKEND`, and real SMTP recipients get exactly
  the message they got before.
