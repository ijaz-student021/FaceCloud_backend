[CmdletBinding()]
param(
    [ValidateSet("auto", "cpu", "cuda")]
    [string]$Device = "auto",
    [string]$LogLevel = "info"
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repositoryRoot ".runtime\venvs\gfpgan\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw (
        "The isolated GFPGAN environment is missing. Expected: $python"
    )
}

Set-Location -LiteralPath $repositoryRoot
$env:GFPGAN_ENABLED = "True"
$env:GFPGAN_QUEUE_ENABLED = "True"
$env:GFPGAN_DEVICE = $Device

& $python manage.py check_gfpgan --load-model
if ($LASTEXITCODE -ne 0) {
    throw "GFPGAN preflight failed; the worker was not started."
}

& $python -m celery -A config worker `
    --loglevel=$LogLevel `
    --pool=solo `
    --queues=gfpgan `
    --hostname="facecloud-gfpgan@%h"
