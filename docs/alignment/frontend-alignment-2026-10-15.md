# Backend against the frontend, 2026-10-15

Frontend: `ali4347/facecloud-ai-platform` at `622f2a4` (read-only). Backend: branch
`feat/lightweight-enhancement`. This supersedes the baseline in
`backend-alignment-tracker.md` (taken against frontend `ee7d126`); the per-screen
mapping there still holds, because the frontend's API layer has not changed since.

## Verdict

**The backend already provides everything the frontend asks of it; most of what is left
is on the frontend side.** The frontend's own work log lists the integration as "in progress": sign-in, the user upload-to-images chain and
dashboard, and the moderator dashboard and queue read the live API; roughly 29 other
screens still read `src/data/mock.ts`.
The backend work that remained was small and is done in this change (section 1).

## 1. Backend gaps found and closed in this change

| Finding | Source | Resolution |
|---|---|---|
| A moderator could promote a member to moderator through `PATCH /api/organizations/{id}/members/{id}/` (invitations refused it) | Frontend team, found while writing the moderator role guide (`WORK_LOG.md` §7) | Fixed in `organizations/services.py::update_member` with the same rule as `invite_member`; test written first and shown failing; owners unaffected |
| CI job "Production configuration starts" failed on PR #68 and #71: the guard requires `CACHE_URL` (shared cache for rate limits) and the workflow never set one | CI logs | `CACHE_URL` added to the job's throwaway environment; the same check reproduced locally and passes |
| CI job "Dependency audit and static analysis" failed: bandit medium findings (B608, B108) in the Redis/Celery harness scripts | CI logs | The SQL is now parameterised through a psql variable (tested with a hostile name); the one remaining `/tmp` default is the PostgreSQL socket directory, annotated |
| 45 pyflakes findings (36 unused imports, 6 unused variables, ...) and no lint in CI | `ruff` | All fixed; `pyproject.toml` and a CI lint step added so it stays at zero |

## 2. Does the frontend's API layer match the backend? Yes

Every path the frontend's client calls (28) exists in the backend's OpenAPI schema
(193 paths, 232 operations, 57 models), and every field its typed responses name is in
the schema. Mismatches exist only in prose (below).

## 3. What the frontend still has to do (no backend work needed)

The endpoint for each screen is in `backend-alignment-tracker.md`; all exist.

| Portal | Already on the live API | Still on mock data |
|---|---|---|
| Authentication | Login, register, verify email, password reset | Reset screens (routes missing). 2FA was removed from the backend on 2026-10-16 |
| User | Dashboard, single upload, processing, image list and detail | Multiple and ZIP upload, groups and group detail, downloads, activity, notifications, jobs, settings (profile and security), live enhancement |
| Moderator | Dashboard, review queue (claim, bulk decisions, thumbnails, performance) | Image review, members, batches, notifications, reports, audit, settings |
| Admin | none | All 13 screens (users, audit, security, API docs/test, system, statistics, image monitoring, rejections) |

New backend capabilities the frontend can adopt when it wants them (contracts in
`frontend-handoff.md`): editor sliders including sharpness and warmth with bounded
previews and saved versions; the optional advisor; per-model health reporting (installed, verified, disabled).

**Dashboard sliders:** the five sliders of the live-enhancement panel currently apply browser filters only and send nothing to the
backend. The backend contract now covers all five (a `denoise` control was added for Noise Reduction); the exact mapping is in
`docs/enhancement/dashboard-sliders.md`.

## 4. Where the frontend's words no longer match the backend

These are copy and documentation, not code; they matter because a reviewer will read
both repositories.

* **Models.** The frontend names Zero-DCE, GFPGAN, Real-ESRGAN, NAFNet, PureVision, PureScale, colourisation,
  InsightFace and MediaPipe in 30 mock entries, the landing page, the live-enhancement panel, About, the footer and the
  README. The backend's set is now: Retinexformer and Zero-DCE (low light), NAFNet-SIDD (denoise), NAFNet-GoPro (deblur),
  CodeFormer and GFPGAN (faces), Real-ESRGAN (super-resolution), YuNet or RetinaFace (detection) and SFace or ArcFace
  (identity; ArcFace is InsightFace's model). **PureVision, PureScale and colourisation were removed** and should leave the
  frontend copy; MediaPipe is not used. All are installed but **off by default** (`docs/enhancement/optional-models.md`),
  so the copy should say "optional, disabled by default" rather than imply they run on every photo.
* **`README_BACKEND_ROADMAP.md`** describes a NestJS, Prisma, BullMQ and Python-worker
  architecture that was never built; the backend is Django, DRF, Celery and Redis.
* **`WORK_LOG.md` §1/§5** still say the queue and workers are "not started", the
  frontend "talks to nothing" and the backend has "180 endpoints, 43 models, 1172
  tests". Now: 193 paths, 57 models, about 1,620 tests.
* **Plans.** The landing page prices Free / Basic / Pro; the backend has Free / Team /
  Enterprise labels; neither is enforced anywhere (the frontend notes this too). Decide
  one vocabulary, or remove the pricing claims.
* **Moderator guide.** One row was deliberately narrowed because of the promotion gap;
  with the backend fix it can now say a moderator cannot appoint moderators.

## 5. Making the backend professional: state and what is left

Done: CI jobs repaired and a lint gate added; typed response schemas with a
zero-warning OpenAPI gate; migration check; production-configuration guard tested;
dependency audit and bandit in CI; models kept installed, checksum-verified and visibly disabled by policy; protected downloads, ownership checks and audit trails.

Left, in order of value for a review:

1. **Merge order.** #67, then #68, then #71 (each stacked on the previous). Back up the
   database first: migrations `0009` and `0010` have only run on disposable databases.
2. **CI green on #71** after this change (re-check; the two failures were not caused by it).
3. **Repository files:** `LICENSE` (your decision which), `SECURITY.md`, a
   `CHANGELOG.md` or release notes. None exists.
4. **README accuracy:** counts and the model section were updated; re-read it once end
   to end before submission.
5. **Real OpenAI validation of the advisor** (optional; needs a key, a model name,
   photos you may send and a cost cap). Until then it is documented as not validated.
6. **Docker staging stack** has never been built.
7. **Dependabot PRs** (#63, #65, #69, #64, #70): torch/torchvision/onnx concern files
   that are gone or training-only; the others are routine.
8. **Operational monitoring** for host CPU/GPU telemetry (the frontend's System page)
   is deliberately not a Django concern; it needs a metrics stack.
9. **Data model decisions only you can make:** whether plan limits (storage, image
   counts) should be enforced, and whether the private curve-model training material
   stays in the repository.
