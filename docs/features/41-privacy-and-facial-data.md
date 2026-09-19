# Feature 41: Privacy and facial-data security

## Status

Backend complete for consent, erasure with verification, retention, export,
audit reporting and deployment checks (`privacy` app). Still open: temporary
processing-file cleanup, a review of the embedding cipher and key rotation,
encryption of stored images (Feature 43), and the consent screens in the
frontend. Details under "Open items".

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

Run it from cron (this repo has no Celery beat schedule):

```powershell
python manage.py purge_expired_facial_data
```

or schedule the Celery task `privacy.purge_expired_facial_data`. Note that
with consent enforcement **off**, preprocessing an existing image can create
embeddings again after a purge; retention is only meaningful together with
`BIOMETRIC_CONSENT_REQUIRED=True`.

## Export

`GET /api/privacy/export/` returns the user's own data as a JSON attachment:
account, consent history, image and enhancement metadata, identity group
names, sign-in and activity events, and past privacy requests. **Embeddings
and centroids are never exported** (only a count), so a leaked export cannot
be used to recognise the person. Each collection is capped at
`PRIVACY_EXPORT_MAX_ROWS` and flagged in `truncated`.

## Encryption at rest - what is actually implemented

Face embeddings and group centroids are stored as `protected_*` binary
fields (`identities/storage.py`): a keyed BLAKE2s keystream XOR-ed over the
float vector, with an HMAC-SHA256 tag (encrypt-then-MAC, random 16-byte
nonce, `FCID1` format marker). The key is derived from
`IDENTITY_EMBEDDING_KEY`.

Findings from this review:

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
3. **Not fixed, recommended:** the cipher is a hand-built construction. It is
   not obviously broken, but a standard AEAD (AES-GCM through the
   `cryptography` package) with key versioning would be preferable, together
   with a re-encryption command for key rotation. That means a data
   migration of every stored embedding, so it is left as a deliberate
   follow-up rather than changed silently.
4. **Not encrypted at rest:** uploaded/enhanced images on disk (Feature 43),
   and face boxes/landmarks in the database. Use disk- or database-level
   encryption in production.

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

- Temporary-processing cleanup (orphaned temp files from failed builds).
- Embedding cipher review and key rotation (finding 3 above).
- Frontend: consent screen, "delete my face data", export download.
- Schedule the retention purge in the deployment.
