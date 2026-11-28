# Release gate, 2026-11-28: what ships, in which order, how to go back

**Nothing in this file has been executed on the VPS.** It is the plan the owner approves step by step. Every production-changing step below is marked **[APPROVAL]**.

## What the release candidate contains

Backend branch `release/backend-rc` (CI-only Draft PR #182) = `main` + #177 (PostgreSQL invitation hotfix) + #175 (download approval, OFF by default) + #176 (owner invitation, personal/organization isolation) + #179 (total-user plan limits 5 / 25 / 100) + #180 (member leave request) + #181 (suspended-member content lock) + #183 (Celery results ignored). Frontend branch `release/frontend-rc` (Draft PR #14) = `main` + frontend #13 + #12 (production builds can never be demo builds) + #11 + seat UI + leave-request UI.

Real merge order into `main` (each retargeted to `main` as the one below lands): backend **#177, #175, #176 (rebase: delete its 0014, regenerate as 0015), #179, #180, #181, #183**; frontend **#14** (or #13, #12, #11 in that order if the combined PR is not used). Deploy only from a tag or commit on `main`.

## Migrations (all additive; applied to a populated copy of the real local database with no existing row changed)

On a database at `organizations 0013` the plan is: `organizations.0014` (download approval fields), `downloads.0004`, `notifications.0011`, `organizations.0015` (pending owner invitation), `notifications.0012` and `organizations.0016` (leave request table). Download approval stays OFF for every organization (`download_approval_required` defaults to false).

## Step 0: read-only audit of the VPS (no approval needed, changes nothing)

```
ssh -t deploy@2.24.154.198 'bash -s' < ~/FaceCloud_ops/vps_readonly_audit.sh | tee ~/Downloads/vps_audit_$(date +%F).txt
```
It prints services, nginx hostnames/timeouts, certificates, unit timeouts, the environment file's key NAMES (values only for a fixed safe list), the database engine, migration state and counts, pending migration plan, backups and media size. Read it before anything else: the VPS database is its own dataset and is not the 17-user local one.

## Step 1: backups and a restore proof **[APPROVAL: it writes backup files on the server]**

1. Database: `sudo /usr/local/sbin/facecloud-backup` if installed (the audit says), else `sudo -u postgres pg_dump -Fc facecloud > /var/backups/facecloud/db-before-release-$(date +%F-%H%M).dump`.
2. **Prove it restores:** `sudo -u postgres createdb facecloud_restore_test && sudo -u postgres pg_restore -d facecloud_restore_test <dump> && sudo -u postgres psql -d facecloud_restore_test -c 'select count(*) from accounts_user'` then drop the test database. The counts must equal the live ones.
3. Code is backed up by `deploy_vps.sh` (`/var/backups/facecloud/app-before-<REL>-*.tgz`); keep a copy of `/etc/facecloud/facecloud.env` (`sudo cp -p`).
4. Media (private photos) is not touched by this release; confirm the media backup/snapshot exists (Hostinger VPS snapshot) before the first migration.

## Step 2: environment check on the VPS **[read the audit output first]**

`FRONTEND_URL=https://www.facecloudai.com` (invitation links are built from it), `DJANGO_DEBUG=False`, `CELERY_TASK_ALWAYS_EAGER=False`, `CORS_ALLOWED_ORIGINS=https://www.facecloudai.com`, `EMAIL_*` set, **no** `ORG_PLAN_*` overrides unless wanted. New optional knobs: `ORG_PLAN_<FREE|TEAM|ENTERPRISE>_USERS` (default 5 / 25 / 100), `CELERY_TASK_IGNORE_RESULT` (default true).

## Step 3: backend deploy **[APPROVAL]**

```
cd <repo on the Mac at the approved tag/commit on main>
bash ~/FaceCloud_ops/deploy_vps.sh <release-name>
```
The script backs up the code (and the database if the backup script exists), replaces the code, installs requirements, runs `check --deploy`, `migrate`, `collectstatic`, restarts `facecloud-web facecloud-worker facecloud-beat` and smoke-tests the gunicorn socket. Afterwards: `curl -s https://api.facecloudai.com/api/docs/ -o /dev/null -w '%{http_code}\n'` must print 200.

## Step 4: personal-workspace backfill **[APPROVAL]**

Accounts created before the isolation work have no personal workspace (locally 10 of 17). Dry run first, read the list, then apply:
```
sudo -u facecloud bash -c 'set -a; . /etc/facecloud/facecloud.env; set +a; cd /srv/facecloud/app && /srv/facecloud/venv/bin/python manage.py ensure_personal_workspaces'
# only after reading the list:  ... ensure_personal_workspaces --apply
```
It is idempotent (a second apply creates nothing) and touches no image, folder or membership of any existing organization (verified on a database copy).

## Step 5: frontend **[APPROVAL]**

```
FRONTEND_REF=origin/main bash ~/FaceCloud_ops/build_frontend.sh      # builds exactly that git commit, refuses anything that is not API mode
FRONTEND_REF=origin/main bash ~/FaceCloud_ops/deploy_frontend.sh      # dry run
FRONTEND_REF=origin/main bash ~/FaceCloud_ops/deploy_frontend.sh --go # uploads; the old site is backed up to ~/FaceCloud_ops/frontend_backups/<time>
curl -s https://www.facecloudai.com/ | grep -o 'assets/index-[^"]*\.js'   # must show the new bundle name
```
Because Hostinger rebuilds the site when the frontend repo's `main` changes, merging frontend `main` replaces the live bundle by itself within a minute: with frontend PR #12 merged that build fails instead of shipping a demo bundle (it needs `VITE_USE_API=true` and `VITE_API_URL=https://api.facecloudai.com`, which the guard checks).

## Step 6: smoke test (after 3 to 5)

Owner enters their own credentials privately: sign in, open the personal dashboard, My Images, Person Folders, Admin console (real counts), create an organization by owner e-mail to a **test mailbox the owner names** (this is the first real e-mail: **[APPROVAL]**), accept it, invite a moderator and a member, request/approve/deny a leave request. Download approval stays OFF; enable it only for a pilot organization with an active moderator after all of this passes.

## Rollback

| What went wrong | Go back by |
|---|---|
| Backend code | `sudo tar -xzf /var/backups/facecloud/app-before-<REL>-*.tgz -C /srv/facecloud && sudo systemctl restart facecloud-web facecloud-worker facecloud-beat`. The migrations are additive: old code simply ignores the new tables and columns, so the database can stay. |
| Data corrupted (not expected) | Stop the three services, `pg_restore --clean -d facecloud <dump>` from Step 1, restart. Photos on disk are never changed by this release. |
| Frontend | Upload the previous folder from `~/FaceCloud_ops/frontend_backups/<time>` (Hostinger File Manager) or re-run `deploy_frontend.sh --go` with the old commit via `FRONTEND_REF`. |
| An invitation/leave feature misbehaves | The features are opt-in by use: nothing happens until an administrator creates an organization by e-mail or a member asks to leave. Download approval stays off. |

## Single-VPS consolidation (optional after the release is stable) **[APPROVAL: DNS and SSL]**

Today (measured read-only 2026-11-28): `www.facecloudai.com` and the apex resolve to Hostinger's CDN/shared hosting (`*.hstgr.net`, Let's Encrypt certificate to 2026-12-30); `api.facecloudai.com` is the VPS `2.24.154.198` behind nginx (certificate to 2027-02-22); the name servers are `dns-parking.com` (Hostinger DNS). Plan, each step reversible: (1) lower the TTL of the `www` and apex records a day before; (2) on the VPS add an nginx server block for `www.facecloudai.com` and `facecloudai.com` serving a `dist` folder owned by `deploy` (`root /srv/facecloud/web;`, `try_files $uri /index.html;`, long cache for `/assets/`, no cache for `index.html`; apex redirects to `www`); (3) test it before DNS with `curl --resolve www.facecloudai.com:443:2.24.154.198 https://www.facecloudai.com/` after `certbot --nginx -d www.facecloudai.com -d facecloudai.com` (needs the names to resolve to the VPS, so request the certificate right after step 4 or use the DNS challenge); (4) change the `A` records of `www` and the apex to `2.24.154.198`; (5) keep the Hostinger hosting untouched for a week as the fallback (rollback = put the old records back; DNS TTL bounds the switch time). Keep one database: the VPS PostgreSQL stays authoritative.

## Approvals the owner must give

1. Merge of the PRs in the order above. 2. Step 1 (server backup files). 3. Step 3 (deploy, production migrations). 4. Step 4 (`--apply` of the backfill). 5. Step 5 (frontend upload / merge of frontend `main`). 6. The first real e-mail to a named test mailbox. 7. Any DNS/SSL change.
