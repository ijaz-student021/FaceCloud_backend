# Admin console alignment audit

The frontend (`ali4347/facecloud-ai-platform`, read-only) is the reference; the backend adapts. One row per admin screen: the endpoint that feeds it,
what the screen reads, and whether the backend now provides it. Audited against backend `main` at `10dbdbc` (after #85). Everything added is
additive: no field or endpoint is renamed or removed (two existing tests that pinned the exact key set of an account row were updated for the two
new keys).

Status: `matches` (the data exists and the shapes line up), `mapping` (the data exists; the screen builds its own words or colours from it),
`not available` (nothing in the backend produces it, and it is not faked).

| # | Screen | Endpoint | What the screen reads | Status |
|---|---|---|---|---|
| 1 | Users | `GET/POST /api/user/admin/users/`, `PATCH .../{id}/` | name, email, role, suspended, joined, **uploads** | matches: `uploads_count`, `last_login`, `?q=`, `?role=`, `?status=` added; paginated with `?page=` |
| 2 | Moderators | same | same, role = moderator | matches |
| 3 | Admin Overview | `GET /api/system/analytics/` | totals, upload trend, rejection reasons, storage, live sessions, success and rejection rate | matches: `overview.*` added (see below); totals and trend were already there |
| 4 | Statistics | same | uploads / enhanced / rejected per day, weekly success vs failed, live sessions per day, storage growth by month | matches: `overview.rejections.per_day`, `enhancement_success_per_week`, `live_sessions.per_day`, `storage.growth_by_month` |
| 5 | Upload Statistics | same | total, today, average size, single / multiple / ZIP counts, per-day split | matches: `overview.uploads` |
| 6 | Rejection Summary, Rejections | same | total, rate, reasons with counts, per day | matches: `overview.rejections` and `top_rejection_reasons` (codes; the screen words them with `reasons.ts`) |
| 7 | Audit Logs | `GET /api/system/audit/` | actor, **role**, action sentence, resource, **ip**, status, time | matches: `actor`, `actor_name`, `actor_role`, `type`, `status`, `message`, `resource`, `ip_address` added; `ip_address` is recorded for sign-ins only |
| 8 | Security | `GET /api/system/security/alerts/` (+ `acknowledge`, `rules`) | type, message, ip, time, risk | matches: `type`, `risk`, `message`, `ip_address` added. Two risk words only (`medium`, `high`): the backend has no `low` or `critical` |
| 9 | System Health, System Monitoring | `GET /api/system/health/`, `/queue/` | service name, state, latency, last checked, queue size | matches: `services[]` and `process` added. **Not available:** uptime %, CPU %, memory per service (nothing measures them) |
| 10 | Image Monitoring | `GET /api/system/images/`, `/{id}/`, `/{id}/preview/` | filename, status, size, dimensions, sharpness, brightness, thumbnail | mapping: `original_name`, `file_size`, `width` x `height`, `quality.sharpness_score` / `brightness_score`, and the preview URL |
| 11 | Login (admin) | `POST /api/auth/login/` | role gate | matches |

## Not in the backend, and not faked

* Service **uptime percentage, CPU and memory**. These need a monitoring system (for example Prometheus node metrics); the health endpoint reports only what
  it can check (reachable, latency, queue size, free disk).
* **Risk `low` / `critical`** on security alerts. The rules produce `warning` and `high` only.
* **IP addresses on activity events.** Only sign-in events record one.
* **Percent change** has no value (`null`) until there is a previous period to compare with.

## Closing the gaps

| Item | Rows | What was added |
|---|---|---|
| 1 | 3-6 | `overview` on `GET /api/system/analytics/` (upload types, recent counts, rejections per day and rate, weekly enhancement success, live sessions, storage split and growth, change vs the previous period); the daily series no longer splits a day on a distinct queryset |
| 2 | 7 | Audit rows: actor name and role, address, type, status, sentence, resource |
| 3 | 8 | Security alerts: type, risk, sentence, address |
| 4 | 1, 2 | Account list: `uploads_count` (one query for the page), `last_login`, search and filters |
| 5 | 9 | Health: `services[]` rows and `process` |

The moderator-role promotion gap noted in the frontend's work log was already closed in `57f5510` (a moderator may only set the plain member role;
tests `test_a_moderator_cannot_promote_a_member_to_moderator` and `..._grant_the_owner_role`).
