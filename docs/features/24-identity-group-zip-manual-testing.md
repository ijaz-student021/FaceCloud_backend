# Feature 24 Manual Testing - Identity-Group ZIP Export

Run these commands from the FaceCloud backend repository in PowerShell. Execute
each code block separately. Do not copy Markdown fence markers.

## 1. Confirm services

```powershell
docker exec facecloud-redis redis-cli ping
python -m celery -A config inspect ping --timeout=5
```

Expected results are `PONG`, one Celery node, and `pong`.

Verify that the Feature 24 task is registered:

```powershell
python -m celery -A config inspect registered | Select-String "identities.generate_group_export"
```

Confirm Django is available:

```powershell
$Api = "http://127.0.0.1:8000"
try {
    Invoke-WebRequest -Method Get -Uri "$Api/api/auth/login/" -UseBasicParsing -ErrorAction Stop
} catch {
    if ($_.Exception.Response) {
        Write-Host "Django responded with HTTP" ([int]$_.Exception.Response.StatusCode)
    } else {
        throw
    }
}
```

HTTP `405` is expected because the login route accepts POST, not GET. A connection
error means Django must be started in another terminal:

```powershell
python manage.py runserver 127.0.0.1:8000
```

## 2. Login as the export owner

Use a verified development account that already owns an identity group:

```powershell
$Username = "YOUR_USERNAME"
$Password = "YOUR_PASSWORD"
$LoginBody = @{ identifier = $Username; password = $Password } | ConvertTo-Json
$Login = Invoke-RestMethod -Method Post -Uri "$Api/api/auth/login/" -ContentType "application/json" -Body $LoginBody -ErrorAction Stop
$AccessToken = $Login.access
$RefreshToken = $Login.refresh
$Headers = @{ Authorization = "Bearer $AccessToken" }
$Artifacts = ".\.runtime\manual-tests\feature-24"
New-Item -ItemType Directory -Force -Path $Artifacts | Out-Null
Write-Host "Login successful:" (-not [string]::IsNullOrWhiteSpace($AccessToken))
```

If an access token expires, log in again. With refresh-token rotation enabled,
never reuse an already consumed refresh token.

## 3. Select an owned identity group

```powershell
$GroupResponse = Invoke-RestMethod -Method Get -Uri "$Api/api/identities/groups/" -Headers $Headers -ErrorAction Stop
$GroupList = if ($GroupResponse.results) { @($GroupResponse.results) } else { @($GroupResponse) }
$GroupList | Select-Object id,display_name,state,member_count | Format-Table
$Group = $GroupList | Where-Object { $_.state -eq "active" -and $_.member_count -gt 0 } | Select-Object -First 1
$GroupId = $Group.id
Write-Host "Selected group:" $GroupId $Group.display_name
```

`$GroupId` must contain an integer. If it is empty, complete Feature 18 identity
grouping first or select a populated group manually:

```powershell
$GroupId = 1
```

Replace `1` with an ID shown in the group list.

## 4. Queue a ZIP export

```powershell
$ExportBody = @{
    include_originals = $true
    include_preprocessed = $true
    include_enhanced = $true
    include_report = $true
    report_format = "json"
} | ConvertTo-Json

$Queued = Invoke-RestMethod -Method Post -Uri "$Api/api/identities/groups/$GroupId/exports/" -Headers $Headers -ContentType "application/json" -Body $ExportBody -ErrorAction Stop
$Queued | ConvertTo-Json -Depth 12
$ExportId = $Queued.export.id
Write-Host "Created:" $Queued.created "Export ID:" $ExportId
```

Expected HTTP status is `202` for a new export. An identical active request may
return `200` with `created: false` and reuse the safe in-progress export.

## 5. Poll export status

```powershell
if (-not $ExportId) {
    throw "No export ID was returned; do not start polling."
}

do {
    Start-Sleep -Seconds 2
    $Export = Invoke-RestMethod -Method Get -Uri "$Api/api/identities/exports/$ExportId/" -Headers $Headers -ErrorAction Stop
    Write-Host "Status:" $Export.status "Progress:" $Export.progress_percentage
} while ($Export.status -in @("queued", "generating"))

$Export | ConvertTo-Json -Depth 12
```

Expected final status is `ready`. If it is `failed`, inspect the sanitized
`failure_reason` plus the Celery server log; the API must not reveal a traceback,
credential, or filesystem path.

## 6. Download the ZIP

```powershell
$ZipPath = Join-Path $Artifacts "identity-group-$GroupId-export-$ExportId.zip"
curl.exe -f -sS -L "$Api/api/identities/exports/$ExportId/download/" -H "Authorization: Bearer $AccessToken" -o "$ZipPath"
Get-Item -LiteralPath $ZipPath | Select-Object FullName,Length,LastWriteTime
```

Confirm that the file is a valid ZIP:

```powershell
Add-Type -AssemblyName System.IO.Compression.FileSystem
$Zip = [System.IO.Compression.ZipFile]::OpenRead((Resolve-Path $ZipPath))
$Zip.Entries | Select-Object FullName,Length | Format-Table
$Zip.Dispose()
```

## 7. Inspect the manifest and report

```powershell
$ExtractPath = Join-Path $Artifacts "export-$ExportId-extracted"
New-Item -ItemType Directory -Force -Path $ExtractPath | Out-Null
Expand-Archive -LiteralPath $ZipPath -DestinationPath $ExtractPath -Force
Get-ChildItem -LiteralPath $ExtractPath -Recurse -File | Select-Object FullName,Length
$Manifest = Get-Content -LiteralPath (Join-Path $ExtractPath "manifest.json") -Raw | ConvertFrom-Json
$Manifest | ConvertTo-Json -Depth 12
```

Verify expected ownership-safe contents:

```powershell
$Manifest.group_id -eq $GroupId
$Manifest.summary.unique_images
$Manifest.summary.included_files
$Manifest.skipped_files
```

Search for prohibited sensitive fields. The command should return no matches:

```powershell
Get-ChildItem -LiteralPath $ExtractPath -Recurse -File |
    Where-Object { $_.Extension -in @(".json", ".csv", ".txt") } |
    Select-String -Pattern "protected_embedding|protected_centroid|IDENTITY_EMBEDDING_KEY|DB_PASSWORD|CELERY_BROKER_URL|^[A-Za-z]:\\" -ErrorAction Stop
```

Confirm no archive entry is absolute or contains directory traversal:

```powershell
$Zip = [System.IO.Compression.ZipFile]::OpenRead((Resolve-Path $ZipPath))
$UnsafeEntries = @($Zip.Entries | Where-Object {
    $_.FullName.StartsWith("/") -or
    $_.FullName.StartsWith("\\") -or
    $_.FullName -match "(^|/)\.\.(/|$)" -or
    $_.FullName -match "^[A-Za-z]:"
})
$Zip.Dispose()
Write-Host "Unsafe entries:" $UnsafeEntries.Count
```

Expected result:

```text
Unsafe entries: 0
```

## 8. Verify export listing and download history

```powershell
Invoke-RestMethod -Method Get -Uri "$Api/api/identities/groups/$GroupId/exports/" -Headers $Headers -ErrorAction Stop | ConvertTo-Json -Depth 12
Invoke-RestMethod -Method Get -Uri "$Api/api/downloads/?resource_type=identity_group_package" -Headers $Headers -ErrorAction Stop | ConvertTo-Json -Depth 10
```

The history must contain the owner's successful Feature 24 download.

## 9. Mandatory cross-user security test

Log in as a different verified development user:

```powershell
$OtherUsername = "OTHER_VERIFIED_USERNAME"
$OtherPassword = "OTHER_PASSWORD"
$OtherLoginBody = @{ identifier = $OtherUsername; password = $OtherPassword } | ConvertTo-Json
$OtherLogin = Invoke-RestMethod -Method Post -Uri "$Api/api/auth/login/" -ContentType "application/json" -Body $OtherLoginBody -ErrorAction Stop
$OtherHeaders = @{ Authorization = "Bearer $($OtherLogin.access)" }
```

Every request below must return HTTP `404`:

```powershell
$ProtectedRequests = @(
    @{ Method = "GET"; Uri = "$Api/api/identities/groups/$GroupId/exports/" },
    @{ Method = "GET"; Uri = "$Api/api/identities/exports/$ExportId/" },
    @{ Method = "GET"; Uri = "$Api/api/identities/exports/$ExportId/download/" },
    @{ Method = "POST"; Uri = "$Api/api/identities/exports/$ExportId/regenerate/" },
    @{ Method = "DELETE"; Uri = "$Api/api/identities/exports/$ExportId/" }
)

foreach ($Request in $ProtectedRequests) {
    try {
        $RequestParameters = @{
            Method = $Request.Method
            Uri = $Request.Uri
            Headers = $OtherHeaders
            ErrorAction = "Stop"
        }
        if ($Request.Method -eq "POST") {
            $RequestParameters.ContentType = "application/json"
            $RequestParameters.Body = "{}"
        }
        Invoke-RestMethod @RequestParameters | Out-Null
        Write-Host "SECURITY FAILURE:" $Request.Method $Request.Uri
    } catch {
        $Code = [int]$_.Exception.Response.StatusCode
        Write-Host $Code $Request.Method $Request.Uri
    }
}
```

Expected result: five `404` responses. Verify the other user has no matching
download-history event:

```powershell
Invoke-RestMethod -Method Get -Uri "$Api/api/downloads/?resource_type=identity_group_package" -Headers $OtherHeaders -ErrorAction Stop | ConvertTo-Json -Depth 10
```

## 10. Development-only expiry and regeneration test

This command modifies only the selected development export record so that expiry
can be tested without waiting 24 hours:

```powershell
python manage.py shell -c "from django.utils import timezone; from identities.models import IdentityGroupExport; e=IdentityGroupExport.objects.get(pk=$ExportId, owner__username='$Username'); e.expires_at=timezone.now(); e.save(update_fields=['expires_at','updated_at']); print(e.pk, e.expires_at)"
```

The owner's next download must return HTTP `410` and delete the stored ZIP:

```powershell
try {
    Invoke-WebRequest -Method Get -Uri "$Api/api/identities/exports/$ExportId/download/" -Headers $Headers -UseBasicParsing -ErrorAction Stop
} catch {
    Write-Host "Expiry status:" ([int]$_.Exception.Response.StatusCode)
}
```

Regenerate it:

```powershell
$Regenerated = Invoke-RestMethod -Method Post -Uri "$Api/api/identities/exports/$ExportId/regenerate/" -Headers $Headers -ContentType "application/json" -Body "{}" -ErrorAction Stop
$NewExportId = $Regenerated.export.id
$Regenerated | ConvertTo-Json -Depth 10
Write-Host "New export ID:" $NewExportId
```

## 11. Owner deletion test

Delete only a disposable export after testing:

```powershell
$DisposableExportId = $NewExportId
Invoke-RestMethod -Method Delete -Uri "$Api/api/identities/exports/$DisposableExportId/" -Headers $Headers -ErrorAction Stop
Write-Host "Deleted export:" $DisposableExportId
```

Confirm it is no longer visible; expected status is `404`:

```powershell
try {
    Invoke-RestMethod -Method Get -Uri "$Api/api/identities/exports/$DisposableExportId/" -Headers $Headers -ErrorAction Stop
} catch {
    Write-Host "Deleted export status:" ([int]$_.Exception.Response.StatusCode)
}
```

## 12. Acceptance checklist

- Owner can queue and list an export.
- Celery changes it from `queued` to `generating` to `ready`.
- ZIP downloads successfully and opens normally.
- Included image variants match the selected options.
- Multiple faces from one image do not duplicate that image.
- Manifest group ID matches the owned group.
- No embeddings, centroids, credentials, paths, or tracebacks are exposed.
- ZIP contains no absolute or traversal paths.
- Download history records the owner event.
- A different user receives `404` from every protected endpoint.
- Expired ZIP is deleted and returns `410`.
- Regeneration creates a new export.
- Owner deletion removes the export and stored ZIP.
