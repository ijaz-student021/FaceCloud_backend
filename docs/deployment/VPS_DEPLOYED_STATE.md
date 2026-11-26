# What is deployed where (written 2026-11-25, after the first real deployment)

No secrets are in this file; every key lives only in `/etc/facecloud/facecloud.env` on the server.

| Part | Where | Notes |
|---|---|---|
| Frontend (React build) | Hostinger shared hosting, `public_html` of facecloudai.com (www and the apex) | Built with `VITE_USE_API=true`, `VITE_API_URL=https://api.facecloudai.com`; uploaded by hand (File Manager) or with `scripts/deploy_frontend.sh`; keep `.htaccess` (single-page-app rewrite) |
| Backend API | Hostinger KVM 2 VPS (2 vCPU, 7.8 GB RAM, Ubuntu 24.04), `https://api.facecloudai.com` | Nginx + Let's Encrypt in front of gunicorn (unix socket, **2 workers x 2 threads**, see below); Celery worker (`--concurrency=1`) and beat under systemd; PostgreSQL and Redis on localhost only |
| Database | PostgreSQL `facecloud` on the VPS | Separate from the laptop's SQLite; never copy one into the other (the encryption and embedding keys differ) |
| Photos | `/var/lib/facecloud/media` (encrypted at rest, not served by Nginx) | Daily backup (database dump and media) is installed: cron `/etc/cron.d/facecloud-backup` at 02:15 runs `/usr/local/sbin/facecloud-backup` (root only, mode 750) into `/var/backups/facecloud`; see `VPS_RUNBOOK.md` section 10. Check with `sudo ls -lh --time-style=long-iso /var/backups/facecloud`. Keep a copy of a dump off the server |
| Models | `/srv/facecloud/app/.runtime/models` | CPU torch; YuNet, SFace, Zero-DCE, NAFNet GoPro, Real-ESRGAN, CodeFormer, DDColor; semantic search (SigLIP ViT-B/16) is **on** since 2026-11-25 (see below) |
| E-mail | Hostinger mailbox via smtp.hostinger.com:587 (STARTTLS) | The mailbox plan must stay active: after it expired, the server refused every sender with `553 Sender address rejected` and no verification mail could be sent |
| Cloud enhancement | Deep Image, Pro preset only (`CLOUD_ENHANCEMENT_WHEN=ai_style`) | `DEEP_IMAGE_PRESET=auto_enhance` makes the provider enlarge to 4096 px; `auto_enhance_pro` keeps the photo's size. Consent is required in production |

## Deploying a new `main`
`bash scripts/deploy_vps.sh` from a Mac (asks the `deploy` password once for ssh and once for sudo). It packs the tracked files of `main`, backs up the current code on the server, replaces it (keeping `.runtime`), installs requirements, runs `check --deploy`, `migrate`, `collectstatic`, restarts the three services and runs a smoke test. Rollback: the last line it prints.

## Settings that mattered on this server (names only)
`CLOUD_ENHANCEMENT_MIN_STRUCTURE_SSIM` (0.30; accepted cloud results measured 0.91 to 0.94), `CLOUD_ENHANCEMENT_LANDMARK_SHIFT_MAX` (0.10), `CLOUD_ENHANCEMENT_MIN_IDENTITY` (0.50), `ENHANCEMENT_MAX_INPUT_PIXELS` (13 000 000), `DEEP_IMAGE_DEADLINE_SECONDS` (300), `DEEP_IMAGE_REQUEST_TIMEOUT_SECONDS` (90), `ADAPTIVE_TIME_BUDGET_SECONDS` (240).

## Diagnosing a Pro photograph that was not enhanced by the cloud
`journalctl -u facecloud-worker | grep -iE "Deep Image answered|cloud_enhancement"`. The reason codes: `unauthorized`/`rejected` with HTTP 404 "No user found for provided token" = wrong API key; `no_credits`; `plan_required`; `no_consent` (the account has not allowed face processing: the Pro request is not sent at all, `attempted: false`; the upload screens now ask for this permission when Pro is chosen, frontend PR #10, so a person is not left on the standard result without knowing why; consent is per account); `structure_changed` (the structure gate refused the provider's result); `not_requested` (an Auto photograph: the cloud is for Pro only).

## Deploying the frontend (it is separate from the backend)
Changing the frontend code does not change the live site by itself: only the steps below do.
1. `bash scripts/build_frontend.sh` builds in a clean copy with `VITE_USE_API=true` and `VITE_API_URL=https://api.facecloudai.com` and **refuses to make the zip if the bundle does not contain the API address**. A build without those two variables is a silent "demo mode" site: register never reaches the server, no e-mail is sent and the Verify button stays enabled. That is what the live site was serving on 2026-11-25 until the right bundle was uploaded. Build in a clean copy because a local `.env.local` changes the bundle (3.2 MB instead of 2.5 MB).
2. Either upload the zip by hand (delete `assets/` and `index.html` in `public_html`, upload, extract, keep `.htaccess`), or run `scripts/deploy_frontend.sh` (dry run by default, `--go` to upload). It needs `FRONTEND_SSH` (user@host), `FRONTEND_SSH_PORT` (Hostinger: 65002) and `FRONTEND_REMOTE` (the facecloudai.com `public_html` folder only: the hosting account also serves other sites). New `assets/` go up first, `index.html` last, old assets are removed at the end, `.htaccess` and the other folders are never deleted, and a backup of `index.html` and `assets/` is saved first.
3. Check: `curl -s https://www.facecloudai.com/ | grep -o 'assets/index-[^"]*\.js'` must show the name the build script printed.

**A merge into the frontend's `main` can replace the live site by itself.** On 2026-11-26 the live bundle was replaced 49 seconds after a pull request was merged: a build in demo mode (no API address in the bundle) took the place of the API-mode build, and people who had registered earlier could not log in. The cause was not confirmed (the timing points to an automatic build on the hosting side); nothing in the repository does it. Until a committed `.env.production` makes every build API-mode, check the bundle after **every** merge to the frontend's `main` and run `deploy_frontend.sh --go` again if it is not the API-mode one. A demo-mode bundle has no `api.facecloudai.com` and no `auth/register` in it.

## Gunicorn worker count (a server-only setting)
`/etc/systemd/system/facecloud-web.service` runs `--workers 2 --threads 2 --timeout 120`. With semantic search on, each gunicorn worker holds about 1.25 GB (the search model is loaded per process); three workers left about 2.3 GB of memory free, two leave about 6.8 GB. This line is not in git. Rollback: `--workers 3`, then `sudo systemctl daemon-reload && sudo systemctl restart facecloud-web`.

## Semantic search (search by what is in the photograph) on the VPS
Turned on on 2026-11-25. What was needed: the model folder `.runtime/models/siglip-vit-b-16` (copied from a machine that has it; the weights' SHA-256 `81942ea8c09b9e41963357cca5d2682118bf7eb7491c2ae51a28bc4f4f95b194` must match `docs/model-licenses/semantic-search-model.md`), the libraries of `requirements-semantic.txt` (install them without the `timm` pin: the server has timm 1.0.31 that the other models use, and the dry run showed torch and torchvision would not change), and `SEMANTIC_SEARCH_ENABLED=True`, `SEMANTIC_SEARCH_MODEL_SHA256`, `SEMANTIC_SEARCH_MIN_SCORE=0.06`, `HF_HUB_OFFLINE=1` in `/etc/facecloud/facecloud.env`. Then `check --deploy`, restart web, worker and beat, and `python manage.py index_semantic_search` once (15 photographs were indexed; the beat task indexes new ones). Rollback: copy the saved env file back and restart.
Where it shows: the **Search page** (`/dashboard/search?q=...`, which calls `/api/search/photos/`). The small search box in the header calls `/api/search/` and only matches photograph names, people, albums and folders. Search covers only the signed-in person's own indexed photographs. The first query after a restart loads the model in each gunicorn worker and takes several seconds.

## Measured on this server (2 vCPU, CPU torch), 2026-11-25
`enhancement.process_image` over 16 photographs in the worker log: median about 28 s, most between 20 and 67 s, fastest 8.7 s, slowest 128.7 s (two photographs above 125 s; the log does not say which preset). The face, quality, duplicate and thumbnail steps take 0.05 to 0.2 s each; sending an e-mail about 1 s. The worker runs one photograph at a time (`--concurrency=1`), so a bulk upload queues.
