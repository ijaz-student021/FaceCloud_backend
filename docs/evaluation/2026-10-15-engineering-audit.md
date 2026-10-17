# Engineering audit of the adaptive backend (2026-10-15)

Scope: `enhancement/adaptive/`, the upload / job / model / storage / results paths, and the documents that describe them. Method: read the
code, run it with the real models on 16 phone photographs (one phone, mostly one person; synthetic degradations), and repeat any claim
that rested on a number. Status of each finding: **FIXED** (changed and tested in this branch), **MITIGATED** (guarded or reported, not
solved), **OPEN** (needs data, hardware, credentials or a decision that is not available here). Evidence is a file, a script, or a
document that reproduces it; "measured" always means on these photographs on an Apple M1 8 GiB.

## CRITICAL

**C1. The low-light result lost colour, and the earlier colour fix rejected good lifts. FIXED.**
`enhancement/adaptive/operations.py::run_low_light`, `enhancement/services.py::_amplify_source_chroma`, `validator.py::evaluate` (colour gate).
Evidence: raw Retinexformer keeps 0.56-0.81 of the clean photograph's chroma (hue off by 6-10 degrees); the square-root chroma gain
left a further 8-39 % loss and, in the full engine, the lift of the first test photograph was *rejected* so the user received the dark original
(`docs/evaluation/2026-10-15-lowlight-colour.md`). The old colour gate compared absolute CIELAB a/b and could not see pallor.
Consequence: grey-brown clothing, pale skin, or a night photograph returned unchanged. Fix: colours from the photograph in linear light, brightness
from the network (`adaptive/colour.py`), and a lift-specific colour gate (hue shift, saturation ratio). Verification:
`scripts/evaluate_lowlight_colour.py` (calibration and held-out halves), `enhancement/test_adaptive_colour.py`, validator tests; full-engine before/after in the
colour document.

**C2. Commercial licences. MITIGATED, OPEN.**
`docs/model-licenses/commercial-inventory-2026-10.md`. CodeFormer (S-Lab), the ArcFace weights, and Zero-DCE are non-commercial; GFPGAN is used under a permission
that is not in the repository; the RetinaFace weights' terms and every checkpoint's *training-data* terms were never checked. As configured the system is
academic-use only. Consequence: the product cannot be sold as is. Mitigation: `config/production_guard.py::model_rule` refuses to start in production with any of
these enabled unless `NONCOMMERCIAL_MODELS_ACKNOWLEDGED=True` (a decision record, not a licence). Verification: `config/test_production_guard.py`.
Open: the owner must resolve each "Unresolved" row.

**C3. The identity gate cannot see the drift that matters, and its thresholds are uncalibrated. MITIGATED, OPEN.**
`service.py::IdentityChecker.check`, `validator.py::_identity_gate`, `identities/providers.py::thresholds_for`. Evidence (existing benchmark): gate similarity
0.85-0.91 against the *degraded input* while similarity to the true clean face had fallen 0.07-0.13; the 0.70 ArcFace threshold rests on five data points; a face too dark to
embed cannot be verified at all. Consequence: a generative face restoration could pass the gate and still change the person. Mitigation: CodeFormer and GFPGAN stay off
by default, are never accepted without a verified identity, and are now blocked in production unless acknowledged; the API reports `identity_verification.state`
(`verified`, `uncertain`, `not_verifiable`, `not_applicable`) with a notice, and an unavailable comparison is never reported as passed (finding H3).
Open: calibration on a larger labelled, diverse set. `scripts/calibrate_identity.py` is the reproducible procedure (genuine/impostor distributions per degradation,
threshold at a stated false-match rate, thresholds chosen on one half of the *people* and judged on the other); it has been smoke-tested only, because no consented
multi-photo identity dataset is available here.

## HIGH

**H1. Noise measurement was blind to the grain a lift leaves. FIXED (narrowed).** `enhancement/services.py::analyze_image_quality` (3x3 residual median) feeding
`analyzer.py` and the validator's noise gate. Evidence: a result with clearly visible grain read 2.0 before and after. Fix: `analyzer.flat_noise` and `noise.level`
(thresholds are the existing calibrated ones translated by measurement); a lift may take grain up to level 1.0. Still under-reads strongly correlated grain
(measured sigma 9.8 reads 6.0). Verification: analyzer test, `scripts/evaluate_denoising.py` (grain column).

**H2. Blur typing is weaker than the calibration suggested. MEASURED, no threshold change.** `analyzer.py::blur_signals`, `router.py::_consider`. Held-out: defocus sent to the
motion model 10/32, motion missed 16/40 by construction; but the model gains nothing on camera-shake paths (0/32) and does gain on large defocus, and the router captures 90 %
of the model's measured gain (`docs/evaluation/2026-10-15-blur-routing.md`). Consequence: approximate typing, net positive with the validator as second gate. Open: real
camera shake and subject-only motion (a global blur measure cannot see a blurred subject on a sharp background).

**H3. Identity reporting was inaccurate. FIXED.** `service.py` final identity block. Defects: a photograph with no face reported "Biometric processing is not permitted";
an unavailable comparison came with an empty notice; an unchanged photograph claimed `verified`. Fix: `identity_report()` with explicit `state`, scope text and notice.
Verification: `IdentityReportTests`.

**H4. Time budget did not bound a model call. FIXED.** `service.py::enhance`, `runtime.py::tiled_inference`. The budget was checked only between operations; one Retinexformer pass on a
CPU is 56-70 s at 1.2 megapixels (measured, section below), so a large photograph could run to the Celery limit (55 minutes) before being cut off. Fix: a cooperative
deadline checked between tiles (`runtime.deadline_until`), not retried as out-of-memory; the user receives the original with "The enhancement ran out of time".
Verification: `test_runtime` (real tensors) and an engine test.

**H5. Inference could not be isolated from other work. FIXED (opt-in).** `config/celery_routes.py`, `ENHANCEMENT_QUEUE_NAME`. By default every task shared one queue; with the setting the model-loading
tasks go to their own queue, to be served by a separately sized worker pool.

**H6. Previews run model inference inside the HTTP request. MITIGATED by documentation, OPEN.** `enhancement/previews.py::create_preview` calls `apply_gallery_adjustments`,
which loads the model sliders' networks in the API process. This cannot be changed without altering the (frontend-facing) preview contract, which was out of scope. Consequence:
an API worker that serves model-slider previews needs PyTorch and gigabytes of memory. See `docs/deployment/hostinger-readiness-2026-10.md` for how to deploy around it.

**H7. Nothing was validated on real AWS S3, on CUDA, on real smartphone noise, or by people. OPEN.** No credentials, no GPU and no consented real-noise or human-rating data
were available. These are release blockers, not features: see the readiness documents.

## MEDIUM

* **M1. A small face enlarged the whole photograph. FIXED.** `router.py::_consider` (super-resolution). Now off by default (`ADAPTIVE_FACE_UPSCALE_BELOW=0`); only a genuinely small photograph (short edge under 480 px) is enlarged, at x2.
* **M2. `ZERO_DCE_ENABLED` defaults to True** (`config/settings.py`) while its licence is unresolved. MITIGATED: ignored by the default policy; flagged by the production guard under any other.
* **M3. Real-ESRGAN benefit is unmeasured by a no-reference test**; pixel metrics put it 1-3 dB under bicubic and its x2 path computes the native x4 before reducing. OPEN.
* **M4. Candidate preference `improvement - 2 x change` is a heuristic**, not fitted. OPEN; the denoise evidence (two variants, the smaller change often better) supports the design, not the weight.
* **M5. Upload limit is 100 megapixels** (`MAX_IMAGE_PIXELS`) while enhancement caps at 24 MP; decoding a 100 MP upload costs several hundred MB per copy in the API process. OPEN (a decision for the deployment's RAM).
* **M6. Lifts on faces too dark to embed are accepted with identity `uncertain`/`not_verifiable`.** By design (the lift does not synthesise detail), now reported honestly rather than silently.
* **M7. Blur is measured over the whole frame**, so subject-only motion is under-detected (see H2). OPEN.

## LOW

* **L1. Encryption nonces:** a random 64-bit prefix per file plus a 32-bit chunk counter under one key (`config/image_crypto.py`); the birthday bound is about 3e-4 at 10^8 files, negligible at the expected scale but not zero.
  The associated data binds header, chunk index and finality, not the storage path, so a party with bucket write access could swap two users' objects (the application's database ownership still gates access). Fix when needed: a per-file subkey (HKDF) and the path in the associated data (a format version change). OPEN.
* **L2. A failed job returned an empty summary. FIXED.**
* **L3. `manage.py test --parallel 2` crashes the runner with a traceback that cannot be pickled**; the serial run passes. Not investigated further; CI runs serially. OPEN.
* **L4. Earlier documents quoted 1,883 tests; the current figure is in the final report.**

## Verified as already sound (no change)

Authentication and ownership on result, download and edit endpoints (tests: other user cannot see or download); one active job per image enforced by a partial unique constraint
(`enhancement_one_active_per_image`); claim, lease, renewal, takeover of a dead worker's claim, bounded claim waits, retry only for dropped database connections, a worker that lost its
claim publishes nothing, duplicate delivery leaves the running job alone (`test_claim_retry.py`, `test_execution_ownership.py`, `test_pipeline_regressions.py`); `acks_late`, `reject_on_worker_lost`, visibility timeout longer than the
task limit; production guard (secrets, hosts, TLS, eager mode, consent); uploads validated, decompression bombs refused, ICC profiles converted to sRGB, EXIF orientation applied, outputs carry no EXIF; checkpoints loaded with `weights_only=True` and
pinned SHA-256; debug artifacts off by default and now refused in production; no pixels, paths or user data in the new log line.
These are mocked/unit-level facts: no real Redis, no killed worker process, no real storage failure was exercised.
