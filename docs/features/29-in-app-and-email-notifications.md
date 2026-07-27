# Feature 29: In-App and Email Notifications

## Status

**Complete backend baseline.** Frontend integration remains.

Feature 29 provides owner-scoped in-app notifications, configurable user
preferences, optional background email delivery, unread-state management, and
retention cleanup. The current processing workflows publish upload,
enhancement, quality-decision, batch, report, and download-package events.

Identity-group, share-link, moderator-decision, and live-session publishers are
added when their owning features are implemented; they must use the same
notification service rather than creating a second notification system.

## User capabilities

- View a paginated list of personal notifications.
- Filter notifications by unread state, event type, or severity.
- View the current unread count for the dashboard badge.
- Mark one notification or all notifications as read.
- Delete an owned notification.
- Enable or disable accepted-photo, rejected-photo, completed-batch,
  identity-group, email, and live-session preferences.
- Receive optional email messages for eligible processing results.

## Events published by current workflows

- Upload received.
- Background processing started.
- Enhancement completed or failed.
- Photo accepted, rejected, failed, or sent for review.
- Multiple-image or ZIP batch completed, partially completed, failed, or
  cancelled.
- Processing report ready or failed.
- Batch download package ready or failed.

Notifications include only safe identifiers and display metadata. They never
contain passwords, tokens, biometric embeddings, filesystem paths, private
report payloads, or internal exception details.

## API contract

All endpoints require a valid JWT access token.

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/notifications/` | List the authenticated user's notifications |
| `GET` | `/api/notifications/unread-count/` | Return the unread badge count |
| `GET` | `/api/notifications/{id}/` | Read one owned notification |
| `DELETE` | `/api/notifications/{id}/` | Delete one owned notification |
| `POST` | `/api/notifications/{id}/read/` | Mark one notification as read |
| `POST` | `/api/notifications/mark-all-read/` | Mark every unread notification as read |
| `GET` | `/api/notifications/preferences/` | Read notification preferences |
| `PATCH` | `/api/notifications/preferences/` | Update selected preferences |

The list endpoint supports:

- `unread=true` or `unread=false`
- `event_type=<supported-event-type>`
- `severity=info|success|warning|error`
- `page=<positive-integer>`
- `page_size=<1-100>`

Preference update example:

```json
{
  "photo_accepted": true,
  "photo_rejected": true,
  "batch_upload_finished": true,
  "person_group_created": true,
  "email_notifications": false,
  "live_session_alerts": false
}
```

## Delivery and idempotency

Every producer supplies a stable idempotency key, such as a batch identifier
plus its lifecycle event. A database uniqueness constraint prevents Celery
retries, repeated polling, or concurrent workers from creating duplicate
notifications for the same recipient and event.

Eligible email is queued only after the notification transaction commits. The
Celery task rechecks the global email switch, the user's preference, verified
email status, and active-account status before sending. Temporary delivery
errors use bounded exponential retries; permanent failure is stored as a safe
status without exposing mail-server details.

Email is intentionally disabled by each user's default preference. A user must
enable `email_notifications` before processing emails are sent.

## Security and privacy controls

- Every query is filtered by `request.user`.
- Cross-user detail and mutation requests return `404`.
- Preferences are one-to-one with the authenticated account.
- Account deletion cascades to preferences and notifications.
- API serializers expose safe notification fields only.
- Metadata is restricted to an explicit allowlist of display-safe scalar
  values.
- Email action links are constructed from the configured FaceCloud frontend
  origin and internal dashboard paths.
- Read and preference mutations use a dedicated API throttle.
- Notification emails require an active account and verified address.

## Retention and cleanup

Read notifications older than 90 days are removed by default. Unread
notifications are retained so unseen events are not silently deleted.

Manual cleanup command:

```powershell
python manage.py cleanup_expired_notifications
```

Celery also exposes `notifications.cleanup_expired` for Celery Beat or another
production scheduler.

## Configuration

```dotenv
NOTIFICATION_EMAILS_ENABLED=True
NOTIFICATION_RETENTION_DAYS=90
NOTIFICATION_EMAIL_MAX_RETRIES=3
```

The standard `FRONTEND_URL`, `DEFAULT_FROM_EMAIL`, and Django email backend
settings control email links and delivery.

## Verification

- Migration `notifications.0001_initial` creates preferences, notifications,
  owner indexes, the unread partial index, and the idempotency constraint.
- 12 focused tests cover preferences, filtering, unread actions, owner
  isolation, metadata safety, idempotency, email delivery, retention, account
  deletion, and report-ready events.
- Existing upload, enhancement, quality, report, and download suites protect
  the event integration points from processing regressions.
- The complete backend suite passes **131 tests** against PostgreSQL.
- `python manage.py check` and migration-drift checks are part of final
  verification.

## Frontend integration remaining

- Connect the header bell to `GET /api/notifications/unread-count/`.
- Add a paginated notification panel using `GET /api/notifications/`.
- Use `severity` and `is_read` to render accessible visual states.
- Use `action_path` for internal navigation; do not construct resource URLs
  from IDs on the client.
- Connect the Settings toggles to the preferences `GET` and `PATCH` endpoints.
- Add loading, empty, expired-token, failure, retry, and optimistic read states.
- Refresh the list after mark-read, mark-all-read, or delete actions.
