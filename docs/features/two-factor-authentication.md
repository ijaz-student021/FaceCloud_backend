# Two-factor authentication (TOTP)

Optional, per-account. Adds a second step to login: a 6-digit code from an
authenticator app (Google Authenticator, Authy, 1Password, ...), on top of the
password. Not tied to any other feature's numbering; part of Feature 01
(`docs/FEATURES.md`).

## Why TOTP, and why hand-written

TOTP (RFC 6238, the algorithm every authenticator app already implements) rather
than SMS codes, which need an SMS provider this project does not have and are
weaker against SIM-swap attacks. Implemented directly with the standard library's
`hmac`/`hashlib` (`accounts/totp.py`) rather than adding a dependency for a short,
precisely specified algorithm — verified against RFC 6238 Appendix B's own
published test vector (`accounts/test_two_factor.py`'s
`test_matches_the_published_rfc_6238_test_vector`).

## The secret's encryption

A user's TOTP secret is encrypted at rest (`accounts/two_factor_crypto.py`, Fernet
from the already-a-dependency `cryptography` package), keyed by its own
`TWO_FACTOR_SECRET_KEY` — never `DJANGO_SECRET_KEY` or the identity-embedding/image
keys, so rotating one can never lock or expose another. This is the same rule
`config/production_guard.py` already enforces for `IDENTITY_EMBEDDING_KEY` and
`IMAGE_ENCRYPTION_KEY`, extended to this new secret (`secrets_rule`, tested in
`config/test_production_guard.py`). Unlike those two keys, this one has no
rotation ladder (`_KEY_ID`/`_PREVIOUS_KEYS`): if it is ever rotated, every
enrolled user simply re-enrolls (cheap — a TOTP secret carries no history worth
preserving, unlike a face embedding), so the extra machinery was not built.

## The API

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /api/auth/login/` | — | Unchanged for password-only accounts. For a 2FA account, returns `{two_factor_required: true, login_token}` instead of tokens. |
| `POST /api/auth/2fa/verify/` | — (needs `login_token`) | `{login_token, code}` → the real `{access, refresh, user, role, dashboard}`, same shape a normal login returns. |
| `POST /api/auth/2fa/setup/` | session | Generates a new secret, **not yet enforced** until confirmed. Returns `{secret, otpauth_uri}` (render the URI as a QR code; `secret` is for manual entry). Refused while already enabled. |
| `POST /api/auth/2fa/confirm/` | session | `{code}` → proves one real code, turns 2FA on, returns `{recovery_codes: [...]}` (shown once). |
| `GET /api/auth/2fa/status/` | session | `{enabled}`. |
| `POST /api/auth/2fa/disable/` | session | `{code}` (a device code or a recovery code) → 204. Refused without a valid code — a signed-in session alone can never turn this off. |
| `POST /api/auth/2fa/recovery-codes/` | session | `{code}` → a fresh `{recovery_codes: [...]}`; the old ones stop working immediately. |

## Security properties (each has a test)

- **The secret is never returned again after setup.** `TwoFactorSetupResponseSerializer`
  is the only response that ever carries it.
- **A stolen access token cannot turn 2FA off.** Every disable/regenerate call
  requires a live device code or a recovery code, checked by
  `accounts/services.py::_verify_second_factor`.
- **Replay protection.** `TwoFactorCredential.last_used_counter` is a watermark:
  the same code (or an older one) cannot be reused, exactly like a real device's
  currently-displayed code being spent once it is used.
- **Brute-force protection.** Both the disable/regenerate path and the login-verify
  path reuse `accounts/lockout.py` (the same per-account backoff that already
  protects password login), keyed per user — by design, a burst of wrong 2FA
  codes and a burst of wrong passwords share one lockout budget for that account,
  not two separate counters.
- **The login challenge token is short-lived and re-checked.** `login_token`
  (`django.core.signing.TimestampSigner`, `TWO_FACTOR_LOGIN_CHALLENGE_MINUTES`,
  default 5) proves only "the password was correct a moment ago" — it is not an
  access token. `verify_two_factor_login` re-checks `is_active`,
  `is_email_verified`, `is_suspended` and that 2FA is still enabled, rather than
  trusting the state from the password step, since the account can change in
  between.
- **Recovery codes are hashed like passwords** (`TwoFactorRecoveryCode.code_hash`,
  Django's own hasher) and each is usable once.

## A real bug this caught before it shipped

The first draft of `disable_two_factor`, `regenerate_recovery_codes` and
`verify_two_factor_login` wrapped the *entire* function in `@transaction.atomic`.
On a wrong code, `lockout.record_failure()` wrote a failure count and then the
function raised `ValidationError` — and because the write and the raise were in
the same atomic block, the whole transaction rolled back, silently discarding the
failure count. The lockout tests initially failed with "5 wrong codes were
recorded but the account is not locked," which traced to this. The fix: only the
success-path mutations (clearing the lock, deleting the credential, saving the
new counter) are wrapped in `transaction.atomic()`; `lockout.record_failure()` and
the subsequent `raise` happen outside it, as a small, already-committed write —
confirmed by re-running the test suite against the unfixed code first (6 of 47
tests failed) and against the fix (47/47 pass).

## Settings

`TWO_FACTOR_SECRET_KEY` (32+ chars in production, never reused elsewhere),
`TWO_FACTOR_ISSUER` (default `FaceCloud`, shown in the authenticator app),
`TWO_FACTOR_RECOVERY_CODE_COUNT` (default 10), `TWO_FACTOR_LOGIN_CHALLENGE_MINUTES`
(default 5). Throttle scope `two_factor` (20/hour) on setup/confirm/disable/
regenerate; the verify step reuses the existing `login` scope.

## Not built

- **No SMS/email fallback code.** Only TOTP and recovery codes.
- **No admin-forced 2FA policy** (an organization or platform requiring it for
  every member) — this is per-account opt-in only.
- **No "remember this device" cookie** to skip the second step on trusted
  browsers — every login asks for a code once 2FA is on.
- **Frontend.** No frontend repo exists yet; see `docs/FRONTEND_API_GUIDE.md`.
