# Feature 28: Complete User Activity Timeline

## Status

Complete backend. The dashboard frontend can consume the owner-scoped endpoint
below to render a single, chronological "everything that happened to my data"
feed.

## Design

A new standalone `activity` app holds the `ActivityEvent` model. It has no
dependency on any other app, so any app's services or Celery tasks can call
`activity.services.record_activity_event(...)` without introducing a circular
import. Categories: `upload`, `processing`, `moderation`, `download`, `group`,
`share`, `admin`.

The pre-existing `accounts.AuthEvent` trail (registration, login, password,
role, suspension events) is not duplicated into `ActivityEvent`. Instead, the
timeline endpoint merges both tables in Python, sorted by `created_at`, so the
account audit trail and the content activity trail read as one feed.

## Recorded events

- **Upload** — `upload_received` when a single, multiple, or ZIP batch is created
- **Processing** — `processing_started`, `batch_completed`/`batch_partial`/
  `batch_failed`/`batch_cancelled`, `quality_{decision}`,
  `enhancement_{status}`
- **Download** — `report_{status}`, `package_{status}`,
  `identity_export_{status}`
- **Group** — `group_renamed`, `group_merged`, `group_moved`, `group_removed`
  (emitted from the same `identities.management._record` call that already
  writes the immutable `IdentityChange` history, so every group action is
  covered with a single hook)
- **Auth** (merged from `AuthEvent`) — registration, login, logout, password
  change/reset, role change, suspension/reactivation, account deletion

Moderation and share-link categories are reserved for Features 26 and 27; no
events are emitted yet because those workflows do not exist.

## API contract

- `GET /api/user/activity/` — owner-scoped, merged timeline.
  - `category` (optional): one of the categories above, or `auth`.
  - `page`, `page_size` (optional, default 20, max 100).
  - Response: `{"page", "page_size", "has_next", "results": [...]}` where each
    result has `source` (`activity` or `auth`), `category`, `event`,
    `object_type`, `object_id`, `identifier`, `ip_address`, `details`,
    `created_at`.

This replaces the earlier auth-only `AuthEventListView` at the same URL and
name (`user-activity`); no frontend integration existed yet, so there is no
breaking change to ship around.

## Known simplification

The merge is a Python k-way merge over two sorted querysets, not a SQL
`UNION`, so pagination fetches up to `page * page_size + 1` rows from each
source (capped at 500). This is correct and efficient at this project's event
volume; very deep pagination (many thousands of combined events) would need a
database-level union if that ever becomes a real workload.
