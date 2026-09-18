# Starts the FaceCloud Celery worker for local development.
#
# Prerequisites: Redis must already be reachable at CELERY_BROKER_URL
# (default redis://127.0.0.1:6379/0). Start it once with:
#   docker run --name facecloud-redis -p 127.0.0.1:6379:6379 -d redis:7-alpine
# If the container already exists but is stopped, use `docker start facecloud-redis`.
#
# See docs/MANUAL_TESTING_COMMANDS.md for the full manual end-to-end flow.

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$redisUp = docker ps --filter "name=facecloud-redis" --filter "status=running" --format "{{.Names}}"
if (-not $redisUp) {
    Write-Host "Redis container 'facecloud-redis' is not running." -ForegroundColor Yellow
    $existing = docker ps -a --filter "name=facecloud-redis" --format "{{.Names}}"
    if ($existing) {
        Write-Host "Starting existing container..."
        docker start facecloud-redis | Out-Null
    } else {
        Write-Host "Creating and starting a new container..."
        docker run --name facecloud-redis -p 127.0.0.1:6379:6379 -d redis:7-alpine | Out-Null
    }
}

& ".\venv\Scripts\python.exe" -m celery -A config worker --loglevel=info --pool=solo --hostname=facecloud@worker
