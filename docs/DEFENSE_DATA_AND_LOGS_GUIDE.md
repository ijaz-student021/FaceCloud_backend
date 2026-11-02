# Where the data is stored, and how to show it (defence guide)

Every command below was run against a throwaway demo database on 2026-10-16. Use a **demo** database for the demonstration, never one you care about.

## 1. The answer in one paragraph

Records (users, uploads, enhancement results, identity groups, consents, audit events) live in the **database** (SQLite while developing, PostgreSQL in staging and production).
The photographs themselves live **outside the database as files** (a folder under `MEDIA_ROOT` while developing; a private S3 bucket in production). The database stores only the *path* of
each file plus its size, dimensions and SHA-256. With `IMAGE_ENCRYPTION_ENABLED=True` the files are **encrypted** (AES-256-GCM), so opening one directly shows unreadable bytes; the only way to
see the photograph is through the authenticated API, which checks ownership and decrypts. **Logs** are the console output (and an optional rotating file), and the **audit trail** is database tables.

## 2. Set up a demo (2 minutes)

```bash
export DB_NAME=/tmp/demo/demo.sqlite3 MEDIA_ROOT=/tmp/demo/media LOG_FILE=/tmp/demo/facecloud.log
export IMAGE_ENCRYPTION_ENABLED=True IMAGE_ENCRYPTION_KEY=any-demo-key-of-at-least-32-characters
mkdir -p /tmp/demo
python manage.py migrate
python scripts/seed_demo.py --photo ~/Desktop/images/<a photo>.jpeg --dark        # uploads + enhances one photo through the real API
DJANGO_SUPERUSER_USERNAME=admin DJANGO_SUPERUSER_EMAIL=admin@example.com DJANGO_SUPERUSER_PASSWORD='<pick one>' python manage.py createsuperuser --noinput
python manage.py runserver                                                     # then open http://127.0.0.1:8000/admin/
```

`--dark` darkens the photograph to **x0.40** of its light (a normal portrait, brightness about 70, becomes about 28; only a photograph so bright that x0.40 would leave it above 30 is darkened further, to 30). The script prints each photograph's brightness, its quality verdict and the reasons, and if the checks refuse a photograph it says so and carries on. The quality check refuses a photograph only when it is practically black (brightness under about 6) or has almost no contrast; a dark one is lifted. (x0.25, the earlier setting, left a normal portrait at brightness 18, which the check used to refuse.)

For the adaptive engine add `ENHANCEMENT_RENDER_POLICY=adaptive_v1` (and the model flags you want). The seed script prints the database path, the files folder and the ids it created. Your own `db.sqlite3` in the
project folder was **empty (0 bytes)** when checked: it has never been migrated, which is why a demo database is the safe choice.

## 3. See the database

**a) Django admin** (`/admin/`, sign in as the superuser): browse and search every table: users, upload batches, images, preprocessing results, enhancement results, identity groups, consents, activity events, authentication events.

**b) SQLite command line** (installed on macOS): `sqlite3 -header -column /tmp/demo/demo.sqlite3`, then:

```sql
.tables                                                -- every table (accounts_*, uploads_*, enhancement_*, identities_*, privacy_*, activity_*, ...)
select id, username, email, role, is_email_verified from accounts_user;
select id, owner_id, original_name, file_size, width, height, status, substr(checksum_sha256,1,12) as sha, original_file from uploads_imageupload;
select id, image_id, status, requested_level, applied_level, output_width, output_height, processing_duration_ms, enhanced_file from enhancement_imageenhancementresult;
select json_extract(applied_parameters,'$.adaptive.outcome'), json_extract(applied_parameters,'$.algorithm'),
       json_extract(applied_parameters,'$.identity_verification.state') from enhancement_imageenhancementresult;
select id, category, event, object_type, object_id, created_at from activity_activityevent order by id desc limit 10;   -- processing audit trail
select event, identifier, created_at from accounts_authevent order by id desc limit 10;                                  -- logins, failures, resets
```

Real output from the demo run: `partially_enhanced | adaptive_v1+zero_dce | not_verifiable` for the result row, and `enhancement_completed`, `batch_completed`, `quality_rejected` rows in the activity trail.
A GUI alternative is **DB Browser for SQLite** (open the same file). On PostgreSQL: `psql -h <host> -U <user> <database>` and the same `select` statements (use `applied_parameters -> 'adaptive' ->> 'outcome'` for JSON).

**c) Through the API**: `/api/docs/` (Swagger UI) lists every endpoint. For a user's own data: `/api/images/my/`, `/api/enhancements/results/`, `/api/user/activity/`, `/api/privacy/audit/`, `/api/privacy/export/`.
For administrators: `/api/system/audit/` (platform audit feed), `/api/system/health/` (database, queue, storage, every model's state), `/api/system/queue/`, `/api/system/jobs/failed/`.

## 4. See the stored files

Files are under `MEDIA_ROOT` in `users/<user id>/<kind>/<random name>`: `originals/`, `preprocessed/`, `enhanced/`, `thumbnails/`, `edited/`, `previews/`, `comparisons/`. The upload's file name is only
metadata in the database; the stored name is random. In the demo:

```
media/users/1/originals/…png    media/users/1/preprocessed/…png    media/users/1/enhanced/…png    media/users/1/thumbnails/…jpg
```

With encryption on, the first bytes of a stored image are `FCIMG1` (the file header), not an image signature:

```bash
head -c 6 /tmp/demo/media/users/1/originals/<file>.png     # prints FCIMG1: ciphertext, not viewable
```

To *see* the photograph you download it through the API (`GET /api/images/{id}/download/`, `GET /api/enhancements/results/{id}/download/`), which checks that the caller owns it and decrypts on the way out. In production
the same ciphertext sits in a private S3 bucket (Block Public Access on, no public URLs): the S3 console shows objects but not photographs. **Real S3 has not been validated here** (simulated only).
The encryption key lives in the server's environment, never in the database or the bucket.

## 5. See the logs

* **Console**: the terminal running `runserver` (or gunicorn, or the Celery worker) prints application logs. INFO and above from the application, WARNING and above from libraries. (Before 2026-10-16 INFO lines were silently dropped; fixed.)
* **File**: set `LOG_FILE=/path/facecloud.log` (rotating, 5 MB x 5). `LOG_LEVEL=DEBUG|INFO|WARNING` changes verbosity. Gunicorn writes its own access log with share-link tokens redacted.
* **One line per enhancement** (no photo content, no file paths):

```
INFO enhancement.adaptive.service adaptive_enhancement {"outcome":"partially_enhanced","operations":["low_light_enhancement"],"seconds":2.27,
 "megapixels":1.23,"faces":1,"conditions":["low_light","defocus_blur","face_poor"],"identity_state":"not_verifiable",
 "stages":[{"operation":"low_light_enhancement","status":"accepted","seconds":1.41,"models":["zero_dce"],"rejected_for":null}],"skipped":[],"unresolved":["defocus_blur"],"colour_method":"chromaticity"}
```

* **Summary of the last N days**: `python manage.py adaptive_report --days 7` prints outcome counts, the original-preserved rate, P50/P95 job time, queue wait, which operations were rejected and why, and the models run.
* **Audit trail (permanent, in the database)**: `accounts_authevent` (registrations, logins, failures, lockouts, password resets, logouts), `activity_activityevent` (uploads, batches, enhancements), `security_securityevent` and security alerts, `privacy_*`
  (consents and facial-data actions), `organizations_organizationauditevent`, `downloads_downloadevent`.
* Logs and audit rows never contain passwords, tokens (share-link and email-change paths are redacted), photo pixels or file paths.

## 6. Questions a panel may ask, and honest answers

| Question | Answer |
|---|---|
| Where are the photographs? | In files, not the database: `MEDIA_ROOT/users/<id>/…` in development, a private S3 bucket in production. The database holds the path, size, dimensions and checksum. |
| Can an administrator or someone with the disk just open them? | With encryption on, no: the files are AES-256-GCM ciphertext (`FCIMG1`). The key is in the server's environment. Access to a photograph is through the API, after an ownership check (another user gets a 404). |
| What is in the database about a face? | Face embeddings and identity groups, only with the user's recorded biometric consent (`privacy_biometricconsent`); the user can export their data and erase their facial data (`/api/privacy/...`). |
| How do you know what happened to a photograph? | `enhancement_imageenhancementresult.applied_parameters` records the analysis, each model that ran, what was rejected and why, the outcome and the identity check; plus the activity and audit tables and the structured log line. |
| What happens if a model fails or the result is unsafe? | The original is returned (`original_preserved`) with a plain sentence; nothing unvalidated is shown. |
| How long is data kept? | Previews, debug artifacts and download packages expire and are cleaned up; facial data has a retention setting (`FACIAL_DATA_RETENTION_DAYS`); account deletion removes the user's data. |
| Is it tested on real AWS? | Not yet: storage is tested against a simulated S3; the first real-bucket check is `python manage.py check_storage` with authorised, non-root credentials. |

## 7. Housekeeping before the demonstration

* Use a demo database and a demo `MEDIA_ROOT` (section 2). The project's `media/` folder held 578 files from earlier test runs (users 1-3, unencrypted) and `db.sqlite3` is empty, so they do not match: do not demonstrate from them.
* Debug artifacts (`ADAPTIVE_DEBUG_ARTIFACTS`) store copies of photographs and must stay off for a demonstration of privacy; `manage.py cleanup_enhancement_debug --older-than-hours 0` removes any.
* Never open the real photographs on a projector screen during the privacy discussion: use the seeded demo photograph.
