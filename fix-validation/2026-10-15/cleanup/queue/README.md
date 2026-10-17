# Queue integration under the default lightweight policy, 2026-10-15

Real Redis, real Celery workers (web environment, default queue), a throwaway PostgreSQL
database, the real OpenCV/Pillow pipeline (`scripts/check_queue_integration.py`).

* `2026-10-15-retirement/`: all eight scenarios. Six passed. **Two failed for a reason in the
  harness, not the application:** `database_failure_then_retry` and
  `worker_killed_then_restarted` kill a worker "mid-task", and the lightweight pipeline now
  finishes in about 0.2 s, so the task had already completed (`connections_terminated: 0`,
  `status_when_killed: completed`). The learned models used to keep it running for tens of seconds.
* `2026-10-15-retirement-b/`: the same two scenarios after the harness holds the worker inside
  the pipeline with its existing fault injector (`sleep:N` after the claim). Both pass
  (database connection killed mid-task: retried and completed once, 51 s; worker SIGKILLed
  mid-task: redelivered after the 300 s visibility timeout, took over and completed once, 302 s).
