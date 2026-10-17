# Queue integration `2026-10-15-retirement-b`

Real Redis, real Celery workers (web environment, default queue), a throwaway PostgreSQL database and the real pipeline. Visibility timeout 300 s, task time limit 200 s and claim lease 60 s for this run.

| Scenario | Expected | Result |
|---|---|---|
| database_failure_then_retry | the worker's database connection is killed mid-task; the task retries, resumes its own claim and completes once | **PASS** |
| worker_killed_then_restarted | the task is redelivered after the visibility timeout, waits out the dead worker's claim, takes over and completes once | **PASS** |
