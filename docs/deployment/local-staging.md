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

## Local development after pulling

A local SQLite database does not follow the code by itself. After every `git pull` (or branch switch) that brings new
migrations, the running code expects columns the file does not have yet. The typical symptom: Django admin or the API fails with
`OperationalError: no such column: accounts_user.display_name`, and the frontend then shows "Authentication credentials were
not provided" because restoring the session (token refresh) fails on the server.

**Every time you pull:**

```sh
python manage.py migrate --check      # exit code 0: nothing pending; 1: migrations are waiting
python manage.py showmigrations --plan | grep "\[ \]"    # which ones
cp db.sqlite3 /tmp/db-before-migrate.sqlite3                 # a cheap backup first (see merge-and-migration-plan.md, section 4, for the full one)
python manage.py migrate
```

Restart the server after migrating. `runserver` reloads on code changes but never applies migrations.

**Know which database the server uses.** There can be several files at once (a checkout's own `db.sqlite3`, a `.runtime/...`
file, a copy you made for a demo). The server uses `DB_NAME` from its environment or `.env`; if neither is set it uses
`db.sqlite3` next to `manage.py` of the checkout it was started from. To see it:
`python manage.py shell -c "from django.db import connection; print(connection.settings_dict['NAME'])"` with the same environment,
or `lsof -p <server pid> | grep cwd` and `ps eww -p <pid>` for its `DB_NAME`.

**When a migration was renumbered** (for example `accounts` 0007/0008 became 0009/0010 when `main` took those numbers): look for
the old names in your database before migrating.

```sh
sqlite3 db.sqlite3 "select app, name from django_migrations where app = 'accounts' and name like '000%' order by name;"
```

* Only the names `main` has, and some are pending: just run `migrate`.
* Old names present (`0007_user_profile_fields_and_avatar`, `0008_user_sessions`, `0009_account_invitations`,
  `0010_remove_two_factor`): do not run a plain `migrate`, the tables already exist. Back up, then follow the rename/fake
  procedure in `docs/deployment/merge-and-migration-plan.md` (it was rehearsed on a copy: delete the four old rows,
  `migrate accounts 0008`, `migrate accounts 0012 --fake`, `migrate`), or recreate the file.
* Anything else looks inconsistent: stop, keep the backup, and rebuild from `dumpdata` instead of guessing.
