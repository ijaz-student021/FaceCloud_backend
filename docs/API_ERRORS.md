# API error catalogue

What the frontend can rely on when a request fails. The shapes below are
asserted by `config/test_error_contract.py` and `accounts/tests.py`, and the 401
and 429 responses are added to every operation in the OpenAPI schema
(`config/schema_hooks.py`).

## Shapes

| Status | Body | When |
|---|---|---|
| 400 | `{"field": ["message", ...], ...}` | Request validation failed. Keys are field names. |
| 400 | `{"detail": ["message"], "code": "consent_required"}` | A face-recognition feature was refused because the person has not given consent (identity analysis, face search, person collage). Match on `code`. |
| 400 | `{"detail": ["message"]}` | A rule raised inside a serializer (login failures, password reset link, batch has nothing to enhance). Note `detail` is a **list** here. |
| 401 | `{"detail": "message"}` | No credentials, or the account was rejected (see below). |
| 401 | `{"detail": "...", "code": "token_not_valid", "messages": [...]}` | The token is malformed, expired or blacklisted. |
| 403 | `{"detail": "message"}` | Authenticated, but the role is not allowed. |
| 404 | `{"detail": "message"}` | The record does not exist **or belongs to someone else**. |
| 410 | `{"detail": "message"}` | A public share link that has expired, been revoked, or whose owner is no longer active. |
| 429 | `{"detail": "message"}` | Rate limit or job limit reached. |

Treat `detail` as either a string or a one-element list and show it as is; the
messages are written for end users.

## Authentication failures (401)

The same 401 status covers different causes; use the `detail` text or the
frontend's refresh flow, not the status alone.

| `detail` | Meaning | What to do |
|---|---|---|
| Authentication credentials were not provided. | No `Authorization` header. | Send the user to login. |
| Given token not valid for any token type (`code: token_not_valid`) | Expired, malformed or blacklisted. | Try `POST /api/auth/refresh/` once; if that fails, log in again. |
| This token has been revoked. | Password, email, role or suspension changed, or "log out everywhere". | Log in again. Refreshing will also fail. |
| Email verification is required. | The email was never verified, or was just changed. | Ask the user to verify (resend endpoint). |
| This account is suspended. | An administrator suspended the account. | Show the message; no retry helps. |

Login itself (`POST /api/auth/login/`) answers with **400**, not 401, for wrong
credentials and for unverified, suspended or inactive accounts. An unknown
account and a wrong password produce the identical message so accounts cannot be
enumerated.

## Rate limits (429)

Two independent mechanisms return 429:

- **Request throttling.** Per address and per endpoint group (login, upload,
  reports, and so on) plus overall anonymous and per-user ceilings. The response
  carries a `Retry-After` header in seconds. The rates are in
  `REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]`.
- **Login backoff.** After repeated wrong passwords the account is refused with
  `429` and `Retry-After` for a while, even with the right password. Show the
  message and offer "reset your password".
- **Job limits.** `POST /api/enhancements/images/{id}/` and
  `POST /api/enhancements/batches/{id}/` refuse new work while the user already
  has `MAX_ACTIVE_ENHANCEMENTS_PER_USER` single-image or
  `MAX_ACTIVE_BATCH_ENHANCEMENTS_PER_USER` batch jobs in progress. The message
  states the limit. Retry after some jobs finish; there is no `Retry-After`.

## 404 instead of 403 for other people's data

Owner-scoped endpoints (images, batches, results, reports, packages, identity
groups) look records up within the caller's own data. Someone else's record is
therefore indistinguishable from a missing one, so ids cannot be probed. Do not
build UI that expects a 403 for "not yours".

## Failed background work

A job that fails does not raise an HTTP error; it reports `status: "failed"` with
a `failure_reason` on its own resource. Jobs whose worker disappeared are failed
automatically after a timeout with the reason "The background worker stopped
responding, so this job was marked failed automatically. You can start it
again." See [Feature 20 job operations](features/20-background-job-operations.md).
