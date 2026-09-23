# Feature 34 - System Health API

## Purpose

Feature 34 gives administrators one safe operational view of FaceCloud's runtime
dependencies. It reports readiness without exposing credentials, absolute paths,
model contents, or raw exception messages.

## Implementation plan

1. Add a migration-free `operations` Django application.
2. Expose an administrator-only `GET /api/system/health/` endpoint.
3. Check the API process, database, Redis broker, Celery workers, media storage,
   email backend, and configured AI model files independently.
4. Include per-check latency, aggregate response latency, application version,
   available storage, and pending/active/failed processing-job counts.
5. Map check results to `healthy`, `degraded`, or `unavailable` and return HTTP
   200 for an operational/degraded service or HTTP 503 when a required dependency
   is unavailable.
6. Add authorization, aggregation, failure-sanitization, and response-contract
   tests.

## API contract

`GET /api/system/health/`

Authentication: JWT access token belonging to an administrator or Django
superuser.

Example response shape:

```json
{
  "status": "healthy",
  "version": "dev",
  "checked_at": "2026-09-17T08:00:00Z",
  "response_time_ms": 14.27,
  "checks": {
    "api": {"status": "healthy", "latency_ms": 0.01},
    "database": {"status": "healthy", "latency_ms": 1.2},
    "redis": {"status": "healthy", "latency_ms": 2.1},
    "celery": {"status": "healthy", "latency_ms": 4.5, "workers": 1},
    "storage": {"status": "healthy", "latency_ms": 0.7, "available_bytes": 1000000},
    "email": {"status": "healthy", "latency_ms": 0.5},
    "models": {"status": "healthy", "latency_ms": 0.8, "models": {}}
  },
  "jobs": {"pending": 0, "active": 0, "failed": 0}
}
```

Individual checks may contain a stable `code`, such as `connection_failed` or
`not_configured`. They never contain the caught exception text.

## Health policy

- `unavailable`: a required dependency (database, Redis, Celery, or storage) is
  unavailable. The endpoint returns HTTP 503.
- `degraded`: optional email/model configuration is unavailable, no Celery worker
  replies, or another non-fatal readiness issue exists. The endpoint returns HTTP
  200 so monitoring can distinguish degradation from total unavailability.
- `healthy`: all enabled checks pass.

Model checks validate file presence and, when supplied, SHA-256 checksums. They do
not load large neural networks on each health request.

## Acceptance criteria

- Anonymous requests return 401.
- authenticated non-admin users return 403.
- administrators receive the documented response without secrets or raw errors.
- dependency failures are isolated and reflected in the aggregate status.
- required dependency failure returns HTTP 503.
- automated tests pass and no migration is introduced.

