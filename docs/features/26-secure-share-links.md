# Feature 26: Secure share links

## Status

Complete for the backend (`sharing` app). The frontend needs a "Share" dialog, a
list of the user's links, and optionally a public viewer page.

## What it does

An owner turns one of their completed results (or one of its edits) into a link
that anyone can open **without an account**. The link expires, can be revoked at
any moment, can carry a watermark, and records how often it was viewed.

## Security design

- **Unguessable token.** 256 bits from `secrets.token_urlsafe(32)`. Knowing that
  a link exists tells an attacker nothing, so the API distinguishes 404 (never
  existed) from 410 (expired, revoked, or owner no longer active).
- **Only the hash is stored.** The database holds the SHA-256 of the token. The
  raw token is returned once, in the creation response, and cannot be shown
  again; a database leak or admin-panel access does not yield working links (the
  admin hides the hash field as well). Logs and the activity timeline never
  contain the token.
- **No identity for viewers.** Access tracking stores the time and a truncated
  user agent, deliberately **not** an IP address. This is a facial-image product,
  so recording who looked at someone's photo would be a privacy liability.
- **The owner stays in control.** Revocation takes effect on the next request.
  Links also stop working if the owner is suspended or deactivated, and they are
  deleted with the result, edit or account they point to.
- **Public but hardened.** The public endpoints ignore any `Authorization`
  header (a stale login token cannot break a shared link), are throttled per
  address (`share_public`, 120/hour), and respond with `Cache-Control: private,
  no-store`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`
  and `X-Robots-Tag: noindex`.
- **Bounded.** Lifetime is capped (`SHARE_LINK_MAX_TTL_HOURS`, default 30 days)
  and each user may keep `SHARE_LINK_MAX_ACTIVE_PER_USER` active links (default
  50), returning 429 beyond that.

"View-only" means the recipient gets no account access and cannot change or
delete anything. The browser can still save an image it displays; use a
watermark if the copy should be identifiable.

## Endpoints

Owner (login required):

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/shares/` | Create a link. Returns the token and `public_url` once. |
| GET | `/api/shares/` | List your links (state, expiry, view count; never the token). |
| GET | `/api/shares/{id}/` | One link, with its 20 most recent views. |
| POST | `/api/shares/{id}/revoke/` | Revoke now (idempotent). |

Create request:

```json
{
  "result": 42,
  "edit": null,
  "ttl_hours": 24,
  "label": "For Sara",
  "watermark": "username",
  "watermark_position": "bottom_right",
  "watermark_opacity": 0.5
}
```

Only `result` is required. `edit` shares a completed edit of that result instead
of the enhanced file. The `watermark*` fields are the same as in
[Feature 25](25-watermark-options.md), and the marked copy is generated per view
while the stored file stays clean.

Public (no login):

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/shares/public/{token}/` | The image, inline. Counts as a view. |
| GET | `/api/shares/public/{token}/info/` | JSON: filename, content type, size, expiry, whether watermarked. Does not count as a view and never names the owner. |

`state` is `active`, `expired` or `revoked`.

## Housekeeping

`sharing.cleanup_expired` runs daily at 04:00 from the beat schedule and deletes
links that ended more than `SHARE_LINK_RETENTION_DAYS` (default 30) ago, together
with their view records. Settings are listed in `.env.example`.

## Things to know when deploying

- The token is part of the URL path, so **web-server access logs will contain
  it**. Treat those logs as sensitive, or strip the path segment after
  `/api/shares/public/` when shipping them.
- Set `PUBLIC_BACKEND_URL` correctly; it builds the `public_url` shown to the
  owner.
- A frontend page at your own route can call the `info` endpoint for the
  metadata and use the `public_url` as the `<img>` source.

## Verification

`sharing/tests.py` (27 tests): token returned once and only the hash stored,
never re-shown in list, detail or activity events; default and maximum lifetime;
ownership and completion rules for results and edits; watermark validation and
rendering with the stored file untouched; login required and private lists;
per-user cap and slot freed by revoking; idempotent revoke; anonymous access,
secure headers, stale-login-header tolerance; 404 and 410 cases including
suspended and deactivated owners and cascaded deletion; view counting without
storing viewer identity; edit links; the info endpoint; and cleanup that keeps
active and recently ended links.
