# Merge order, migration plan and backup/restore (for review)

Nothing in this document has been executed. No pull request is merged and no
existing database has been migrated. Follow the steps only after review.

## 1. Merge dependencies

| Order | PR | Branch → base | Depends on | Notes |
|---|---|---|---|---|
| 1 | [#67](https://github.com/ijaz-student021/FaceCloud_backend/pull/67) | `fix/pipeline-review-followups` → `main` | — | Audit fixes; adds enhancement migrations 0007–0008 |
| 2 | [#68](https://github.com/ijaz-student021/FaceCloud_backend/pull/68) | `feat/backend-frontend-alignment` → `fix/pipeline-review-followups` | #67 | After #67 merges, change the base to `main`; GitHub then shows only #68's own commits |

* Merge #67 with a merge commit (not squash), so #68's history stays a clean
  continuation.
* Do not merge the open dependabot PRs (#63–#66) in between. They change torch,
  torchvision, onnx and cryptography floors. Re-run the worker test suite on
  them separately afterwards.
* The branch contents were last verified at `658b860`: web and worker suites
  pass, PostgreSQL suite passes, queue harness 8/8. Re-run the checks in §5 on
  the merge result before relying on it.

## 2. Which databases exist

| Database | Contents | Migration state | Action |
|---|---|---|---|
| `/Users/Ali/FaceCloud_backend/db.sqlite3` (this checkout's `.env`) | **Empty file (0 bytes)** | nothing applied (100 pending) | Back up anyway (trivial), then `migrate` builds everything |
| `/Users/Ali/Projects/FaceCloud_backend/db.sqlite3` (the second copy) | **Has data: 2 users** | same as `main` (accounts 0006, enhancement 0006, moderation 0005, organizations 0008) | The one that needs the careful procedure below |
| PostgreSQL `facecloud` on `/tmp` (8.5 MB) | No Django tables (`django_migrations` absent) | not a FaceCloud database | Leave alone; confirm what it is before using the name |

## 3. Migrations to be applied (from `main`)

All are additive (new tables, new nullable or defaulted columns, new choices, a
replaced partial unique constraint). None deletes, renames or rewrites existing
rows.

| Migration | Change | Data impact |
|---|---|---|
| enhancement 0007_result_processing_claim | claim token and claimed_at columns | none; defaults |
| enhancement 0008_claim_task_and_attempt | claim task id and attempt columns | none; defaults |
| accounts 0009_user_profile_fields_and_avatar | profile columns, avatar (renumbered from 0007 after `main` took 0007/0008) | none; empty defaults |
| accounts 0010_user_sessions | new `UserSession` table; auth-event choices (renumbered from 0008) | none |
| accounts 0011_account_invitations | new `AccountInvitation` table; auth-event choices (renumbered from 0009) | none |

`accounts/0012_remove_two_factor` (renumbered from 0010) then drops the two-factor tables.

A database that already ran these under their old names (`0007_user_profile_fields_and_avatar`, `0008_user_sessions`, `0009_account_invitations`, `0010_remove_two_factor`) must not simply run `migrate`: the columns exist, so Django would try to add them again. Back it up, then either rename those rows in `django_migrations` to the new names or recreate the database.
| moderation 0006_report_period | `date_from`, `date_to`, `period_key` (`"all"`); constraint replaced | Existing reports get `period_key="all"`. **Precondition:** at most one queued or generating report per (organization, type, format); this already held under the old constraint |
| moderation 0007_general_activity_report | report-type choice | none |
| organizations 0009_moderator_preview_and_reprocess_actions | audit-action choices | none |
| contact 0001, security 0001, camera 0001 | new tables | none |

Existing JWTs carry no session id. They stay valid until they expire (at most
7 days) and are simply not listed among the sessions.

## 4. Backup and restore

### SQLite (both local files)

The web server and workers for that checkout must be stopped, so nothing
writes during the copy.

```sh
cd /Users/Ali/Projects/FaceCloud_backend            # or the checkout being upgraded
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p ~/FaceCloud_backups/$STAMP
sqlite3 db.sqlite3 ".backup '$HOME/FaceCloud_backups/$STAMP/db.sqlite3'"   # consistent online copy
sqlite3 ~/FaceCloud_backups/$STAMP/db.sqlite3 "PRAGMA integrity_check;"    # expect: ok
venv/bin/python manage.py dumpdata --natural-foreign --natural-primary \
  --exclude contenttypes --exclude auth.permission --indent 2 \
  > ~/FaceCloud_backups/$STAMP/data.json                                   # portable second copy
tar -czf ~/FaceCloud_backups/$STAMP/media.tar.gz media/                    # uploaded files, if any
shasum -a 256 ~/FaceCloud_backups/$STAMP/* > ~/FaceCloud_backups/$STAMP/SHA256SUMS
```

**Restore:** stop the processes, then
`cp ~/FaceCloud_backups/<stamp>/db.sqlite3 db.sqlite3` and
`tar -xzf ~/FaceCloud_backups/<stamp>/media.tar.gz`. Check out the commit that
matches the restored schema (current `main`) before starting again.

### PostgreSQL (staging, or any later PostgreSQL deployment)

```sh
pg_dump -h <host> -U <user> -Fc -d <db> -f ~/FaceCloud_backups/$STAMP/<db>.dump
pg_restore --list ~/FaceCloud_backups/$STAMP/<db>.dump > /dev/null && echo "dump readable"
```

**Restore** into a new database, then switch the application to it:
`createdb <db>_restored && pg_restore -d <db>_restored --no-owner ~/FaceCloud_backups/<stamp>/<db>.dump`

For the staging stack:
`docker compose -f docker-compose.staging.yml --env-file .env.staging exec db pg_dump -U facecloud -Fc facecloud_staging > staging.dump`

### Rehearse first

Restore the backup into a scratch copy and migrate that, before touching the
real file:

```sh
cp ~/FaceCloud_backups/$STAMP/db.sqlite3 /tmp/rehearsal.sqlite3
DB_NAME=/tmp/rehearsal.sqlite3 venv/bin/python manage.py migrate --plan     # review the list (matches §3)
DB_NAME=/tmp/rehearsal.sqlite3 venv/bin/python manage.py migrate
DB_NAME=/tmp/rehearsal.sqlite3 venv/bin/python manage.py check
sqlite3 /tmp/rehearsal.sqlite3 "select count(*) from accounts_user;"      # still 2
```

## 5. Migration procedure (after #67 and #68 are merged)

1. `git pull` on `main` in the checkout being upgraded; install requirements
   (`venv/bin/pip install -r requirements.txt`).
2. Stop its web server and workers.
3. Back up (§4) and verify the checksum file.
4. Rehearse on a copy (§4) and compare row counts of key tables
   (`accounts_user`, `uploads_imageupload`, `enhancement_imageenhancementresult`).
5. `venv/bin/python manage.py migrate --plan`: confirm it lists exactly §3.
6. `venv/bin/python manage.py migrate`
7. `venv/bin/python manage.py check` and `makemigrations --check --dry-run`
   (both clean), then start the processes.
8. **Smoke test:** log in, upload one image, enhance it, download it; list
   sessions; request a report.
9. **If anything fails:** stop, restore the backup (§4), check out the previous
   `main` commit and restart. Migrations are not reversed in place; the backup
   is the rollback.

New settings to add to `.env` when upgrading. All have safe defaults in
development; `CACHE_URL` is required once `DJANGO_DEBUG=False`:

* `CACHE_URL`
* `CONTACT_RECIPIENT_EMAILS`
* `CELERY_VISIBILITY_TIMEOUT` (at least the time limit plus the claim lease; default 6300)
* optional thresholds: `SECURITY_ALERT_*`, `*_RETENTION_DAYS`,
  `ACCOUNT_INVITATION_TTL_HOURS`

## 6. Validation status by hardware

| Hardware | What was run | Result |
|---|---|---|
| **CPU / Apple M1 (MPS)** | All test suites (web, worker incl. slow, PostgreSQL); real checkpoints for every model on CPU or MPS; queue harness with real GFPGAN on MPS; visual evaluation on real photos | Passing. Quality limits are documented in `docs/evaluation/` |
| **CUDA (NVIDIA)** | Nothing | **Not validated.** No CUDA timing, memory or numerical results exist. Run `scripts/validate_models.py` and the GPU runbook before enabling GPU-only settings |
| **Docker (Linux, CPU)** | Configuration validated only (`docker compose config`, Django check) | Not built or run yet |
