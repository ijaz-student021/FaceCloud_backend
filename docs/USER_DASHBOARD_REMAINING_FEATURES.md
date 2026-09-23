# FaceCloud User Dashboard: Remaining Features

## Purpose

This document is the implementation checklist for the normal-user portal. It is
derived from the authoritative `docs/FEATURES.md` catalogue and the current user
dashboard screens. Administrator-only and moderator-only work is excluded.

The portal currently has working foundations for authentication, uploads,
preprocessing, enhancement, quality decisions, face validation, duplicate
detection, batch progress, dashboard statistics, My Images listing, and protected
individual downloads. The backend also provides a non-destructive Auto/Manual
gallery editor; the frontend still needs to connect its sliders and polling UI.

## Summary

Fourteen catalogue features still contain work required by the user dashboard:

- 11 planned user features
- 3 partially complete user features
- 7 practical implementation workstreams

The affected catalogue numbers are Features 18–20, 24–26, 28, 30, and 36–41.

## 1. Identity and Person Folders

### Feature 18: Face recognition and identity grouping — Complete backend; deployment gate remains

Automatically organize accepted images into real person folders.

Remaining work:

- Integrate a commercially permitted FaceNet, ArcFace, or private equivalent model.
- Extract embeddings only from validated face regions.
- Compare embeddings using calibrated similarity thresholds.
- Match a face to an existing identity or create a new identity group.
- Support multiple valid people in one image without rejecting the image.
- Route uncertain matches to review instead of forcing a group.
- Associate original, standardized, and enhanced images with the group.
- Enforce owner-only visibility for all identities and embeddings.

### Feature 19: Identity group management — Complete backend

Allow a user to correct and organize automatically generated person folders.

Remaining work:

- List and open real identity groups.
- Rename a person folder.
- Merge two groups belonging to the same user.
- Move an image to another group.
- Remove an incorrectly grouped image.
- Set a group profile image.
- Delete an invalid or empty group safely.
- Store confidence values and group-change history.

## 2. Reliable Background Processing

### Feature 20: Background batch processing — Partial

Preprocessing, enhancement, quality processing, and duplicate analysis already use
Celery foundations. Production hardening remains.

Remaining work:

- Add identity-recognition tasks to Celery.
- Configure bounded retries and task timeouts.
- Add controlled CPU/GPU concurrency.
- Isolate failures so one image cannot fail an entire batch.
- Complete cooperative cancellation across every pipeline stage.
- Prevent stale and duplicate tasks from overwriting newer results.
- Add production queue monitoring and recovery procedures.

## 3. Reports and Downloads

### Feature 22: Processing reports — Complete backend

Owner-scoped image and batch reports are implemented in JSON, CSV, and PDF.
Generation runs through Celery and includes normalized quality, face, duplicate,
enhancement, duration, reason, and protected-link fields. The Downloads page only
needs to connect to these APIs and poll queued jobs.

Frontend integration work:

- Add JSON, CSV, and PDF selection to the Downloads page.
- Poll queued/generating report state and enable downloads when ready.
- Show safe report failures and a regenerate action.

### Feature 23: Image and batch downloads — Complete backend

Protected original, standardized, enhanced, and report downloads now create
owner-only history entries. Accepted-results and complete-batch ZIP packages are
generated asynchronously, expire safely, and can be regenerated.

Frontend integration work:

- Add accepted-results and complete-batch package actions.
- Add JSON, CSV, and PDF report-format selection.
- Poll queued/generating packages and enable ready downloads.
- Display download history, expiry, failures, regeneration, and deletion.

### Feature 24: Identity-group ZIP export — Planned

Allow a user to download all authorized images for one person.

Remaining work:

- Generate an `original/` directory.
- Generate an `enhanced/` directory.
- Include `report.csv` and group metadata.
- Use safe archive paths and bounded archive generation.
- Add an owner-scoped download and audit event.

### Feature 25: Watermark options — Planned

Allow optional branding on exported enhanced copies.

Remaining work:

- Support no watermark, small watermark, username, FaceCloud, and custom text.
- Support validated position and opacity controls.
- Provide an export preview.
- Preserve the master enhanced image without a permanent watermark.

### Feature 26: Secure share links — Planned

Allow external view-only access without exposing the user's account.

Remaining work:

- Generate cryptographically secure share tokens.
- Support configurable expiration, including a 24-hour default.
- Provide view-only access to one authorized result.
- Allow immediate revocation.
- Enforce automatic expiry and access tracking.
- Prevent guessing, listing, or extending another user's links.

## 4. Activity and Notifications

### Feature 28: Complete user activity timeline — Partial

Authentication events and limited dashboard activity exist. The complete content
timeline remains.

Remaining work:

- Record upload and batch events.
- Record preprocessing, enhancement, validation, and quality decisions.
- Record duplicate and identity-group actions.
- Record report, download, export, and share-link events.
- Return a paginated owner-only timeline with filters.

### Feature 29: In-app and email notifications — Backend complete

The backend now persists owner-scoped notifications and preferences, publishes
events from upload, enhancement, quality, batch, report, and download workflows,
and optionally sends eligible emails through Celery. It provides paginated list,
filter, unread-count, mark-read, mark-all-read, delete, and preference APIs.

Frontend integration remaining:

- Connect the dashboard bell, notification panel, unread actions, and Settings
  toggles to `/api/notifications/`.
- Add identity-group, share-expiration, moderator-decision, and live-session
  producers when those owning backend features are implemented.

## 5. User Dashboard Integration

### Feature 30: User dashboard — Backend complete; integration remains

Dashboard statistics and My Images APIs exist, while several visible screens still
contain demonstration values or frontend-only behavior.

Remaining work:

- Connect Overview to `GET /api/user/dashboard/`.
- Connect My Images to `GET /api/images/my/` with pagination and filters.
- Display real previews, statuses, quality reasons, and duplicate warnings.
- Connect before/after comparison results.
- Connect batch progress polling, retry, and cancellation.
- Connect real Person Folders, Downloads, activity, and notifications when ready.
- Remove hard-coded totals, filenames, groups, charts, and history.
- Provide loading, empty, expired-token, failure, and retry states.
- Correct frontend upload limits and accepted formats to match backend policy.
- Extend the top search beyond filename-only search when identities and reports exist.

## 6. Live Camera Enhancement

### Feature 36: Live camera face detection — Planned backend

The frontend camera demonstration exists, but a complete production workflow remains.

Remaining work:

- Request explicit camera permission and support camera selection.
- Return live face boxes, count, size, position, and confidence.
- Provide no-face, excessive-face, low-light, and blur guidance.
- Allow capture only after a valid user decision; group images remain supported.

### Feature 37: Real-time camera enhancement — Planned backend

Remaining work:

- Provide controlled original and enhanced previews.
- Support low, medium, high, and automatic enhancement levels.
- Run a commercially permitted model or approved OpenCV fallback.
- Bound frame rate, resolution, latency, and resource use.
- Save only a user-confirmed enhanced snapshot.

### Feature 38: Live enhancement comparison — Planned backend integration

Remaining work:

- Compare original, OpenCV, and AI results side by side.
- Return live brightness, blur, sharpness, face-confidence, FPS, and latency data.
- Recommend a result without preventing the user from choosing another.
- Generate an optional captured-frame comparison report.

### Feature 39: Live processing communication — Planned

Remaining work:

- Implement an authenticated WebSocket or rate-controlled frame API.
- Resize and compress incoming frames safely.
- Apply backpressure and discard outdated frames.
- Implement session start, timeout, heartbeat, and stop operations.
- Return detection coordinates, processing state, and enhanced results.

### Feature 40: Live camera privacy and security — Planned

Remaining work:

- Make the active-camera state explicit.
- Disable recording and persistence by default.
- Store only user-confirmed snapshots.
- Clean temporary frames immediately.
- Terminate the server session when the user stops or closes the camera.
- Require HTTPS in production and record consent without storing video.

## 7. Advanced Facial-Data Privacy

### Feature 41: Privacy and facial-data security — Partial

JWT authentication, ownership filters, secure filenames, protected downloads, and
cross-user duplicate isolation exist. Advanced controls remain.

Remaining work:

- Add facial-image and embedding retention rules.
- Add user-visible deletion and retention controls for generated artifacts.
- Encrypt sensitive biometric templates according to deployment policy.
- Add consent records for identity and live-camera processing.
- Add export and erasure workflows for personal facial data.
- Complete security tests for all new identity, sharing, and camera endpoints.

## Recommended implementation order

1. Feature 18 — face recognition and identity grouping
2. Feature 19 — identity group management
3. Feature 20 — background-recognition hardening
4. Feature 24 — identity-group downloads
5. Feature 28 — complete activity timeline
6. Feature 30 — replace all remaining demonstration data
7. Feature 26 — secure share links
8. Feature 25 — watermark exports
9. Features 36–40 — production live-camera workflow
10. Feature 41 — final privacy, retention, consent, and erasure hardening

## Next feature

The next implementation should be **Feature 18: Face recognition and identity
grouping**. It unlocks the Person Folders page, identity management, group ZIP
exports, identity report fields, and several moderator workflows.
