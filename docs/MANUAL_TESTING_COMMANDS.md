# FaceCloud Manual Testing Commands

This runbook tests the current Django backend from PowerShell on Windows. Run all
commands from the repository root unless a section says to use another terminal.

## 1. Test conventions

- API base URL: `http://127.0.0.1:8000`
- Commands use `Invoke-RestMethod` for JSON and `curl.exe` for file uploads and
  binary downloads.
- Keep the Django server and Celery worker running in separate terminals.
- Replace credentials below with a verified development account. Do not use a
  production account or production database.
- HTTP `200`, `201`, `202`, and `204` are successful depending on the operation.
- Background operations may initially return `queued`; poll their detail endpoint
  until they become `completed`, `ready`, `failed`, or `needs_review`.

## 2. Terminal A - activate and validate the project

```powershell
Set-Location "C:\Users\csbah\OneDrive\Desktop\Development\facecloud_backend"
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\venv\Scripts\Activate.ps1
python --version
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py showmigrations
```

Run automated smoke tests before manual API testing:

```powershell
python manage.py test accounts operations identities --keepdb -v 1
```

## 3. Verify or start Redis

Check the existing Docker container:

```powershell
docker ps --filter "name=facecloud-redis" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
docker exec facecloud-redis redis-cli ping
```

If the container exists but is stopped:

```powershell
docker start facecloud-redis
docker exec facecloud-redis redis-cli ping
```

If it does not exist, create it once:

```powershell
docker run --name facecloud-redis -p 127.0.0.1:6379:6379 -d redis:7-alpine
docker exec facecloud-redis redis-cli ping
```

Expected Redis response:

```text
PONG
```

## 4. Terminal B - run the Celery worker

```powershell
Set-Location "C:\Users\csbah\OneDrive\Desktop\Development\facecloud_backend"
.\venv\Scripts\Activate.ps1
python -m celery -A config worker --loglevel=info --pool=solo --hostname=facecloud@worker
```

Verify it from Terminal A:

```powershell
python -m celery -A config inspect ping --timeout=5
python manage.py shell -c "from operations.services import check_redis, check_celery; print(check_redis()); print(check_celery())"
```

Expected result: Redis and Celery report `healthy`, with at least one worker.

## 5. Terminal C - run Django

```powershell
Set-Location "C:\Users\csbah\OneDrive\Desktop\Development\facecloud_backend"
.\venv\Scripts\Activate.ps1
python manage.py migrate
python manage.py runserver 127.0.0.1:8000
```

## 6. Terminal A - initialize API variables

```powershell
$Api = "http://127.0.0.1:8000"
$Username = "YOUR_VERIFIED_USERNAME_OR_EMAIL"
$Password = "YOUR_PASSWORD"
$TestImage = (Resolve-Path ".\Dataset\image\5720.png").Path
$Artifacts = ".\.runtime\manual-tests"
New-Item -ItemType Directory -Force -Path $Artifacts | Out-Null
```

Confirm the image exists:

```powershell
Get-Item -LiteralPath $TestImage | Select-Object FullName,Length,LastWriteTime
```

Optional development-only command to create a verified administrator. This writes
to the configured development database:

```powershell
python manage.py shell -c "from django.contrib.auth import get_user_model; U=get_user_model(); u,created=U.objects.get_or_create(username='manual-admin', defaults={'email':'manual-admin@example.com','role':'admin','is_email_verified':True}); u.role='admin'; u.is_email_verified=True; u.set_password('ChangeMe!12345'); u.save(); print('created' if created else 'updated')"
```

If used, set:

```powershell
$Username = "manual-admin"
$Password = "ChangeMe!12345"
```

## 7. Authentication tests

Login and retain both tokens:

```powershell
$LoginBody = @{ identifier = $Username; password = $Password } | ConvertTo-Json
$Login = Invoke-RestMethod -Method Post -Uri "$Api/api/auth/login/" -ContentType "application/json" -Body $LoginBody
$AccessToken = $Login.access
$RefreshToken = $Login.refresh
$Headers = @{ Authorization = "Bearer $AccessToken" }
$Login | ConvertTo-Json -Depth 5
```

Test the authenticated profile and dashboard:

```powershell
Invoke-RestMethod -Method Get -Uri "$Api/api/user/profile/" -Headers $Headers | ConvertTo-Json -Depth 8
Invoke-RestMethod -Method Get -Uri "$Api/api/user/dashboard/" -Headers $Headers | ConvertTo-Json -Depth 10
Invoke-RestMethod -Method Get -Uri "$Api/api/user/activity/" -Headers $Headers | ConvertTo-Json -Depth 8
```

Refresh the access token:

```powershell
$RefreshBody = @{ refresh = $RefreshToken } | ConvertTo-Json
$Refresh = Invoke-RestMethod -Method Post -Uri "$Api/api/auth/refresh/" -ContentType "application/json" -Body $RefreshBody
$AccessToken = $Refresh.access
$Headers = @{ Authorization = "Bearer $AccessToken" }
```

Negative authentication test; expected status is `401`:

```powershell
try {
    Invoke-RestMethod -Method Get -Uri "$Api/api/user/profile/"
} catch {
    $_.Exception.Response.StatusCode.value__
}
```

## 8. Feature 34 - system health

This endpoint requires an administrator account:

```powershell
$Health = Invoke-RestMethod -Method Get -Uri "$Api/api/system/health/" -Headers $Headers
$Health | ConvertTo-Json -Depth 12
$Health.status
$Health.checks.redis
$Health.checks.celery
$Health.checks.models
$Health.jobs
```

Expected result: HTTP `200`. Overall status is `healthy` or `degraded`; required
dependency failure returns HTTP `503`. No credentials, absolute model paths, or
raw exception messages should appear.

Confirm a regular user receives `403` by logging in as a non-admin and repeating
the request.

## 9. Single-image upload

```powershell
$UploadJson = curl.exe -sS -X POST "$Api/api/images/upload/single/" `
    -H "Authorization: Bearer $AccessToken" `
    -F "file=@$TestImage"
$UploadJson
$Batch = $UploadJson | ConvertFrom-Json
$BatchId = $Batch.id
$ImageId = $Batch.images[0].id
"BatchId=$BatchId ImageId=$ImageId"
```

List and inspect uploads:

```powershell
Invoke-RestMethod -Method Get -Uri "$Api/api/images/batches/" -Headers $Headers | ConvertTo-Json -Depth 10
Invoke-RestMethod -Method Get -Uri "$Api/api/images/batches/$BatchId/" -Headers $Headers | ConvertTo-Json -Depth 10
Invoke-RestMethod -Method Get -Uri "$Api/api/images/$ImageId/" -Headers $Headers | ConvertTo-Json -Depth 8
Invoke-RestMethod -Method Get -Uri "$Api/api/images/my/" -Headers $Headers | ConvertTo-Json -Depth 10
```

## 10. Preprocessing, face validation, and quality

Preprocess the uploaded image:

```powershell
$PreprocessBody = @{ output_format = "png"; output_name = "manual-test"; force = $false } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$Api/api/images/$ImageId/preprocess/" -Headers $Headers -ContentType "application/json" -Body $PreprocessBody | ConvertTo-Json -Depth 8
```

Inspect preprocessing and download the result:

```powershell
Invoke-RestMethod -Method Get -Uri "$Api/api/images/$ImageId/preprocessing/" -Headers $Headers | ConvertTo-Json -Depth 8
curl.exe -sS -L "$Api/api/images/$ImageId/preprocessing/download/" -H "Authorization: Bearer $AccessToken" -o "$Artifacts\manual-preprocessed.png"
Get-Item "$Artifacts\manual-preprocessed.png"
```

Run face validation and quality analysis:

```powershell
Invoke-RestMethod -Method Post -Uri "$Api/api/validation/images/$ImageId/" -Headers $Headers -ContentType "application/json" -Body "{}" | ConvertTo-Json -Depth 10
Invoke-RestMethod -Method Get -Uri "$Api/api/validation/images/$ImageId/" -Headers $Headers | ConvertTo-Json -Depth 10
Invoke-RestMethod -Method Post -Uri "$Api/api/quality/images/$ImageId/" -Headers $Headers -ContentType "application/json" -Body "{}" | ConvertTo-Json -Depth 10
Invoke-RestMethod -Method Get -Uri "$Api/api/quality/images/$ImageId/" -Headers $Headers | ConvertTo-Json -Depth 10
```

## 11. Enhancement pipeline

Queue automatic enhancement:

```powershell
$EnhanceBody = @{ level = "auto" } | ConvertTo-Json
$Enhancement = Invoke-RestMethod -Method Post -Uri "$Api/api/enhancements/images/$ImageId/" -Headers $Headers -ContentType "application/json" -Body $EnhanceBody
$Enhancement | ConvertTo-Json -Depth 10
$EnhancementId = $Enhancement.id
```

Poll until processing finishes:

```powershell
do {
    Start-Sleep -Seconds 2
    $Enhancement = Invoke-RestMethod -Method Get -Uri "$Api/api/enhancements/results/$EnhancementId/" -Headers $Headers
    "Enhancement status: $($Enhancement.status)"
} while ($Enhancement.status -in @("pending", "queued", "processing"))
$Enhancement | ConvertTo-Json -Depth 12
```

Inspect comparison data and download a completed result:

```powershell
Invoke-RestMethod -Method Get -Uri "$Api/api/enhancements/results/$EnhancementId/comparison/" -Headers $Headers | ConvertTo-Json -Depth 12
curl.exe -sS -L "$Api/api/enhancements/results/$EnhancementId/download/" -H "Authorization: Bearer $AccessToken" -o "$Artifacts\manual-enhanced.png"
Get-Item "$Artifacts\manual-enhanced.png"
```

## 12. Duplicate analysis

```powershell
Invoke-RestMethod -Method Post -Uri "$Api/api/duplicates/images/$ImageId/" -Headers $Headers -ContentType "application/json" -Body "{}" | ConvertTo-Json -Depth 10
Invoke-RestMethod -Method Get -Uri "$Api/api/duplicates/images/$ImageId/" -Headers $Headers | ConvertTo-Json -Depth 10
Invoke-RestMethod -Method Get -Uri "$Api/api/duplicates/images/$ImageId/matches/" -Headers $Headers | ConvertTo-Json -Depth 12
```

For a meaningful duplicate test, upload the same image again and analyze the new
image ID.

## 13. Feature 18 - face recognition and identity grouping

Start identity analysis after preprocessing and face validation:

```powershell
$IdentityBody = @{ force = $false } | ConvertTo-Json
$Identity = Invoke-RestMethod -Method Post -Uri "$Api/api/identities/images/$ImageId/" -Headers $Headers -ContentType "application/json" -Body $IdentityBody
$Identity | ConvertTo-Json -Depth 10
```

Poll the analysis:

```powershell
do {
    Start-Sleep -Seconds 2
    $Identity = Invoke-RestMethod -Method Get -Uri "$Api/api/identities/images/$ImageId/" -Headers $Headers
    "Identity status: $($Identity.status)"
} while ($Identity.status -in @("pending", "queued", "processing"))
$Identity | ConvertTo-Json -Depth 12
```

Inspect generated groups and review assignments:

```powershell
$Groups = Invoke-RestMethod -Method Get -Uri "$Api/api/identities/groups/" -Headers $Headers
$Groups | ConvertTo-Json -Depth 12
Invoke-RestMethod -Method Get -Uri "$Api/api/identities/review/" -Headers $Headers | ConvertTo-Json -Depth 12
```

Repeat the upload-to-identity sequence with images of the same person and a
different person. Confirm that similar faces join the same group while dissimilar
faces create a new group or enter review.

## 14. Feature 19 - identity-group management

Select real identifiers from the group responses:

```powershell
$GroupId = YOUR_GROUP_ID
$MembershipId = YOUR_MEMBERSHIP_ID
$TargetGroupId = YOUR_TARGET_GROUP_ID
```

Retrieve and rename a group:

```powershell
Invoke-RestMethod -Method Get -Uri "$Api/api/identities/groups/$GroupId/" -Headers $Headers | ConvertTo-Json -Depth 12
$RenameBody = @{ display_name = "Manual Test Person" } | ConvertTo-Json
Invoke-RestMethod -Method Patch -Uri "$Api/api/identities/groups/$GroupId/rename/" -Headers $Headers -ContentType "application/json" -Body $RenameBody | ConvertTo-Json -Depth 10
```

Set a profile face:

```powershell
$ProfileBody = @{ membership_id = $MembershipId } | ConvertTo-Json
Invoke-RestMethod -Method Patch -Uri "$Api/api/identities/groups/$GroupId/profile/" -Headers $Headers -ContentType "application/json" -Body $ProfileBody | ConvertTo-Json -Depth 10
```

Move a membership:

```powershell
$MoveBody = @{ target_group_id = $TargetGroupId; reason = "Manual correction" } | ConvertTo-Json
Invoke-RestMethod -Method Patch -Uri "$Api/api/identities/memberships/$MembershipId/move/" -Headers $Headers -ContentType "application/json" -Body $MoveBody | ConvertTo-Json -Depth 10
```

Merge a source group into the group identified by `$GroupId`:

```powershell
$SourceGroupId = YOUR_SOURCE_GROUP_ID
$MergeBody = @{ source_group_id = $SourceGroupId; reason = "Confirmed same person" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$Api/api/identities/groups/$GroupId/merge/" -Headers $Headers -ContentType "application/json" -Body $MergeBody | ConvertTo-Json -Depth 12
```

Inspect the audit history:

```powershell
Invoke-RestMethod -Method Get -Uri "$Api/api/identities/history/" -Headers $Headers | ConvertTo-Json -Depth 12
```

Do not run destructive remove/delete commands until the preceding operations are
verified. An empty group can be deleted with:

```powershell
$EmptyGroupId = YOUR_EMPTY_GROUP_ID
$DeleteBody = @{ reason = "Manual test cleanup" } | ConvertTo-Json
Invoke-RestMethod -Method Delete -Uri "$Api/api/identities/groups/$EmptyGroupId/delete/" -Headers $Headers -ContentType "application/json" -Body $DeleteBody
```

## 15. Reports

Feature 24 has a dedicated secure export runbook at
`docs/features/24-identity-group-zip-manual-testing.md`.

Create an image report:

```powershell
$ReportBody = @{ format = "json" } | ConvertTo-Json
$Report = Invoke-RestMethod -Method Post -Uri "$Api/api/reports/images/$ImageId/" -Headers $Headers -ContentType "application/json" -Body $ReportBody
$Report | ConvertTo-Json -Depth 12
$ReportId = $Report.id
```

Poll and download it:

```powershell
do {
    Start-Sleep -Seconds 2
    $Report = Invoke-RestMethod -Method Get -Uri "$Api/api/reports/$ReportId/" -Headers $Headers
    "Report status: $($Report.status)"
} while ($Report.status -in @("queued", "generating"))
curl.exe -sS -L "$Api/api/reports/$ReportId/download/" -H "Authorization: Bearer $AccessToken" -o "$Artifacts\manual-report.json"
```

Repeat with `csv` and `pdf` values to test every supported format.

## 16. Secure batch package

```powershell
$PackageBody = @{ package_type = "complete_batch"; report_format = "csv" } | ConvertTo-Json
$Package = Invoke-RestMethod -Method Post -Uri "$Api/api/downloads/batches/$BatchId/packages/" -Headers $Headers -ContentType "application/json" -Body $PackageBody
$Package | ConvertTo-Json -Depth 12
$PackageId = $Package.id
```

Poll and download:

```powershell
do {
    Start-Sleep -Seconds 2
    $Package = Invoke-RestMethod -Method Get -Uri "$Api/api/downloads/packages/$PackageId/" -Headers $Headers
    "Package status: $($Package.status)"
} while ($Package.status -in @("queued", "generating"))
curl.exe -sS -L "$Api/api/downloads/packages/$PackageId/download/" -H "Authorization: Bearer $AccessToken" -o "$Artifacts\manual-batch.zip"
Get-Item "$Artifacts\manual-batch.zip"
```

## 17. Notifications

```powershell
Invoke-RestMethod -Method Get -Uri "$Api/api/notifications/" -Headers $Headers | ConvertTo-Json -Depth 10
Invoke-RestMethod -Method Get -Uri "$Api/api/notifications/unread-count/" -Headers $Headers | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Get -Uri "$Api/api/notifications/preferences/" -Headers $Headers | ConvertTo-Json -Depth 8
Invoke-RestMethod -Method Post -Uri "$Api/api/notifications/mark-all-read/" -Headers $Headers -ContentType "application/json" -Body "{}" | ConvertTo-Json -Depth 5
```

## 18. Ownership and security checks

Login as a second verified user and store its token in `$OtherToken`. Requests for
the first user's resources must return `404` or `403`:

```powershell
$OtherHeaders = @{ Authorization = "Bearer $OtherToken" }
try {
    Invoke-RestMethod -Method Get -Uri "$Api/api/images/$ImageId/" -Headers $OtherHeaders
} catch {
    $_.Exception.Response.StatusCode.value__
}

try {
    Invoke-RestMethod -Method Get -Uri "$Api/api/identities/groups/$GroupId/" -Headers $OtherHeaders
} catch {
    $_.Exception.Response.StatusCode.value__
}
```

Inspect API responses and confirm they never expose:

- passwords, JWT signing keys, Redis URLs, or database credentials;
- protected face embeddings or identity centroids;
- absolute storage/model paths;
- raw internal exception messages.

## 19. Final health and result checks

```powershell
Invoke-RestMethod -Method Get -Uri "$Api/api/system/health/" -Headers $Headers | ConvertTo-Json -Depth 12
Invoke-RestMethod -Method Get -Uri "$Api/api/images/batches/$BatchId/progress/" -Headers $Headers | ConvertTo-Json -Depth 12
Invoke-RestMethod -Method Get -Uri "$Api/api/enhancements/results/" -Headers $Headers | ConvertTo-Json -Depth 10
Invoke-RestMethod -Method Get -Uri "$Api/api/reports/" -Headers $Headers | ConvertTo-Json -Depth 10
Invoke-RestMethod -Method Get -Uri "$Api/api/downloads/" -Headers $Headers | ConvertTo-Json -Depth 10
```

Record for every test:

- command and endpoint;
- HTTP status;
- returned resource ID;
- final background status;
- expected and actual result;
- screenshot or saved response for failures;
- relevant Celery log lines.

## 20. Stop local services after testing

Stop Django and the foreground Celery worker with `Ctrl+C` in their terminals.

Redis can remain running. To stop it explicitly:

```powershell
docker stop facecloud-redis
```
