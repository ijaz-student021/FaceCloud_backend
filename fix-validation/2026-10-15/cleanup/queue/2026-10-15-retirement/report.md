# Queue integration `2026-10-15-retirement`

Real Redis, real Celery workers (web environment, default queue), a throwaway PostgreSQL database and the real pipeline. Visibility timeout 300 s, task time limit 200 s and claim lease 60 s for this run.

| Scenario | Expected | Result |
|---|---|---|
| routing_and_full_flow | preprocessing and enhancement run on the worker; the result is the lightweight pipeline's, with no retired stage; file stored once; download returns the stored image; progress reports completion | **PASS** |
| duplicate_delivery | two deliveries of one task; the image is enhanced and published once | **PASS** |
| database_failure_then_retry | the worker's database connection is killed mid-task; the task retries, resumes its own claim and completes once | **FAIL** |
| cancellation_mid_batch | cancel while the first image is processing: the job ends cancelled, unfinished images are cancelled and get no file | **PASS** |
| worker_killed_then_restarted | the task is redelivered after the visibility timeout, waits out the dead worker's claim, takes over and completes once | **FAIL** |
| duplicate_while_original_renews | the original's claim is renewed through a stage longer than the lease; the duplicate waits a bounded number of times, never takes over, and leaves the result to the original, which publishes once | **PASS** |
| old_worker_fails_after_takeover | an old worker whose claim lapsed fails after another execution took the result over; the failure does not touch the new owner's row, which completes and is published once | **PASS** |
| worker_killed_mid_batch | the batch is redelivered while the dead worker's claim is live; it comes back for that image once the claim lapses instead of skipping it; every image completes once and the job counts are right | **PASS** |
