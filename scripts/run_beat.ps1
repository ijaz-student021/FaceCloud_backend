# Starts the FaceCloud Celery beat scheduler for local development.
#
# Beat only enqueues the periodic maintenance tasks defined in
# CELERY_BEAT_SCHEDULE (config/settings.py); a worker (scripts/run_worker.ps1)
# must be running to execute them. Run exactly one beat process per deployment.

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

New-Item -ItemType Directory -Force ".runtime" | Out-Null
& ".\venv\Scripts\python.exe" -m celery -A config beat --loglevel=info --schedule ".runtime\celerybeat-schedule"
