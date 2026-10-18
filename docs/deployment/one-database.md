# One database for everything

A development machine can end up with several databases without anyone deciding to: every clone and every `git worktree` has its own
`db.sqlite3` next to `manage.py` (Django's default), and a background process started in an old clone keeps writing to that clone's file.
The symptoms are users who exist in one place and not another, uploads that "disappear" after switching branch, and `no such column`
errors after a pull.

## The rule

**One file, named explicitly.** Put the absolute path in the `.env` of the clone you run:

```
DB_NAME=/Users/you/Projects/FaceCloud_backend/db.sqlite3
```

Everything that touches data (the web server, Celery workers, Celery beat, management commands, the Django shell) reads the same `.env`, so they all
use that file. A process started from another clone or worktree uses *that* clone's `.env`; if that has no `DB_NAME` it creates its own file.

## See which one a process is using

```
python manage.py database_info
```

prints the engine, the exact file, its size, how many users, photos and upload batches it holds, and whether any migration is unapplied. It
changes nothing. Run it before a demo, after a pull, and whenever data looks wrong.

## Check what is running

```
ps aux | grep -E "manage.py|celery"          # every server / worker / beat
lsof -p <pid> | grep -E "cwd|sqlite"          # the clone it was started from, and the database it holds open
```

A `celery beat` or `runserver` whose working directory is not your main clone is the usual second database. Stop it; delete nothing.

## Consolidating

1. Pick the database that has the data you want to keep (`database_info` on each).
2. Back every candidate up first: `sqlite3 <file> ".backup '<backup>'"` (safe while a server is running).
3. Point `DB_NAME` at the chosen file and restart the server and worker.
4. Leave the others where they are as backups, or move them under `~/FaceCloud_backups/`. Never delete one until the chosen database has been
   used for a while.

Merging rows from two databases is a data migration, not a setting: do it on a copy (see `docs/deployment/merge-and-migration-plan.md`).

## Production

SQLite is for development. Production uses PostgreSQL through `DB_ENGINE`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`
(`config/production_guard.py` refuses to start on anything else), so there is exactly one database server and every web and worker process
connects to it.


## After restarting a development server

A restart drops whatever the server was processing (with `DJANGO_DEBUG=True` the work runs inside the web process). The job then reads
`processing` until the reaper fails it, which by default is after more than an hour, and only if `celery beat` is running. To recover at
once:

```
python manage.py reap_stuck_jobs --older-than-seconds 120      # fails jobs "processing" for over two minutes; the owner can retry them
python manage.py reap_stuck_jobs --dry-run --older-than-seconds 120    # see what it would fail
```
