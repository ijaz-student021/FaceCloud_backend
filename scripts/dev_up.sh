#!/usr/bin/env bash
# One command for development and for the demonstration, in the REAL mode: Redis, Django, a Celery worker and Celery beat, with CELERY_TASK_ALWAYS_EAGER=False.
#   bash scripts/dev_up.sh                 # Django on http://127.0.0.1:8000
#   PORT=8001 bash scripts/dev_up.sh
#
# Why: with CELERY_TASK_ALWAYS_EAGER=True (the DEBUG default) an upload is processed INSIDE the request, so it blocks for the whole enhancement and the screen shows no progress
# (system check operations.W001). Here the upload returns at once and GET /api/images/batches/<id>/progress/ moves through the real stages
# (queued, preprocessing, face_validation, quality_analysis, enhancement, identity_grouping, finalizing, completed).
#
# It first checks Redis (starts one if redis-server is installed and none runs), that the migrations are applied, and which restoration stages can run
# (manage.py check_restoration --require upscale). It stops everything it started when you press Ctrl-C. Logs: $FACECLOUD_LOG_DIR (default /tmp/facecloud-dev).
# Settings come from .env as usual; DB_NAME, MEDIA_ROOT and the rest can be overridden in the environment (the defence demo uses a separate database that way).
set -euo pipefail
cd "$(dirname "$0")/.."

for candidate in "${PYTHON:-}" venv/bin/python .venv/bin/python python3; do
  [ -n "$candidate" ] && command -v "$candidate" >/dev/null 2>&1 && PY="$candidate" && break
done
: "${PY:?No Python found: set PYTHON=/path/to/python}"
PORT="${PORT:-8000}"
LOGS="${FACECLOUD_LOG_DIR:-/tmp/facecloud-dev}"
mkdir -p "$LOGS"

export DJANGO_DEBUG="${DJANGO_DEBUG:-True}"
export CELERY_TASK_ALWAYS_EAGER=False          # the point of this script

# --- 1. Redis -------------------------------------------------------------------------------------------------------------------------------------
BROKER="${CELERY_BROKER_URL:-redis://127.0.0.1:6379/0}"
STARTED_REDIS=""
if ! redis-cli -u "$BROKER" ping >/dev/null 2>&1; then
  if command -v redis-server >/dev/null 2>&1; then
    redis-server --daemonize yes --port "$(echo "$BROKER" | sed -E 's#.*:([0-9]+)/.*#\1#')" --save "" --dir "$LOGS" >/dev/null
    STARTED_REDIS=1
    for _ in $(seq 1 20); do redis-cli -u "$BROKER" ping >/dev/null 2>&1 && break; sleep 0.25; done
  fi
fi
redis-cli -u "$BROKER" ping >/dev/null 2>&1 || { echo "Redis is not running at $BROKER and could not be started (brew install redis)."; exit 1; }
echo "Redis    : ok ($BROKER)${STARTED_REDIS:+, started by this script}"

# --- 2. migrations and models ------------------------------------------------------------------------------------------------------------------
"$PY" manage.py migrate --check >/dev/null 2>&1 || { echo "Unapplied migrations: run  $PY manage.py migrate  first."; exit 1; }
echo "Database : migrations applied"
echo "Models   :"
"$PY" manage.py check_restoration --require upscale 2>&1 | sed 's/^/           /' || { echo "A required restoration stage cannot run (above). Fix it, or start without it: SKIP_RESTORATION_CHECK=1 bash scripts/dev_up.sh"; [ "${SKIP_RESTORATION_CHECK:-}" = "1" ] || exit 1; }

# --- 3. the worker (and beat), stopped with the script ---------------------------------------------------------------------------------------------
PIDS=()
cleanup() {
  trap - EXIT INT TERM
  echo; echo "Stopping worker, beat${STARTED_REDIS:+ and Redis} ..."
  for pid in "${PIDS[@]:-}"; do [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; done
  [ -n "$STARTED_REDIS" ] && redis-cli -u "$BROKER" shutdown nosave >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

QUEUES="celery${ENHANCEMENT_QUEUE_NAME:+,$ENHANCEMENT_QUEUE_NAME}${ENHANCEMENT_EDIT_QUEUE_NAME:+,$ENHANCEMENT_EDIT_QUEUE_NAME}"
# macOS: Celery's prefork pool fails on the first task there ("ValueError: not enough values to unpack (expected 3, got 0)" in celery/app/trace.py, Celery 5.6, Python 3.12 and 3.14; measured
# 2026-11-04) and forking after PyTorch/MPS is loaded is unsafe anyway. One model at a time is what we want, so the "solo" pool (the task runs in the worker process itself) is the default
# on macOS; elsewhere it is prefork with one process, as in docs/deployment/VPS_RUNBOOK.md. Override: CELERY_POOL=prefork|solo|threads.
POOL="${CELERY_POOL:-$([ "$(uname)" = "Darwin" ] && echo solo || echo prefork)}"
"$PY" -m celery -A config worker -l info --pool="$POOL" --concurrency=1 -Q "$QUEUES" >"$LOGS/worker.log" 2>&1 &
PIDS+=($!)
"$PY" -m celery -A config beat -l info --schedule "$LOGS/celerybeat-schedule" >"$LOGS/beat.log" 2>&1 &
PIDS+=($!)
for _ in $(seq 1 40); do grep -q "ready\." "$LOGS/worker.log" 2>/dev/null && break; sleep 0.5; done
grep -q "ready\." "$LOGS/worker.log" 2>/dev/null && echo "Worker   : ready (pool: $POOL, queues: $QUEUES; one photograph is enhanced at a time)" \
  || echo "Worker   : started, not yet ready; see $LOGS/worker.log"
echo "Beat     : started"
echo
echo "API      : http://127.0.0.1:$PORT/api/        docs: http://127.0.0.1:$PORT/api/docs/        health: http://127.0.0.1:$PORT/api/system/health/ (admin)"
echo "Progress : GET /api/images/batches/<id>/progress/   (the upload answers at once; the stages move while the worker enhances)"
echo "Logs     : $LOGS/worker.log  $LOGS/beat.log        Stop: Ctrl-C"
echo

# --- 4. Django, in the foreground -----------------------------------------------------------------------------------------------------------------
"$PY" manage.py runserver "127.0.0.1:$PORT"
