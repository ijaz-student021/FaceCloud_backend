# FaceCloud backend audit — 2026-10-14

Branch `fix/pipeline-review-followups`, commit `464e5ae`. Audit only: no code,
configuration, checkpoint or database was changed. Companion files in this
folder: `models.md` (model attributes and thresholds), `evidence.md` (commands,
versions, results), `cleanup-proposal.md`, `checks.log`, `queue-rerun.log`,
`pip-audit.txt`.

Evidence labels used throughout: **[static]** code inspection, **[mocked]**
tests with stand-ins, **[classical]** real OpenCV processing, **[CPU]** / **[MPS]**
real checkpoint inference, **[queue]** real Redis + Celery + PostgreSQL.
**No CUDA evidence exists.**

---

## A. Confirmed issues, ranked

P0 means data loss or a security hole in the default deployment. None was found.

### P1

| # | Issue | Trigger → actual behaviour | Impact | Location | Evidence |
|---|---|---|---|---|---|
| 1 | **16-bit grayscale PNGs are destroyed** | Upload a 16-bit grayscale PNG (gradient) → accepted; Pillow opens it as `I;16` and `convert("RGB")` clips instead of scaling → preprocessed and enhanced output have **3 grey levels**. 16-bit RGB and 8-bit files are fine. | Silent destruction of a supported format | `uploads/services.py:196-211`, `enhancement/services.py` original reader | [classical], probe through the real upload → preprocess → enhance path |
| 2 | **A claim-wait retry can take over a live claim** | Same task delivered twice at attempt 0. The duplicate meets the live claim, `self.retry` makes it attempt 1, and after the wait `_is_earlier_attempt_of` (0 < 1) lets it resume the claim **while the original is still running and refreshing it**. The original is then superseded at its next checkpoint and its work discarded. | Double processing on long tasks; the very thing claims exist to prevent | `enhancement/services.py:2612-2655`, `enhancement/tasks.py:144` | [static]. Not hit by the queue test because the original finished inside the wait. |
| 3 | **Task-level failure ignores claim ownership** | Worker A loses its claim to B (lease lapsed), then raises. The service correctly declines to write, but the task's `_fail_if_unfinished` sets the row FAILED and clears B's token; B is superseded and discards a good result. The batch time-limit loop does the same to rows other workers own. | False failures, lost work | `enhancement/tasks.py:56-65`, called at 154-164, 236-240 | [static] |
| 4 | **Blur is measured on the dark, full-resolution source** | Laplacian variance depends on exposure and resolution. Darkening a sharp photo 4x reads `severe_blur`; the same sharp 24 MP photo reads `mild_blur` at native size, but not at 1600 px (score falls 5-14x with resolution). These input defects drive sharpening, the NAFNet gate and GFPGAN eligibility. | Sharp night and high-resolution photos are sharpened, deblurred (when HD is on) and made eligible for generative face restoration | `enhancement/services.py:287-290` (absolute thresholds), `871`, `1371`, `1478` | [classical] measured on 3 real photos at 4 sizes and 4 exposures |

### P2

| # | Issue | Trigger → actual behaviour | Location | Evidence |
|---|---|---|---|---|
| 5 | Embedded colour profiles dropped without conversion | A Display P3 / Adobe RGB photo (common from phones) is decoded as if sRGB; the preprocessed and enhanced files carry no profile → shifted, duller colour | `uploads/services.py` preprocessing, enhancement reader | [classical] ICC-tagged JPEG through the real upload path: "dropped" |
| 6 | Deliberately dark photos are brightened | Low-key "hands" photo lifted to flat grey (SSIM 0.62 to its original); nothing distinguishes intent from underexposure | routing in `_apply_conservative_routing` | [classical] real stock photo, visual review |
| 7 | Broker visibility guard too weak | A delayed message is unacknowledged for countdown + run time (acks_late). The guard requires visibility > max(time limit, lease); safe needs > lease + time limit. Defaults: 4500 < 1800 + 3600 → a claim-wait retry followed by a long run can be redelivered while running. Also off by one: `retry_after` can be lease + 1. | `config/settings.py:742`, `services.py:2655` | [static]; the failure mode itself was observed with short timeouts [queue] |
| 8 | Claim waits consume the database-retry budget | `self.retry` raises `request.retries`; `_retries_remain` compares it with `max_retries=3`. After 3 waits, one transient DB error fails the result. The comment says they are separate. | `enhancement/tasks.py:46-47, 136-144` | [static] |
| 9 | Batch redelivered after a worker crash skips the dead worker's image | Same task id and attempt → `ResultInProgress` → `continue`. The image is never retried; the reaper eventually marks it FAILED (if beat runs); the job finishes PARTIAL/COMPLETED with that image counted nowhere. The single-image task waits and takes over correctly. | `enhancement/tasks.py:206-211` | [static]; the single-image path passed [queue] |
| 10 | Cancelling after the last image finished marks a complete batch CANCELLED | `_close_job_if_cancelled` after the loop does not check whether every result already completed; counters are not finalised | `enhancement/tasks.py:253` | [static] |
| 11 | Face check can undo GFPGAN while results still say "restored" | `face_fidelity` judges against the pre-restoration image after GFPGAN, may blend the face back, but `face_restoration.applied`, the algorithm string and the identity notice still report a restoration | `enhancement/services.py:1752` | [static]; did not trigger at blend 0.25 in [MPS] runs |
| 12 | Batch retry re-sends notifications for finished images | A batch retried after a DB error re-publishes every already-completed result (notification + activity row) | `enhancement/tasks.py:236` | [static] |
| 13 | Loaders re-hash checkpoints on every call | With the model cached, `gfpgan._load_restorer` costs 471 ms per image (hashes ~544 MB), NAFNet 244 ms, colorization 87 ms, Real-ESRGAN 41 ms | `gfpgan.py:245`, `hd_restoration.py:201-204`, peers | [MPS] measured |
| 14 | 19 generic list endpoints have no pagination (4 others set their own) | e.g. `/api/images/` returned 600 rows / 437 KB in one response on 2026-10-13; also admin users, enhancement results, jobs, identity groups, moderation reviews | `uploads/views.py:158`, `enhancement/views.py:210`, `accounts/views.py:870` and 16 more (`evidence.md`) | [static]; earlier [classical] probe |
| 15 | Throttles are per process | No `CACHES` → LocMemCache; every rate limit multiplies by worker count and resets on restart | `config/settings.py` | [static] |
| 16 | EDSR has no runtime integrity check | `_load_edsr_model` checks only that the file exists; the only checksum check is the offline `model_manifest.py verify`. Any file at that path is loaded. | `enhancement/services.py:359` | [static] |
| 17 | PureVision / PureScale run on every photo when enabled | Not gated on compression or resolution defects; PureScale upscales already-large photos (43 s each); PureVision raised clipping on a clean photo (0.06 % → 0.54 %) | `services.py` purevision/purescale stages | [MPS] real photos |

### P3

| # | Issue | Location |
|---|---|---|
| 18 | Cancelled result sends no notification or activity event (progress *is* synced by the post_save signal, so the progress bar is not stuck — the review overstated that) | `enhancement/tasks.py:147, 211` |
| 19 | Plan says deblur is planned whenever NAFNet is on; the stage now deblurs only with measured blur | `enhancement/services.py:971` |
| 20 | Face check "reverted" status even when violations remain at strength 0 (corners outside the ellipse keep candidate pixels) | `enhancement/face_fidelity.py:186-225` |
| 21 | Withdrawn GFPGAN pass leaves the first tail's warnings and stage reports; `no_correction_needed` counts discarded changes | `enhancement/services.py:2163-2185` |
| 22 | Denoise guard tries weaker blends after a noise-benefit failure, which can never pass (up to 4 wasted full-frame analyses) | `enhancement/denoise_guard.py:268` |
| 23 | Two GPU→host syncs per tile (`raw.min()`, `raw.max()`) | `enhancement/runtime.py:153` |
| 24 | SFace model rebuilt per identity-guard call (44 ms); YuNet per call (4 ms) | `identities/providers.py`, `validation/services.py:135` |
| 25 | basicsr 1.4.2: PYSEC-2026-1215 (SLURM-path code execution; no fix released). Inference does not use that path. | worker environment |
| 26 | `import gfpgan` fails outside the adapter (the torchvision shim lives only in `enhancement/gfpgan.py`) | worker environment |
| 27 | Colorization has only a luminance check; nothing catches chroma blotches | `enhancement/colorization.py` |
| 28 | `quality_after` is measured before encoding; the encoded file is verified (PSNR ≈ 47-50 dB) but its own metrics are not recorded | `enhancement/services.py` |

### Suspected, not confirmed

- A single stage longer than the 1800 s claim lease (Real-ESRGAN on CPU with out-of-memory retries) would let a duplicate take over. Plausible on CPU workers; not measured.
- The stuck-job reaper relies on `updated_at`, which claim heartbeats (queryset `update`) do not touch; for live tasks under the 3600 s limit this is harmless, but it has not been exercised.

### Reviewed and not confirmed

- **Cancelled result leaves the progress bar stuck:** progress is synced by the `post_save` signal on the result.

---

## B. Feature-completion matrix

Backend status reconciled against code and current evidence. Frontend status is
from the docs only (the frontend repository was not audited).

| Area | Backend | Evidence | Frontend | Deployment |
|---|---|---|---|---|
| Signup, verification, login, refresh, reset, logout, 2FA | Implemented and verified | Tests [mocked/classical], PostgreSQL run | Not audited | No deployment files |
| Roles, suspension, organizations, tenant isolation | Implemented and verified (tests) | 1407 tests incl. isolation suites | Moderator panel: planned | – |
| Single / multiple / ZIP upload | Implemented and verified; **16-bit gray PNG defect (P1-1)** | Tests; probe | – | – |
| Preprocessing, thumbnails, encryption at rest | Implemented and verified; colour profiles dropped (P2-5) | Tests; probe | – | Encryption off locally (DEBUG) |
| Enhancement pipeline (classical + Zero-DCE + EDSR) | Implemented, verified on CPU/MPS; routing defects P1-4, P2-6 | [classical] [CPU] [MPS] [queue] | Slider wiring remains (docs) | – |
| HD (NAFNet, Real-ESRGAN), PureVision, PureScale | Implemented, **disabled intentionally** (speed, licence, no measured gain) | [MPS] | – | Needs GPU worker |
| GFPGAN face restoration | Implemented and verified [MPS] [queue]; **optional, off** | real checkpoints | – | GFPGAN worker env required |
| Colorization | Implemented; **disabled intentionally** (visible blotches) | [MPS] visual | – | – |
| Private FaceCloud curve model | **Blocked**: file missing; optional, not used by default | manifest | – | – |
| Identity grouping, search, exports | Implemented; insufficiently verified (production thresholds not evaluated — docs) | Tests | Feature 19 frontend: remaining | – |
| Duplicates, quality decisions, moderation queue | Implemented and verified (tests) | Tests | Moderator dashboard: planned | – |
| Model comparison (incl. identity fields) | Implemented and verified [mocked] | Tests | Not audited | – |
| Sharing, downloads, reports, notifications, activity | Implemented and verified (tests) | Tests | Remaining per docs | Email backend: console locally |
| Privacy: consent, erasure, retention, exports | Implemented and verified (tests) | Tests | – | Beat needed for purges |
| Background jobs: claims, retries, cancellation, reaper | Implemented; verified [queue] except reaper; defects P1-2, P1-3, P2-7..10 | [queue] 5/5 | – | No Redis/beat/worker service definitions |
| Admin analytics, system health, audit feed | Implemented and verified (tests) | Tests | Dashboards planned | Health endpoint admin-only |
| Live camera (Features 36-40) | Missing (planned) | docs | – | – |
| Cloud storage (Feature 43) | Partial: encryption done, local disk only | docs | – | Shared disk needed between web and workers |

---

## C. Model audit

See `models.md` for the full attribute table and threshold calibration. Summary:

| Model | Integrity at runtime | Verified inference | Enabled locally | Main gap |
|---|---|---|---|---|
| Zero-DCE (public Epoch99) | Yes (pinned, cached) | [CPU] | On | Brightens intentional low-key photos |
| Private curve model | – | – | – | Missing file; optional |
| EDSR x2 | **No** (existence only) | [CPU] | On | Add a pinned checksum |
| NAFNet | Yes (re-hashed every call) | [MPS], [CPU] parity | Off | Diverges on near-black tiles (now contained); slow |
| Real-ESRGAN | Yes (re-hashed every call) | [MPS] | Off | 60-100 s per 1.7 MP on MPS |
| PureVision / PureScale | Yes | [MPS] | Off | Non-commercial licence; ungated |
| GFPGAN (+ RetinaFace, ParseNet) | Yes (re-hashed every call, 471 ms) | [MPS] [queue] | Off (optional) | Thresholds uncalibrated on real degraded faces |
| Colorization | Yes | [MPS] | Off | Colour blotches |
| YuNet | Yes (cached) | [CPU] | On | – |
| SFace | Yes (cached) | [CPU] | On | Similarity is evidence, not proof |

---

## D. Remaining work, in dependency order

Each item: why · depends on · missing · acceptance · needs.

1. **Fix claim ownership (P1-2, P1-3, P2-8, P2-9).** Why: duplicate or late workers can discard good output or fail rows they don't own. Depends on: nothing. Missing: distinguish "earlier attempt ended" from "this attempt number came from a claim wait"; ownership-checked failure writes; separate wait and DB-retry counters; batch revisit of skipped images. Acceptance: regression tests reproducing each review scenario; queue harness extended with a long-task duplicate and a killed batch worker. Needs: code.
2. **Fix 16-bit grayscale and colour-profile handling (P1-1, P2-5).** Why: silent image damage. Depends on: nothing. Missing: scale 16-bit modes; convert embedded profiles to sRGB (Pillow `ImageCms`) or preserve and tag. Acceptance: a 16-bit gradient keeps ≥ 250 levels; a P3 test image's sRGB values match a reference conversion. Needs: code + test images.
3. **Make blur measurement resolution- and exposure-independent (P1-4).** Why: drives sharpening, deblur and GFPGAN eligibility. Depends on: nothing; affects 4 (routing). Missing: measure on a normalised proxy (fixed size, normalised contrast) and recalibrate thresholds on real photos. Acceptance: the same sharp photo gets the same blur verdict at 0.5-24 MP and at 0.25-1x exposure; documented calibration set. Needs: code + real photo data.
4. **Visibility timeout rule (P2-7).** Why: redelivery while running. Depends on: 1. Acceptance: settings refuse visibility ≤ lease + time limit + margin; default raised accordingly. Needs: code + a deployment decision on the value.
5. **Cancellation and batch reporting (P2-10, P2-12, P3-18).** Depends on: 1. Acceptance: a fully completed batch stays COMPLETED; no duplicate notifications on retry. Needs: code.
6. **Face check vs GFPGAN reporting (P2-11, P3-20, P3-21).** Depends on: nothing. Acceptance: if the face check reverts a restored face, `face_restoration.applied` is false and no restoration notice is emitted. Needs: code.
7. **Runtime integrity for EDSR; cache verified hashes (P2-13, P2-16).** Acceptance: EDSR refuses a non-matching file; warm loader calls under 5 ms. Needs: code.
8. **Pagination and shared cache (P2-14, P2-15).** Needs: code + frontend agreement on page format (API change).
9. **Low-key photo policy (P2-6).** Needs: **user/product decision** (leave intentionally dark photos alone? offer a "keep mood" option?), then code.
10. **Gate PureVision/PureScale on defects (P2-17)** before any enablement. Needs: code; licence decision for commercial use.
11. **Real-photo calibration set.** Why: every threshold in `models.md` was set on synthetic degradations of ≤ 22 photos. Needs: **data** (consented portraits, group photos, range of skin tones, real night/blur/compression), then evaluation.
12. **CUDA validation.** Needs: **hardware**; run `scripts/validate_models.py` and the GPU runbook appendix.
13. **Private curve model.** Needs: **the file** from its owner, or a decision to drop it from the manifest.
14. **Deployment artefacts.** Dockerfile/compose or service definitions for web, worker, GFPGAN worker, beat, Redis; public health endpoint; Redis cache. Needs: code + hosting decision.
15. **Frontend integration** of identity fields, `no_correction_needed`, pagination. Needs: frontend work.
16. **Documentation cleanup** (`cleanup-proposal.md`). Needs: your approval.

---

## E. Readiness

| Target | Verdict | Why |
|---|---|---|
| **Local demo** (this Mac) | **Ready with care** | All checks pass; default stages validated on CPU/MPS. Run `migrate` on the local SQLite first (91 migrations unapplied). Avoid 16-bit grayscale PNGs and deliberately dark photos in the demo; keep optional models off. Eager mode is on, so no worker is needed. |
| **Staging** | **Not yet** | Needs: P1 fixes (1-3) at least, deployment artefacts, a real worker topology (eager is on locally), Redis cache, the visibility rule, and a real-photo evaluation. |
| **Production** | **No** | Additionally: CUDA validation, calibration on consented real data, pagination, colour management, monitoring, and the product decisions in G. |

---

## F. Model enable/disable recommendation

| Model | Recommendation | Evidence |
|---|---|---|
| Zero-DCE, EDSR, YuNet, SFace, identity guard | Keep on | Validated [CPU]; EDSR needs a runtime checksum |
| NAFNet, Real-ESRGAN | Keep off | Slow on MPS, no measured gain on real photos, NAFNet divergence only contained |
| PureVision, PureScale | Keep off | Licence; ungated; marginal effect |
| GFPGAN | Off by default, optional | Works and is honestly reported; generative; blur gate (P1-4) must be fixed first |
| Colorization | Keep off | Visible artefacts on 2 of 2 portraits |

---

## G. Documentation contradictions and open product decisions

**Contradictions**

- `docs/FEATURES.md` lists "private FaceCloud curve inference" as complete. The file is missing and the default configuration uses public Zero-DCE.
- `docs/structured/tasks.md` T-003 still says "Replace or correctly run NAFNet"; ADR-022 records the adapter fix.
- `docs/features/hd-deblur-super-resolution-pipeline.md` still describes NAFNet as disabled for corruption, with no mention of the fix or the near-black divergence.
- The `requirements-gfpgan.txt` header now says Python 3.12; `scripts/start_gfpgan_worker.ps1` and older docs still describe a Python 3.11 worker.
- `docs/structured/agents.md` refers to `AGENTS.md`-style rules but nothing references it, and no `AGENTS.md` exists.
- The 2026-10-13 docs quote memory and timing figures that were later corrected in ADR-014.

**Decisions needed**

1. Should intentionally dark or high-key photos be left alone, and how would the user signal that?
2. Should a recommended model-comparison profile be penalised for unverified restored faces?
3. Is `low` meant to mean "never denoise"? It no longer does.
4. Should the private curve model stay in the manifest?
5. Commercial use of PureScale/PureVision (non-commercial licence).
6. Pagination format for the frontend.

---

## H. Proposed fix sequence (each a separately reviewable commit)

1. Claim ownership: ownership-checked task failures; attempt provenance; separate wait counter; batch revisit.
2. Image decoding: 16-bit modes and ICC conversion, with fixture images.
3. Blur measurement on a normalised proxy, plus recalibration notes.
4. Broker rule (visibility > lease + time limit) and its tests.
5. Batch cancellation and notification idempotency.
6. Face-check / GFPGAN reporting consistency; strength-0 status; withdrawn-pass cleanup.
7. EDSR checksum; cached checkpoint verification; tile min/max in one reduction; denoise early exit.
8. Pagination + Redis cache (with the frontend).
9. Defect gating for PureVision/PureScale.
10. Documentation consolidation (after approval of `cleanup-proposal.md`).
