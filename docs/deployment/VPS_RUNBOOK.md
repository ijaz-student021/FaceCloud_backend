# VPS runbook: api.facecloudai.com

**Status: written from the code and its checks; not executed on a server.** Nothing here was run on a VPS, and no server was touched. Every command is
standard for the tools named, but treat the first run as a rehearsal: do it on a throwaway VPS before the real one. Measured numbers about models are in
`hostinger-readiness-2026-10.md` and `ec2-small-cpu.md` (an Apple M1, not a VPS): re-measure on the machine.

## 0. The shape

```
Browser (https://www.facecloudai.com, the React app on its own host)
        |  https, bearer token
        v
Nginx (api.facecloudai.com, TLS)  ->  gunicorn (unix socket)  ->  Django REST
                                                |                    |
                                                |                    +--> PostgreSQL (localhost)
                                                |                    +--> Redis (localhost): cache, Celery broker and results
                                                +--> /var/lib/facecloud/media  (private files, never served by Nginx)
Celery worker(s) + exactly one Celery beat  <--  Redis   (same code, same environment file)
```

* **Two hostnames, two roles.** `www.facecloudai.com` is the frontend (static files, hosted wherever it is today). `api.facecloudai.com` is this backend. The
  browser app calls the API cross-origin with a bearer token, so CORS must name `https://www.facecloudai.com` and nothing else.
* **Uploads are private.** There is no `/media/` route in Django and Nginx must not alias the media directory: every photo is fetched through an authenticated
  endpoint, and is encrypted at rest (`IMAGE_ENCRYPTION_*`).
* **Where the models run.** On a CPU VPS use the light profile (`ec2-small-cpu.env.example`): no PyTorch, no GPU. Heavy models belong on a separate GPU worker
  (see `hostinger-readiness-2026-10.md`: S3 storage is a prerequisite for splitting API and worker onto different machines).

Suggested machine: Ubuntu 24.04 LTS, 4 vCPU, 8 GiB RAM, 100 GB disk. 2 vCPU / 4 GiB runs the API and light jobs only (one worker, `--concurrency=1`).

## 1. DNS and the server's door

1. Point `api.facecloudai.com` (an `A` record) at the VPS. Wait for it to resolve before asking for a certificate.
2. Create a non-root sudo user, put your SSH key in, then in `/etc/ssh/sshd_config` set `PermitRootLogin no` and `PasswordAuthentication no`; reload `ssh`.
3. Firewall: `ufw default deny incoming; ufw allow OpenSSH; ufw allow 80,443/tcp; ufw enable`. **PostgreSQL (5432) and Redis (6379) are never opened**: they listen on `127.0.0.1` only.
4. `apt install fail2ban unattended-upgrades` and leave the defaults on.

## 2. Packages

```
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3.12-dev build-essential libpq-dev libglib2.0-0 libgomp1 \
                    postgresql redis-server nginx certbot python3-certbot-nginx logrotate git
```

## 3. Accounts and directories

```
sudo useradd --system --create-home --home-dir /srv/facecloud --shell /usr/sbin/nologin facecloud
sudo install -d -o facecloud -g facecloud -m 0750 /srv/facecloud/app /var/lib/facecloud /var/lib/facecloud/media /var/lib/facecloud/models \
                                                   /var/log/facecloud /var/backups/facecloud
sudo install -d -o facecloud -g www-data -m 0750 /run/facecloud           # the gunicorn socket (systemd recreates it, see the unit)
sudo install -d -o root -g facecloud -m 0750 /etc/facecloud
```

Permissions that matter: `/var/lib/facecloud/media` is `0750 facecloud:facecloud`: **Nginx (`www-data`) cannot read it**, which is what keeps the photos private.
`/etc/facecloud/facecloud.env` (section 6) is `0640 root:facecloud`: the service can read it, other users cannot.

## 4. PostgreSQL

```
sudo -u postgres psql <<'SQL'
CREATE ROLE facecloud LOGIN PASSWORD '<paste the DB_PASSWORD you generated>';
CREATE DATABASE facecloud OWNER facecloud ENCODING 'UTF8' LC_COLLATE 'en_US.UTF-8' LC_CTYPE 'en_US.UTF-8' TEMPLATE template0;
SQL
```

Keep `listen_addresses = 'localhost'` (the default). Generate the password with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` and put it only in the environment file.

## 5. Redis

Edit `/etc/redis/redis.conf`: `bind 127.0.0.1 ::1`, `protected-mode yes`, `appendonly yes`. Restart. The three databases used are `/0` broker, `/1` results, `/2` cache (rate limits live in the cache, so it must be Redis, not per-process memory).

## 6. Code, virtualenv, environment

```
sudo -u facecloud git clone git@github.com:ijaz-student021/FaceCloud_backend.git /srv/facecloud/app
cd /srv/facecloud/app && sudo -u facecloud git checkout <the release tag or commit>      # always a tag or commit, never "main" blindly
sudo -u facecloud python3.12 -m venv /srv/facecloud/venv
sudo -u facecloud /srv/facecloud/venv/bin/pip install -r requirements.txt
```

Create `/etc/facecloud/facecloud.env` (`0640 root:facecloud`) from `docs/deployment/vps.env.example` and fill every value. **The names below are all required**
unless marked optional; `python manage.py check` refuses to start production if any is missing or unsafe (the guard prints every problem at once).

| Name | What |
|---|---|
| `DJANGO_DEBUG` | `False` |
| `DJANGO_SECRET_KEY` | 50+ random characters, its own value |
| `IDENTITY_EMBEDDING_KEY` | 32+ random characters, different from every other key |
| `IMAGE_ENCRYPTION_ENABLED`, `IMAGE_ENCRYPTION_KEY` | `True`; 32+ random characters, different again. **Losing this key makes every stored photo unreadable.** |
| `DJANGO_ALLOWED_HOSTS` | `api.facecloudai.com` |
| `DB_ENGINE`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | `django.db.backends.postgresql`, `facecloud`, `facecloud`, the password, `127.0.0.1`, `5432` |
| `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `CACHE_URL` | `redis://127.0.0.1:6379/0`, `/1`, `/2` (written explicitly: the guard refuses the development defaults) |
| `CELERY_TASK_ALWAYS_EAGER` | `False` |
| `EMAIL_BACKEND`, `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`, `DEFAULT_FROM_EMAIL` | your SMTP provider; the account name and password are required |
| `PUBLIC_BACKEND_URL`, `FRONTEND_URL` | `https://api.facecloudai.com`, `https://www.facecloudai.com` |
| `CORS_ALLOW_ALL_ORIGINS`, `CORS_ALLOWED_ORIGINS` | `False`, `https://www.facecloudai.com` (only this) |
| `CSRF_TRUSTED_ORIGINS` | `https://api.facecloudai.com` (for the admin site; the browser app uses a bearer token and needs no CSRF entry; no wildcards) |
| `SECURE_SSL_REDIRECT` or `TRUST_PROXY_SSL_HEADER` | behind Nginx: `TRUST_PROXY_SSL_HEADER=True` (Nginx sets `X-Forwarded-Proto`) |
| `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` | `True` |
| `SECURE_HSTS_SECONDS`, `SECURE_HSTS_INCLUDE_SUBDOMAINS`, `SECURE_HSTS_PRELOAD` | `31536000`, `True`, `True` (only once HTTPS works everywhere on the domain) |
| `TRUSTED_PROXY_COUNT` | `1` (one Nginx in front). With `0` every visitor looks like Nginx and rate limits misbehave. |
| `BIOMETRIC_CONSENT_REQUIRED`, `FACIAL_DATA_RETENTION_DAYS` | `True`, a number of days you decided |
| `MEDIA_ROOT`, `STATIC_ROOT`, `STORAGE_BACKEND` | `/var/lib/facecloud/media`, `/srv/facecloud/static`, `filesystem` (or `s3` with `AWS_STORAGE_BUCKET_NAME`, see `aws-s3-private-storage.md`) |
| `LOG_FILE` (optional) | `/var/log/facecloud/app.log` (rotating; otherwise logs go to the journal) |
| model switches (optional) | start from `ec2-small-cpu.env.example`; anything non-commercial needs `NONCOMMERCIAL_MODELS_ACKNOWLEDGED` decided by the owner |

Never put the file in git, a chat or a ticket. Back the **keys** up separately from the data (a password manager or secret store): the database dump and the
media are useless without `IMAGE_ENCRYPTION_KEY` and `IDENTITY_EMBEDDING_KEY`.

## 7. First start of the application

```
set -a; . /etc/facecloud/facecloud.env; set +a            # as the facecloud user, for these commands only
cd /srv/facecloud/app
/srv/facecloud/venv/bin/python manage.py check --deploy --fail-level ERROR
/srv/facecloud/venv/bin/python manage.py migrate --noinput
/srv/facecloud/venv/bin/python manage.py collectstatic --noinput
/srv/facecloud/venv/bin/python manage.py createsuperuser           # the first administrator (verified, role admin)
/srv/facecloud/venv/bin/python manage.py database_info             # which database, how many rows, no migration left
```

`check --deploy` should print **no warning**, with one expected exception while no approved face model is configured: `identities.W001`. CI runs the same check
(job "Production configuration starts") with a throwaway environment, so a warning here is yours to read, not noise.

## 8. systemd

`/etc/systemd/system/facecloud-web.service`

```
[Unit]
Description=FaceCloud API (gunicorn)
After=network.target postgresql.service redis-server.service
[Service]
User=facecloud
Group=www-data
RuntimeDirectory=facecloud
RuntimeDirectoryMode=0750
WorkingDirectory=/srv/facecloud/app
EnvironmentFile=/etc/facecloud/facecloud.env
ExecStart=/srv/facecloud/venv/bin/gunicorn config.wsgi:application -c gunicorn.conf.py \
          --bind unix:/run/facecloud/gunicorn.sock --workers 3 --timeout 120
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/facecloud-worker.service` (jobs: enhancement, reports, packages; one job at a time keeps memory bounded)

```
[Unit]
Description=FaceCloud Celery worker
After=network.target postgresql.service redis-server.service
[Service]
User=facecloud
Group=facecloud
WorkingDirectory=/srv/facecloud/app
EnvironmentFile=/etc/facecloud/facecloud.env
ExecStart=/srv/facecloud/venv/bin/celery -A config worker -l info --concurrency=1
Restart=always
RestartSec=5
TimeoutStopSec=300
[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/facecloud-beat.service` (**exactly one** beat on the whole system: it schedules the stuck-job recovery, expiry and retention tasks)

```
[Unit]
Description=FaceCloud Celery beat
After=network.target redis-server.service
[Service]
User=facecloud
Group=facecloud
WorkingDirectory=/srv/facecloud/app
EnvironmentFile=/etc/facecloud/facecloud.env
ExecStart=/srv/facecloud/venv/bin/celery -A config beat -l info --schedule=/var/lib/facecloud/celerybeat-schedule
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
```

`sudo systemctl daemon-reload && sudo systemctl enable --now facecloud-web facecloud-worker facecloud-beat`. Linux uses the default worker pool (the `--pool=solo` advice is for macOS only).
To give image jobs their own worker (recommended once the heavy models run), set `ENHANCEMENT_QUEUE_NAME=inference` in the environment and start a second worker unit with `-Q inference --concurrency=1`
and the first with `-Q celery`.

## 9. Nginx and HTTPS

`/etc/nginx/sites-available/api.facecloudai.com` (then `ln -s` into `sites-enabled`, `nginx -t`, `systemctl reload nginx`):

```
server {
    listen 80;
    server_name api.facecloudai.com;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://$host$request_uri; }
}
server {
    listen 443 ssl http2;
    server_name api.facecloudai.com;
    # certificate lines are added by certbot
    client_max_body_size 2100m;                 # a ZIP may be up to MAX_ZIP_UPLOAD_SIZE (2 GiB); keep this just above it
    client_body_timeout 300s;
    proxy_read_timeout 180s;                    # gunicorn --timeout is 120 s
    proxy_request_buffering on;

    location /static/ { alias /srv/facecloud/static/; expires 7d; }
    # No /media/ block, on purpose: photos are served only through authenticated API endpoints.

    location / {
        proxy_pass http://unix:/run/facecloud/gunicorn.sock;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;      # overwrite, never append a client-supplied value
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

`sudo certbot --nginx -d api.facecloudai.com` obtains and installs the certificate and sets up renewal (`systemctl list-timers | grep certbot` should show it). Test renewal once: `sudo certbot renew --dry-run`.

## 10. Backups (daily) and a restore that was tested

`/usr/local/sbin/facecloud-backup` (`chmod 750`, owned by root):

```
#!/bin/sh
set -eu
DEST=/var/backups/facecloud; DAY=$(date +%F)
sudo -u postgres pg_dump -Fc facecloud > "$DEST/db-$DAY.dump"
tar -C /var/lib/facecloud -czf "$DEST/media-$DAY.tar.gz" media
find "$DEST" -name 'db-*.dump' -mtime +14 -delete
find "$DEST" -name 'media-*.tar.gz' -mtime +7 -delete
```

Cron: `/etc/cron.d/facecloud-backup` containing `15 2 * * * root /usr/local/sbin/facecloud-backup`. **A backup on the same disk is not a backup**: copy `/var/backups/facecloud` off the machine every night
(object storage with `rclone`, or a second VPS), and keep the two encryption keys in the secret store.

Restore rehearsal (do it once, on another machine, before the cutover): create the role and an empty database as in section 4, `pg_restore -d facecloud --no-owner db-<day>.dump`, unpack
`media-<day>.tar.gz` into `/var/lib/facecloud`, set the same environment file, run `manage.py database_info`, open one photo through the API. If the photo does not open, the keys do not match.

## 11. Logs

Gunicorn logs to the journal (`journalctl -u facecloud-web`), with share-link tokens redacted by the gunicorn logger in `gunicorn.conf.py`. If `LOG_FILE=/var/log/facecloud/app.log` is set the app also writes there, already rotated by Python; also add
`/etc/logrotate.d/facecloud`:

```
/var/log/facecloud/*.log {
    weekly
    rotate 8
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
```

Nginx rotates its own logs (`/etc/logrotate.d/nginx` ships with the package). Keep the journal bounded: `SystemMaxUse=500M` in `/etc/systemd/journald.conf`.

## 12. Smoke tests after every start

```
curl -sI https://api.facecloudai.com/api/docs/ | head -1                        # 200
curl -s -o /dev/null -w '%{http_code}\n' http://api.facecloudai.com/            # 301 to https
curl -s https://api.facecloudai.com/api/system/health/ -H "Authorization: Bearer <an admin access token>"   # status healthy, celery workers 1
curl -sI https://api.facecloudai.com/media/anything | head -1                    # 404: the media directory is not exposed
curl -sI -H 'Origin: https://evil.example' https://api.facecloudai.com/api/docs/ | grep -i access-control || echo "no CORS for other origins"
```

Then in the browser at `https://www.facecloudai.com`: register (the email must arrive), sign in, upload one photo, watch it reach `accepted`, open its thumbnail, enhance it, download it.

## 13. Cutover checklist (one pass, in this order)

1. [ ] A restore rehearsal passed (section 10) and the secret store holds `DJANGO_SECRET_KEY`, `IDENTITY_EMBEDDING_KEY`, `IMAGE_ENCRYPTION_KEY`, `DB_PASSWORD`, the SMTP password.
2. [ ] The release is a tag or commit that passed CI (all three jobs) and every PR for it is merged; `git log -1` on the server shows it.
3. [ ] `check --deploy --fail-level ERROR` is clean (only `identities.W001` may remain); `migrate --plan` lists what will run; `database_info` shows the expected database.
4. [ ] DNS for `api.facecloudai.com` resolves to this VPS; certificate issued; `certbot renew --dry-run` passed.
5. [ ] Take a fresh backup (`facecloud-backup`) **now**: it is the rollback point.
6. [ ] `migrate`, `collectstatic`, start `facecloud-web`, `facecloud-worker`, `facecloud-beat`; section 12 smoke tests pass.
7. [ ] Point the frontend at the API: `VITE_API_URL=https://api.facecloudai.com`, `VITE_USE_API=true`, build, upload `dist/` (the frontend's own checklist, including `.htaccess`).
8. [ ] Watch for 30 minutes: `journalctl -fu facecloud-web -u facecloud-worker`; the health endpoint; one real upload end to end. Rate-limit values for the day: `rate-limits.md`.

## 14. Rollback

Decide in the first 30 minutes; after users have uploaded, a database rollback loses their data, so prefer rolling the code forward.

* **Code only (no migration ran, or the migrations were additive):** `git checkout <previous tag>`, `pip install -r requirements.txt`, `systemctl restart facecloud-web facecloud-worker facecloud-beat`.
* **Code and a migration that must be undone:** stop the three services; `python manage.py migrate <app> <previous migration>` for each app that migrated (`migrate --plan` before the release told you which); then the code step above. If a migration cannot be reversed, restore the pre-cutover dump instead.
* **Full restore to the cutover backup:** stop the services, `dropdb facecloud && createdb -O facecloud facecloud`, `pg_restore -d facecloud --no-owner db-<day>.dump`, unpack `media-<day>.tar.gz`, previous code, start. **Everything uploaded since the backup is lost**: say so before choosing this.
* **Frontend:** re-upload the previous `dist/`; point `VITE_API_URL` back, or set `VITE_USE_API=false` to fall back to the demo data while the API is repaired.
* After any rollback run `database_info` and the section 12 smoke tests.

## 15. Routine

* **Deploy a release:** backup, `git fetch && git checkout <tag>`, `pip install -r requirements.txt`, `check --deploy`, `migrate`, `collectstatic`, `systemctl restart facecloud-web facecloud-worker facecloud-beat`, smoke tests.
* **After a restart that interrupts a job** (`DJANGO_DEBUG=False` runs jobs in the worker, so a worker restart can drop one): `manage.py reap_stuck_jobs --older-than-seconds 600`; the beat does it every five minutes anyway after the default allowance.
* **Rotate a secret:** `DJANGO_SECRET_KEY` signs sessions and tokens (everyone signs in again); the encryption keys support previous-key lists (`*_PREVIOUS_KEYS`), see `.env.example`: add the new key, keep the old in the previous list, never drop it until nothing is encrypted with it.
