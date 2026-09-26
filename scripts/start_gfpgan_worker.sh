#!/usr/bin/env bash
# Linux equivalent of scripts/start_gfpgan_worker.ps1 for a GPU machine.
#
# NOT YET RUN ON LINUX: it is a direct translation of the PowerShell launcher that has
# been used on Windows. Treat the first run as part of the GPU session's checks.
#
# Usage: scripts/start_gfpgan_worker.sh [auto|cpu|cuda] [loglevel]
set -euo pipefail

DEVICE="${1:-auto}"
LOG_LEVEL="${2:-info}"
case "$DEVICE" in auto|cpu|cuda) ;; *) echo "device must be auto, cpu or cuda" >&2; exit 2 ;; esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.runtime/venvs/gfpgan/bin/python"
if [ ! -x "$PYTHON" ]; then
  echo "The isolated GFPGAN environment is missing. Expected: $PYTHON" >&2
  exit 1
fi

cd "$ROOT"
export GFPGAN_ENABLED=True
export GFPGAN_QUEUE_ENABLED=True
export GFPGAN_DEVICE="$DEVICE"

"$PYTHON" manage.py check_gfpgan --load-model || { echo "GFPGAN preflight failed; the worker was not started." >&2; exit 1; }

exec "$PYTHON" -m celery -A config worker \
  --loglevel="$LOG_LEVEL" \
  --pool=solo \
  --queues=gfpgan \
  --hostname="facecloud-gfpgan@%h"
