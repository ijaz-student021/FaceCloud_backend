# Advisor queue integration

Provider: scripted local server (no OpenAI). Workers: 3 single-process Celery workers; concurrency limit 2.

| Scenario | Expectation | Result |
|---|---|---|
| concurrency_limit_across_processes | 6 jobs on 3 workers; at most 2 provider requests at once; all completed | PASS |
| failures_stay_with_their_image | one 429 retried and then served, one 401 falls back, four unaffected; each paid attempt in the ledger | PASS |
| duplicate_delivery_sends_once | one provider request, attempts == 1 | PASS |
| cancel_discards_inflight_answers | every job of the run ends cancelled; no answer is published | PASS |
| requests_carry_no_identity | no email, username or file name in any request body | PASS |
