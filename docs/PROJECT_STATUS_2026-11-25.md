# FaceCloud AI: project status on 2026-11-25 (defence week)

An honest statement of what is complete, what was verified on the live system, and what is still open. "Verified live" means somebody ran it on `www.facecloudai.com` / `api.facecloudai.com`; "tested" means an automated test covers it (4150 tests, CI green on PostgreSQL).

## 1. Complete and verified live
| Area | Evidence |
|---|---|
| Backend deployed on a VPS behind HTTPS, PostgreSQL, Redis, Celery worker and beat | services active, certificate valid, smoke tests, `docs/deployment/VPS_DEPLOYED_STATE.md` |
| Frontend deployed on facecloudai.com, connected to the API | login and dashboard seen on the live site |
| Auto enhancement (local models: Zero-DCE, NAFNet, Real-ESRGAN, CodeFormer, DDColor, OpenCV steps) | results produced on the VPS (CPU, 1 to 4 minutes per photograph) |
| Pro enhancement (Deep Image cloud AI) with face checks (landmark geometry, SFace identity, structure) | `cloud used=True`, report text "The faces were checked against your original" |
| E-mail through the Hostinger mailbox | a test message from the server arrived |

## 2. Complete in code and tests, not yet exercised on the live system
Registration with the verification e-mail, organizations and the moderator flow (invitation, review queue, approve and reject), the "you are now a moderator" and "account ready" e-mails, the contact-form inbox, ZIP and HEIC uploads, PDF reports, person folders, trash and albums, notifications. The automated tests cover them; a single run on the live site is still the proof.

## 3. Changes merged on 2026-11-25 (main `8551572`)
* Photographs that a moderator, an administrator or the checks rejected leave All, Accepted and In review and appear under Rejected only; they are not enhanced; the decision can be undone (`docs/ORGANIZATIONS_AND_REVIEW_PROCESS.md`, section 6). Needs `migrate` (quality_control 0002) and `python manage.py backfill_person_rejections --apply` once after deployment.
* E-mails when someone becomes a moderator or an account is created; e-mail copies of notifications are on by default (migration notifications 0010). No e-mail ever contains a password.
* The development server's connection queue (gallery thumbnails no longer fail with "Broken pipe").
* The CI test fixtures for the OpenAI advisor follow the newer SDK; every pull request was failing on them.
* The generated API documentation (`docs/api`) was regenerated: 272 operations.

## 4. Known limits (to state plainly)
* **No model was fine-tuned.** All neural models are public pretrained weights, evaluated in `model_evaluation/`; one small Zero-DCE fine-tuning experiment (35 photographs) lowered its training loss but did not beat the official weights, so it is not installed.
* **CPU server:** heavy models are slow (Auto about 1 minute, Pro 1.5 to 4 minutes); a large ZIP takes long. There is no GPU.
* **Semantic photo search is off on the VPS** (it needs about 1 GB more and is heavy on a CPU); it runs on the development machine.
* **The Pro result depends on the provider:** its API key and credits, and our safety gates (a result that changes the picture's structure too much, or a face, is refused and the local result is kept). The preset `auto_enhance` enlarges the photograph to 4096 px, `auto_enhance_pro` keeps its size.
* The mailbox plan (Hostinger Business Email) is paid until 2026-12-24; when it lapses the server cannot send e-mail.
* One VPS: no redundancy or monitoring.

## 5. Open operational items
Daily database backup (`VPS_RUNBOOK.md`, section 10) installed? Root SSH login closed? Server environment file and keys stored in a password manager? Deploy the merged `main` with `scripts/deploy_vps.sh`.
