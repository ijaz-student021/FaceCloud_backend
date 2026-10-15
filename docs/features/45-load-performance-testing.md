# Feature 45: Load and performance testing

## Status

A Locust harness and a reusable seed command, plus one real baseline run
against a local server. This is infrastructure the project did not have
before -- a way to generate realistic, authenticated, concurrent load against
the actual API and get real numbers -- not a one-time report. The 598 unit/
integration tests, system checks, and manual verification that made up the
rest of Feature 45 were already in place; see `docs/FEATURES.md`.

## What it adds

- **`operations/management/commands/seed_loadtest_users.py`** -- creates
  deterministic, pre-verified accounts (`loadtest-user-1..N` plus one
  `loadtest-admin`) sharing one known password, each with a personal
  organization. Refuses to run unless `settings.DEBUG` is on, since every
  account's password is public in this file -- it must never be pointed at a
  real deployment. Idempotent (`get_or_create`), so it is safe to re-run.
- **`requirements-loadtest.txt`** and **`loadtest/locustfile.py`** -- Locust
  pulls in gevent and its own Flask-based web UI, unrelated to the Django
  app's runtime, so it gets its own isolated environment, the same way
  `requirements-gfpgan.txt` and `requirements-training.txt` do for the GPU
  worker:

  ```bash
  python -m venv .runtime/venvs/loadtest
  .runtime/venvs/loadtest/Scripts/pip install -r requirements-loadtest.txt
  venv/Scripts/python manage.py seed_loadtest_users --count 20   # main venv, DEBUG only
  .runtime/venvs/loadtest/Scripts/locust -f loadtest/locustfile.py --host http://127.0.0.1:8000
  ```

  Two simulated user classes, weighted to resemble real traffic
  (`RegularUser` 10x more common than `AdminUser`):
  - `RegularUser`: lists upload batches, lists "my images", checks a batch's
    progress, and occasionally uploads a real (in-memory-generated) small
    JPEG through the actual validation path.
  - `AdminUser`: hits system health, platform analytics, the new moderation
    overview (Feature 33), the audit feed, and queue status.

  Each simulated user logs in once in `on_start` and reuses that access
  token for the rest of its session, matching how a real client behaves --
  it does not re-login on every request.

## Baseline run

60 seconds, 15 concurrent users (14 `RegularUser` + 1 `AdminUser`, ramped at
3/sec), against `manage.py runserver` locally with Redis running and
Celery's result backend reachable (dispatch -- not a worker -- is enough for
the upload endpoint to return, since processing happens as part of request
handling here, not in a background worker for this payload size):

| Endpoint | Requests | Failures | Median | 95th %ile | Max |
|---|---|---|---|---|---|
| `POST /api/auth/login/` | 15 | 0 | 12.0s | 13.0s | 13.2s |
| `GET /api/images/batches/` | 98 | 0 | 42ms | 170ms | 405ms |
| `GET /api/images/batches/{id}/progress/` | 27 | 0 | 160ms | 500ms | 516ms |
| `GET /api/images/my/` | 105 | 0 | 110ms | 320ms | 2.2s |
| `POST /api/images/upload/single/` | 25 | 0 | 2.2s | 4.7s | 7.8s |
| `GET /api/system/analytics/` | 3 | 0 | 110ms | 220ms | 220ms |
| `GET /api/system/moderation/` | 2 | 0 | 220ms | 220ms | 220ms |
| `GET /api/system/health/` | 4 | 0 | 1.5s | 11.0s | 11.2s |
| **Total** | **280** | **0 (0.00%)** | **110ms** | **9.4s** | **13.2s** |

Zero failed requests at this concurrency. Full Locust CSV output was kept
locally (not committed -- it's a point-in-time artifact, not source).

### Reading these numbers honestly

Two things skew the absolute latencies here and should not be read as
production capacity numbers:

1. **`manage.py runserver` is single-process.** Password hashing on login
   (deliberately slow, by design, to resist brute force) and other CPU-bound
   work serialize behind each other when several requests land at once,
   which is why `login` and `health` (health checks the DB, Redis, and
   Celery) show multi-second tails here. A production deployment runs
   multiple `gunicorn` workers (already a pinned dependency in
   `requirements.txt`) specifically so this doesn't happen. **Gunicorn
   itself cannot run on Windows** (it imports `fcntl`, Unix-only), so a
   multi-worker baseline could not be captured on this development machine;
   re-running this same harness against a `gunicorn -w 4` deployment (Linux,
   or the eventual GPU/production host) is the right way to get numbers that
   mean something for capacity planning.
2. **The login endpoint is intentionally throttled to 20/hour per source
   IP** (`"login": "20/hour"` in `DEFAULT_THROTTLE_RATES`, anti-credential-
   stuffing). A single-machine load test shares one source IP across every
   simulated user, so this caps how many *new* logins a run can include --
   it does not limit already-authenticated traffic, since each simulated
   user logs in once and reuses its token. Keep concurrent users under ~20
   per hour-long run, or raise the scope's rate specifically for a load-test
   environment, never relax it for production.

### Read-path latency (the numbers that do carry over)

The list/detail endpoints -- not serialized behind password hashing --
stayed fast and failure-free even on a single dev process: batch listing
and the moderation overview both under 220ms median, upload (the one
CPU/IO-heavy write path exercised here) at 2.2s median including real image
validation. These are the numbers worth tracking run-over-run as the
platform grows.

## Deliberately not built

- **Sustained/soak testing and a CI-gated performance budget.** This is a
  harness and one baseline, not a continuous benchmark; wiring a load test
  into CI would need a dedicated throwaway environment (a shared CI runner
  can't host 15+ concurrent simulated users against a real DB without
  flaking unrelated jobs) and is future work, not this pass.
- **Live-camera and WebSocket load testing.** No live-camera feature exists
  yet (Features 36-40 are Backlog), so there is nothing to load test there.
- **GPU-pipeline load testing** (enhancement, face validation, duplicate
  detection under concurrency). These need the rented GPU session to be
  meaningful at all -- CPU-only enhancement is already known to be far
  slower than real-time, so load numbers from this machine would not
  reflect the production path. Tracked as part of the GPU-day plan.
