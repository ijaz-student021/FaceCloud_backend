# What is deployed where (written 2026-11-25, after the first real deployment)

No secrets are in this file; every key lives only in `/etc/facecloud/facecloud.env` on the server.

| Part | Where | Notes |
|---|---|---|
| Frontend (React build) | Hostinger shared hosting, `public_html` of facecloudai.com (www and the apex) | Built with `VITE_USE_API=true`, `VITE_API_URL=https://api.facecloudai.com`; uploaded by hand (File Manager); keep `.htaccess` (single-page-app rewrite) |
| Backend API | Hostinger KVM 2 VPS (2 vCPU, 7.8 GB RAM, Ubuntu 24.04), `https://api.facecloudai.com` | Nginx + Let's Encrypt in front of gunicorn (unix socket); Celery worker and beat under systemd; PostgreSQL and Redis on localhost only |
| Database | PostgreSQL `facecloud` on the VPS | Separate from the laptop's SQLite; never copy one into the other (the encryption and embedding keys differ) |
| Photos | `/var/lib/facecloud/media` (encrypted at rest, not served by Nginx) | Daily backup is in `VPS_RUNBOOK.md` section 10 (install it) |
| Models | `/srv/facecloud/app/.runtime/models` | CPU torch; YuNet, SFace, Zero-DCE, NAFNet GoPro, Real-ESRGAN, CodeFormer, DDColor; semantic search (SigLIP) is off on the VPS |
| E-mail | Hostinger mailbox via smtp.hostinger.com:587 (STARTTLS) | The mailbox plan must stay active: after it expired, the server refused every sender with `553 Sender address rejected` and no verification mail could be sent |
| Cloud enhancement | Deep Image, Pro preset only (`CLOUD_ENHANCEMENT_WHEN=ai_style`) | `DEEP_IMAGE_PRESET=auto_enhance` makes the provider enlarge to 4096 px; `auto_enhance_pro` keeps the photo's size. Consent is required in production |

## Deploying a new `main`
`bash scripts/deploy_vps.sh` from a Mac (asks the `deploy` password once for ssh and once for sudo). It packs the tracked files of `main`, backs up the current code on the server, replaces it (keeping `.runtime`), installs requirements, runs `check --deploy`, `migrate`, `collectstatic`, restarts the three services and runs a smoke test. Rollback: the last line it prints.

## Settings that mattered on this server (names only)
`CLOUD_ENHANCEMENT_MIN_STRUCTURE_SSIM` (0.30; accepted cloud results measured 0.91 to 0.94), `CLOUD_ENHANCEMENT_LANDMARK_SHIFT_MAX` (0.10), `CLOUD_ENHANCEMENT_MIN_IDENTITY` (0.50), `ENHANCEMENT_MAX_INPUT_PIXELS` (13 000 000), `DEEP_IMAGE_DEADLINE_SECONDS` (300), `DEEP_IMAGE_REQUEST_TIMEOUT_SECONDS` (90), `ADAPTIVE_TIME_BUDGET_SECONDS` (240).

## Diagnosing a Pro photograph that was not enhanced by the cloud
`journalctl -u facecloud-worker | grep -iE "Deep Image answered|cloud_enhancement"`. The reason codes: `unauthorized`/`rejected` with HTTP 404 "No user found for provided token" = wrong API key; `no_credits`; `plan_required`; `no_consent` (the owner has not switched on face processing in Settings); `structure_changed` (the structure gate refused the provider's result); `not_requested` (an Auto photograph: the cloud is for Pro only).
