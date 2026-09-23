# Feature 20: Background job operations

## Status

Complete for the backend: scheduled maintenance, stuck-job recovery, per-user
concurrency limits and an administrator queue snapshot. The retry, timeout and
cancellation parts of Feature 20 are described in
[20-21-live-batch-progress.md](20-21-live-batch-progress.md).

## Why this exists

Three gaps showed up when the background pipeline was reviewed:

1. Five maintenance tasks existed but nothing ran them, because the project had
   no Celery beat schedule. Expired download packages, expired identity exports
   and old notifications were never cleaned up, and the privacy retention purge
   and temp-file cleanup only ran if someone ran the command by hand.
2. A worker that died (or a Redis queue that was flushed) left its database row
   in `queued` or `processing` forever. The app allows one active enhancement per
   image and one active package per batch, so the user could not simply try
   again.
3. One user could enqueue unlimited work and starve everyone else.

## Beat schedule

Defined in `CELERY_BEAT_SCHEDULE` (`config/settings.py`). Start exactly one beat
process next to the worker: `.\scripts\run_beat.ps1`.

| Entry | Task | When |
|---|---|---|
| reap-stuck-jobs | `operations.reap_stuck_jobs` | every 5 minutes |
| expire-download-packages | `downloads.cleanup_expired` | hourly, minute 5 |
| expire-identity-exports | `identities.cleanup_expired_exports` | hourly, minute 10 |
| cleanup-temporary-files | `privacy.cleanup_temporary_files` | every 2 hours |
| purge-expired-facial-data | `privacy.purge_expired_facial_data` | daily 03:15 |
| expire-share-links | `sharing.cleanup_expired` | daily 04:00 |
| expire-model-comparisons | `enhancement.cleanup_comparisons` | daily 04:30 |
| expire-notifications | `notifications.cleanup_expired` | daily 03:45 |

Every entry is safe to repeat and does nothing when its feature is off (the
retention purge is a no-op while `FACIAL_DATA_RETENTION_DAYS=0`). A test checks
that each scheduled name is a registered task, since a typo would otherwise fail
silently, and that temp-file cleanup runs more often than
`TEMP_FILE_MAX_AGE_HOURS`.

Beat keeps its state in `.runtime/celerybeat-schedule`, which is git-ignored.

## Stuck-job recovery

`operations/jobs.py` defines the job kinds it watches: enhancement results,
enhancement batch jobs, model comparisons, gallery edits, identity analyses, preprocessing results,
upload batches, download packages, processing reports and identity exports.

A row is stale when it has not changed for:

- **processing:** `JOB_PROCESSING_STALE_SECONDS` (default: task time limit + 10
  minutes). A task cannot run longer than the time limit, so a row this old has
  no live task.
- **queued:** `JOB_QUEUED_STALE_HOURS` (default 6). Queued work can legitimately
  wait behind a backlog, so it gets a much longer allowance.

Stale rows are set to `failed` with the reason "The background worker stopped
responding, so this job was marked failed automatically. You can start it
again.", the finish time is recorded where the model has one, and batch progress
is re-synced for the affected images. The update re-applies the staleness filter
in a single statement, so a job that finishes while the reaper runs is not
overwritten. Running it twice does nothing the second time.

```powershell
python manage.py reap_stuck_jobs --dry-run     # report only
python manage.py reap_stuck_jobs
```

Limits worth knowing:

- Reaping a batch job does not recompute its counters; its child results are
  reaped on their own and the batch progress endpoint reads them directly.
- If a stale task was only delayed (not lost) and later starts, it may complete a
  row that was already marked failed. That heals itself and is harmless.

## Per-user concurrency limits

Requests past the limit get HTTP **429** with the code `too_many_active_jobs`
and a message stating the limit, instead of queueing behind other users.

| Setting | Default | Counts |
|---|---|---|
| `MAX_ACTIVE_ENHANCEMENTS_PER_USER` | 10 | single-image enhancements that are pending, queued or processing |
| `MAX_ACTIVE_BATCH_ENHANCEMENTS_PER_USER` | 2 | batch enhancement jobs in those states |

Results that belong to a batch job do not count against the single-image limit;
otherwise every batch of more than ten images would be impossible. `0` disables
a limit. Retrying a failed image is not a new request and is not limited.

## Administrator queue snapshot

`GET /api/system/queue/` (administrators only) returns, per job kind: how many
are queued and processing, how many are stale, the age of the oldest active
row, and the total failed. It also echoes the two stale thresholds. It is
read-only; failed-job management belongs to Feature 33.

## Verification

- `operations/test_jobs.py`: staleness rules for processing and queued rows,
  finished jobs left alone, dry run, idempotence, retry possible after reaping,
  progress re-sync, all other job kinds, the management command, the snapshot and
  its admin-only permission.
- `enhancement/test_capacity.py`: 429 for single and batch limits, finished and
  other users' work not counted, batch results not counted as singles, limit `0`.
- `config/test_beat_schedule.py`: scheduled names are registered tasks.
