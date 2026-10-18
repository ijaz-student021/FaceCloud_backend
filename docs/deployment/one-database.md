
## After restarting a development server

A restart drops whatever the server was processing (with `DJANGO_DEBUG=True` the work runs inside the web process). The job then reads
`processing` until the reaper fails it, which by default is after more than an hour, and only if `celery beat` is running. To recover at
once:

```
python manage.py reap_stuck_jobs --older-than-seconds 120      # fails jobs "processing" for over two minutes; the owner can retry them
python manage.py reap_stuck_jobs --dry-run --older-than-seconds 120    # see what it would fail
```
