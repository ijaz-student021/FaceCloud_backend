# Feature 41: Privacy and facial-data security

## Status

Backend complete for consent, erasure with verification, retention, export,
audit reporting, deployment checks, embedding encryption with key rotation,
and temporary-file and orphaned-media cleanup (`privacy` app and
`identities/storage.py`). Still open: encryption of stored images
(Feature 43) and the consent screens in the frontend. Both jobs are on the Celery
beat schedule (see [Feature 20 operations](20-background-job-operations.md)). Details under "Open items".

## What counts as facial data

| Data | Treated as | Why |
|---|---|---|
| Face embeddings, identity-group centroids, group edit history, group ZIP exports | **Biometric** - covered by consent, erasure, retention | They identify a person |
| Uploaded photos, enhancement results | The user's content | Deleted through the normal image flow and on account deletion |
| Face-detection boxes and landmarks, duplicate-image hashes | Image metadata | They locate a face in an image or match a duplicate image; they cannot recognise a person |

## Consent

`BiometricConsent` is an append-only history (never edited). The newest row
per purpose is the current state, and consent only counts if it was granted
against the **current** policy version (`BIOMETRIC_CONSENT_POLICY_VERSION`),
so changing the policy text asks everyone to consent again. Repeating the
current state does not add a row. Every change is written to the user's
activity timeline (`privacy` category).

Consent is enforced when `BIOMETRIC_CONSENT_REQUIRED=True`:

- identity analysis cannot be queued without it (`enqueue_identity_analysis`);
- a queued analysis is failed safely if consent was withdrawn before it ran
  (`process_identity_analysis`);
- the enhancement identity guard skips itself, since it computes face
  embeddings (transiently, in memory, nothing stored).

It defaults to off so existing deployments keep working, and `manage.py
check --deploy` warns until it is enabled. Face detection alone (used by
quality and validation) is not gated, because it does not identify people.

## Erasure and verification

`erase_facial_data(user)` deletes analyses, faces, memberships, identity
groups, group edit history and group export ZIPs, then **proves** it: it
recounts the database and lists the user's export directory, and records
`verified` on a `PrivacyRequest`. An unverified erasure is marked `failed`,
logged as an error, returned to the caller as `verified: false`, and counted
in the admin audit report. Account deletion cascades through the same rows
(tested: no rows and no files remain).

`PrivacyRequest` stores counts only, never names or embeddings, and survives
account deletion with the user set to null.

## Retention

`FACIAL_DATA_RETENTION_DAYS` (default 0 = keep until the user deletes).
`purge_expired_facial_data` removes analyses older than that, recomputes the
centroid of each affected group, and deletes groups left with no members. It
writes one audit record per run that purged something.

It runs daily at 03:15 from the Celery beat schedule. To run it by hand:

```powershell
python manage.py purge_expired_facial_data
```

Note that
with consent enforcement **off**, preprocessing an existing image can create
embeddings again after a purge; retention is only meaningful together with
`BIOMETRIC_CONSENT_REQUIRED=True`.

## Temporary files and orphaned media

Two kinds of leftovers can hold face photos long after the request that made
them (`privacy/cleanup.py`, `manage.py cleanup_temporary_files`, Celery task
`privacy.cleanup_temporary_files`):

1. **Stale temp archives.** The package and identity-export builders write a
   ZIP into the system temp directory (`delete=False`) and delete it in a
   `finally` block. A worker killed mid-build (out of memory, the hard task
   time limit, a crash) never reaches it, so a ZIP of users' photos, possibly
   gigabytes, stays forever. Files named `facecloud-package-*.zip` or
   `facecloud-identity-export-*.zip` older than `TEMP_FILE_MAX_AGE_HOURS`
   (default 6) are deleted. Nothing else in the temp directory is touched, and
   directories and symlinks are skipped. A startup check (`privacy.E009`)
   requires the limit to exceed `CELERY_TASK_TIME_LIMIT`, so a build still in
   progress can never be deleted.
2. **Orphaned media.** A file saved to storage whose database row was never
   committed is referenced by nothing, so neither the delete signals nor
   account deletion will ever remove it; it would quietly break the "no data
   remains" guarantee. The scanner discovers every file field in the project
   dynamically, lists `users/<id>/<folder>/` for the seven known folders
   through the storage API (so it works with S3 later), and treats a file as an
   orphan only if no row references it **and** it is older than
   `ORPHAN_MEDIA_GRACE_HOURS` (default 24). A test ties the folder list to every
   file field, so a new upload path cannot silently be treated as orphaned.

**Orphans are reported, not deleted, by default.** Removing user files on a
heuristic must never be a surprise: deletion needs
`ORPHAN_MEDIA_CLEANUP_ENABLED=True` or `--delete-orphans`, and `--dry-run`
overrides both. Runs that delete something are recorded as a `temp_cleanup`
privacy request (counts only, never file names). The admin audit report shows
how many stale archives currently exist.

On this project's real media folder the scanner saw 51 files on disk and 51
database references, an exact match, so it reported no orphans.

It runs every two hours from the beat schedule (it must run more often than
`TEMP_FILE_MAX_AGE_HOURS`). To run it by hand:

```powershell
python manage.py cleanup_temporary_files
```

## Export

`GET /api/privacy/export/` returns the user's own data as a JSON attachment:
account, consent history, image and enhancement metadata, identity group
names, sign-in and activity events, and past privacy requests. **Embeddings
and centroids are never exported** (only a count), so a leaked export cannot
be used to recognise the person. Each collection is capped at
`PRIVACY_EXPORT_MAX_ROWS` and flagged in `truncated`.

## Encryption at rest - what is actually implemented

Face embeddings and group centroids are stored as `protected_*` binary
fields (`identities/storage.py`) in **AES-256-GCM**:

```text
b"FCID2" | key id (1 byte) | nonce (12) | ciphertext | GCM tag (16)
```

The header (format marker and key id) is authenticated as associated data,
so it cannot be edited or relabelled. The AES key is derived from the
configured secret with HKDF-SHA256 bound to the key id. `cryptography` is now
a dependency (`requirements.txt`).

The earlier format (`FCID1`, a hand-built keyed-BLAKE2s stream with an
HMAC tag) is **read-only**: nothing writes it any more, and it is verified
against a real payload the old code produced (a pinned test fixture), so
upgrading cannot strand existing data. Its tag identifies which configured
key protected it.

### Key rotation runbook

1. Generate a new secret (32+ random characters).
2. Set `IDENTITY_EMBEDDING_KEY_ID` to the next unused id (1-255), set
   `IDENTITY_EMBEDDING_KEY` to the new secret, and list the retired key(s) as
   `IDENTITY_EMBEDDING_PREVIOUS_KEYS="1:oldsecret"` (`id:secret`, comma
   separated; secrets must not contain commas). The old key that protected
   `FCID1` records goes in this list too, under any unused id. A malformed
   list stops startup rather than being ignored.
3. Restart. Everything stays readable: new records use the new key, old ones
   are found through the previous-keys list.
4. `python manage.py reencrypt_identity_embeddings --dry-run`, then run it
   without `--dry-run`. It rewrites every face embedding and group centroid
   in batches, is idempotent and safe to interrupt, leaves `updated_at`
   untouched (it orders identity-group candidates), leaves unreadable records
   exactly as they were, and exits with an error if any could not be
   decrypted. Non-dry runs are recorded as a `key_rotation` privacy request.
5. The admin audit report (`encryption.stored_records`) shows
   `rotation_needed: false` once no record uses an older key or format.
   Remove `IDENTITY_EMBEDDING_PREVIOUS_KEYS` (`check --deploy` warns with
   `privacy.W005` until you do). Retiring a key too early makes its records
   unreadable, which is rejected rather than returning wrong data.

### Findings from this review

1. **Fixed: a blank key.** `.env.example` ships `IDENTITY_EMBEDDING_KEY=`
   blank and the README says to copy it to `.env`. That parsed as an empty
   string, so embeddings would have been protected with the SHA-256 of
   nothing (a public constant). A blank value now falls back to
   `SECRET_KEY`, and `manage.py check --deploy` reports `privacy.E005` if it
   is ever empty.
2. **Deploy check `privacy.E001`:** the key must not fall back to
   `SECRET_KEY`. Rotating the Django secret would otherwise make every
   embedding undecryptable, and a leaked Django secret would expose them.
   Set a dedicated key of 32+ characters (`privacy.W001`).
3. **Fixed: hand-built cipher and no rotation.** Replaced by AES-GCM with
   key ids and the re-encryption command above.
4. **Still not encrypted at rest:** uploaded/enhanced images on disk
   (Feature 43), and face boxes/landmarks in the database. Use disk- or
   database-level encryption in production.
5. **Limit:** the key is derived from a configured secret, so it is only as
   strong as that secret. Keep it in a secret manager, not in the repository.

## Endpoints

| Method and path | Purpose |
|---|---|
| `GET /api/privacy/consents/` | Current consent state and policy version |
| `POST /api/privacy/consents/` | Grant (`policy_version` required) or withdraw; `erase_facial_data: true` on withdrawal also erases |
| `GET /api/privacy/facial-data/` | Counts of biometric data stored for the user |
| `DELETE /api/privacy/facial-data/` | Erase and verify (body must be `{"confirm": true}`) |
| `GET /api/privacy/export/` | JSON data export |
| `GET /api/privacy/audit/` | Administrator-only aggregate report (consent counts, requests, unverified erasures, retention, encryption posture) |

## Settings

`BIOMETRIC_CONSENT_REQUIRED`, `BIOMETRIC_CONSENT_POLICY_VERSION`,
`FACIAL_DATA_RETENTION_DAYS`, `PRIVACY_EXPORT_MAX_ROWS`,
`IDENTITY_EMBEDDING_KEY`. Throttle scopes: `privacy_consent`,
`privacy_export`, `privacy_erasure`.

## Open items

- Frontend: consent screen, "delete my face data", export download.
- Decide whether to enable `ORPHAN_MEDIA_CLEANUP_ENABLED` after reviewing a
  few `--dry-run` reports.
- In production, set a dedicated `IDENTITY_EMBEDDING_KEY`, then run
  `reencrypt_identity_embeddings` once so any legacy records are upgraded.
