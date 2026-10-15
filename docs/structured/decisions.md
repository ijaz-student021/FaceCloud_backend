# FaceCloud Architecture Decision Log

This is a concise decision log. New decisions should include date, status,
context, decision, and consequences. Superseded decisions remain for history.

## ADR-001 — Preserve immutable originals

- **Status:** Accepted
- **Decision:** Every preprocessing, enhancement, edit, and restoration result is
  stored as a derivative. Original uploads are never overwritten.
- **Reason:** Recovery, auditability, recognition integrity, and user trust.

## ADR-002 — Use PostgreSQL as the authoritative data platform

- **Status:** Accepted
- **Decision:** PostgreSQL stores accounts, processing state, decisions, groups,
  memberships, reports, notifications, and audit metadata.
- **Consequence:** SQLite is not the production target; migrations and constraints
  must be PostgreSQL-compatible.

## ADR-003 — Use Redis and Celery for background processing

- **Status:** Accepted
- **Decision:** Preprocessing and inference execute outside web requests.
- **Consequence:** Tasks require idempotency, retry limits, progress reporting, and
  queue-unavailable failure handling.

## ADR-004 — Separate storage bytes from database metadata

- **Status:** Accepted
- **Decision:** Django storage holds originals and derivatives; PostgreSQL holds
  ownership, paths, hashes, and state.
- **Consequence:** Downloads must pass application authorization and deletion must
  clean both records and stored objects.

## ADR-005 — Conservative enhancement is the default

- **Status:** Accepted
- **Decision:** Default enhancement is bounded and identity-preserving. Models are
  optional stages protected by measurable output guards.
- **Consequence:** Dramatic generative reconstruction requires a separately labelled
  mode and must not silently replace natural enhancement.

## ADR-006 — YuNet and SFace provide the recognition baseline

- **Status:** Accepted
- **Decision:** YuNet detects faces and landmarks; SFace produces embeddings.
- **Consequence:** Matching is owner/tenant-scoped and thresholds require evaluation
  on a consented dataset before production automation.

## ADR-007 — Recognition never consumes generative restoration

- **Status:** Accepted
- **Decision:** Embeddings are generated only from originals or conservative
  standardized derivatives.
- **Consequence:** AI-restored outputs are display/download artifacts with explicit
  provenance and `recognition_eligible=false` semantics.

## ADR-008 — Model artifacts are local, private, and checksum-pinned

- **Status:** Accepted
- **Decision:** Workers never download models during inference. Exact model files
  are configured by path and SHA-256 and remain outside Git.
- **Consequence:** Deployment must provision artifacts and license evidence before
  enabling a model.

## ADR-009 — Real-ESRGAN HD restoration is rollout-gated

- **Status:** Accepted — **superseded by ADR-024**
- **Decision:** Real-ESRGAN is implemented behind `HD_RESTORATION_ENABLED` with
  tiling, output limits, and conservative source-grid blending.
- **Consequence:** It remains disabled by default and should run on a GPU queue.

## ADR-010 — NAFNet remains disabled after failed visual QA

- **Status:** Accepted — **superseded by ADR-024**
- **Decision:** `NAFNET_DEBLUR_ENABLED` defaults to false because the supplied
  checkpoint/runtime produced checkerboard and chroma corruption.
- **Consequence:** Structural checkpoint loading is insufficient; a replacement or
  official compatible runtime must pass sample and identity-preservation QA.

## ADR-011 — Generative restoration is a separate future product mode

- **Status:** Proposed
- **Decision:** Whole-scene diffusion restoration, if approved, will use an isolated
  GPU workflow, explicit consent and labelling, separate derivatives, and no
  recognition eligibility.
- **Consequence:** Model and checkpoint commercial licenses must be reviewed before
  implementation or deployment.

## ADR-012 — Track remaining work by catalogue feature number

- **Status:** Accepted
- **Decision:** The 47-feature catalogue is authoritative. Requirements and tasks
  reference its numbers and distinguish backend, frontend, model-QA and operational
  completion.
- **Consequence:** A feature is not complete merely because an endpoint or adapter
  exists; every stated acceptance layer must be reconciled first.

## ADR-013 — Restoration is judged against a pre-restoration reference

- **Status:** Accepted
- **Decision:** The final output guard compares the finished image with the last
  image accepted before any restoration model ran, resized (bicubic) only for the
  comparison. An upscaled or restored image is never its own reference. At most
  one upscaler runs per image: PureScale is skipped when EDSR or Real-ESRGAN has
  already changed the pixel grid. A NAFNet deblur that passed its own guard is
  kept when the following upscale fails.
- **Consequence:** A restoration model that shifts brightness, contrast, clipping
  or colour is blended back toward the reference instead of passing unnoticed.
  `quality_after` is measured on the saved pixels, after colorization, style and
  the identity guard.

## ADR-014 — Enhancement works within a pixel budget

- **Status:** Accepted
- **Decision:** Sources larger than `ENHANCEMENT_MAX_INPUT_PIXELS` (default 24 MP)
  are reduced to that budget before enhancement. The stored original is never
  changed, and the result records `original_reduced_to_pixel_budget` with both
  sizes and a warning.
- **Consequence:** This narrows `ENHANCEMENT_PRESERVE_ORIGINAL_RESOLUTION`: the
  original grid is preserved up to the budget, not up to the 100 MP upload limit.
  Measured on the CPU path (synthetic noisy photo, Apple Silicon, peak process
  memory): 12 MP 6 s / 1.3 GB, 24 MP 15 s / 1.8 GB, 48 MP 50 s / 2.1 GB.

## ADR-015 — Every stage proposes a candidate; guards are mandatory

- **Status:** Accepted
- **Decision:** One runner executes every pipeline stage. A stage that changes
  pixels is optional: if it raises (including out of memory), the image it was
  given is kept, the failure is logged and reported, and the pipeline continues.
  A stage that measures or guards is required: if it cannot run, the enhancement
  fails. `stage_reports` records status, model, device, tile, duration and
  fallback reason per stage; `plan` records what the analysis intended.
- **Consequence:** A model failure costs that model's contribution, not the
  image. A guard failure costs the image, because an unchecked result is worse
  than none.

## ADR-016 — Generative face restoration requires a verifiable identity

- **Status:** Accepted
- **Decision:** With `GFPGAN_REQUIRE_IDENTITY_CHECK` (default on), GFPGAN runs in
  the Natural and Dynamic styles only when the source has a face that can be
  embedded (consent, identity model, reliable detection), and its result is
  withdrawn if the final identity check is not `verified`. AI Max is exempt.
  Identity is recorded as `verified`, `inconclusive`, `unavailable` or `failed`.
  The bypass is reported, not implied: `identity_verification` records
  `requirement_bypass` (`ai_max_style` or `operator_setting`) and
  `face_restoration_unverified`, and the result API exposes `identity_status`,
  `identity_check_bypassed` and `identity_notice`.
- **Consequence:** A deployment that enables GFPGAN without the identity model
  gets no face restoration outside AI Max. Embedding similarity shows a face
  stayed recognisably the same; it does not show restored detail is true detail.

## ADR-017 — Faces are checked separately from the frame

- **Status:** Accepted
- **Decision:** After the whole-image guard, each detected face is compared with
  the image before restoration and sharpening (structure, fine-texture energy,
  colour, highlights) and blended back toward it when outside
  `FACE_FIDELITY_*`. Only detection is used; no embedding is computed and no face
  coordinates are stored.
- **Consequence:** It can only reduce enhancement on a face. Its thresholds are
  set from synthetic cases and one real portrait; they need calibrating on real
  restoration-model output before being relied on as a quality bar.

## ADR-018 — One task owns a result at a time

- **Status:** Accepted
- **Decision:** `ImageEnhancementResult` carries a claim token and timestamp.
  A task claims the row before enhancing, refreshes the claim at each stage, and
  publishes only while it still holds the claim, under the row lock. A claim not
  refreshed for `ENHANCEMENT_CLAIM_LEASE_SECONDS` may be taken over.
  Cancellation is checked at the same points and before any file is written.
  The claim also records the task id and attempt number: a later attempt of the
  same task resumes its claim at once (Celery starts attempt N+1 only after
  attempt N raised), while a second delivery of the same attempt does not. A
  task that meets another task's live claim stays scheduled, re-checking when
  that claim would lapse, up to two waits beyond its database retries.
- **Consequence:** A redelivered task waits for the lease instead of running
  immediately, so recovery from a dead worker takes up to the lease (30 minutes
  by default) rather than the stuck-job reaper's 65. A database failure
  mid-enhancement is retried without that wait.

## ADR-019 — Denoise strength follows measured noise, calibrated on ground truth

- **Status:** Accepted
- **Decision:** The strength ladder is `0,3,4,7,10,13,16` for noise scores split
  at `1,2,3.5,5,7,9`, and the automatic level denoises from score 5 even when the
  exposure level is LOW. Edge protection uses an absolute floor so edge-free
  regions are fully denoised.
- **Consequence:** Calibrated with synthetic Gaussian noise on four photographs
  (mean SSIM to the clean original at score 6: 0.81 with the old ladder, 0.84-0.85
  now). Real sensor noise is signal-dependent and colour-correlated; the ladder
  should be re-checked on real night photographs. A deployment whose `.env`
  still lists the old `ADAPTIVE_DENOISE_*` values keeps the old ladder.

## ADR-020 — A correction needs a measured defect

- **Status:** Accepted
- **Decision:** On the automatic level with the Natural style, exposure is
  changed only when a lighting defect was measured and sharpening is applied
  only when blur was measured; a photo with neither is returned unchanged
  (`no_correction_needed`). A photo with JPEG blocking is never sharpened or
  given local contrast, at any level or style. (The grain-versus-texture rule
  first added here was replaced by ADR-021.) `plan.routing` lists each
  decision with its reason. An explicit level or style is honoured as before.
- **Evidence:** Real CPU processing, no learned model. Thresholds were set on
  four photographs; the blockiness rule was then checked unchanged on eighteen
  others. Across JPEG qualities 10-90 on those eighteen, mean SSIM change
  relative to the input went from about -0.010 to 0.000 for well-lit photos
  (all untouched) and from about -0.035 to -0.004 for compressed photos with a
  lighting defect. The grain-versus-texture threshold was chosen with all
  twenty-two images in view, so it has no held-out check.
- **Consequence:** Nothing on this path *removes* compression artifacts; it only
  stops amplifying them. A photo resized after compression has no 8-pixel grid
  and is not recognised. Real sensor noise is more correlated than the synthetic
  grain used, so the automatic level may leave some real grain alone.


## ADR-021 — Denoising is decided by measured noise and judged against a pre-denoise reference

- **Status:** Accepted
- **Decision:** Whether to denoise no longer depends on the exposure level: a
  photo is eligible when noise is measured in its flat regions, at any level,
  and not otherwise. The denoised candidate is compared with the image
  immediately before denoising and kept only if it removed at least
  `DENOISE_MIN_NOISE_REMOVED` grey levels of noise, that was either a real share
  of what was there or noise-like in character, and it kept
  `DENOISE_MIN_DETAIL_RETENTION` of the structured detail and
  `DENOISE_MIN_EDGE_RETENTION` of the edges, in the frame and in each face. A
  candidate that fails on detail is tried at lower strengths; one that then
  removes too little noise is rejected. Sharpening is not consulted and is not
  changed by the outcome. `adaptive_denoise` records eligibility, benefit,
  detail, acceptance and duration.
- **Evidence (real CPU processing, synthetic Gaussian grain, no learned model):**
  22 photographs, clean and with four grain levels, at three levels, 330 runs
  against the previous commit: 90 closer to the clean original, 39 further, sum
  of SSIM 273.3 to 283.2. Clean photographs denoised: 27 of 66 before, 4 after.
  The noise-removed and noise-share limits were fixed on four photographs and
  applied unchanged to eighteen; the residual-whiteness limit was chosen with
  all in view. The pipeline's denoiser kept 1.00 of the structured detail in
  every case, so the detail limits were verified only against a deliberately
  destructive candidate (a Gaussian blur kept at most 0.82).
- **Consequence:** An explicit LOW level now removes measured grain; it used to
  mean "never denoise". Known losses: heavily grained, heavily textured
  photographs are left alone where the old code denoised them (the largest was
  0.084 SSIM), and photographs with no flat region cannot be measured well.
  Fine-band texture in flat regions (skin pores) cannot be told from grain by
  any measure here. Nothing was calibrated on real sensor noise.

## ADR-022 — NAFNet's failed QA was an adapter bug, now fixed; it stays off pending visual approval

- **Status:** Accepted (updates ADR-010) — **superseded by ADR-024**
- **Decision:** The corruption recorded in ADR-010 came from this project's
  re-implementation of the network, not from the checkpoint or the device: the
  simplified channel attention passed its pooled summary on in place of the
  features instead of multiplying them by it. With that corrected, the pinned
  checkpoint's mean colour shift on a motion-blurred portrait fell from 66.9 to
  0.26 (limit 8), identically on CPU and on Apple MPS, and its output moved 3 to
  7 dB closer to the sharp original. `NAFNET_DEBLUR_ENABLED` still defaults to
  false.
- **Consequence:** NAFNet now runs and passes its fidelity guard. That is not a
  visual sign-off: it was checked on synthetic motion blur of one portrait.
  Enable it only after it has been looked at on real blurred photographs.

## ADR-023 — The worker environment is the backend's requirements plus the model stack

- **Status:** Accepted — **superseded by ADR-024**
- **Decision:** `requirements-gfpgan.txt` includes `requirements.txt` instead of
  repeating it. BasicSR 1.4.2 imports a module torchvision removed in 0.17;
  `enhancement/gfpgan.py` supplies the one missing name at import time, because
  Django 6 needs Python 3.12 and no older torchvision exists for it.
- **Consequence:** The worker can no longer drift from the application (it had:
  without `cryptography` it could not start Django). The shim touches only a
  module that is absent, and is removed when BasicSR is replaced.

## ADR-024 — The learned enhancement models are retired

- **Status:** Accepted (supersedes ADR-009, ADR-010, ADR-022 and ADR-023; the
  parts of other ADRs that describe Zero-DCE, EDSR, GFPGAN, PureVision, PureScale
  or colourisation are history)
- **Decision:** Enhancement is OpenCV and Pillow only. Zero-DCE, EDSR, NAFNet,
  Real-ESRGAN, GFPGAN, PureVision, PureScale and colourisation were removed:
  adapters, checkpoints, settings, the isolated model-worker environment, GPU
  scripts and their documentation. Automatic mode no longer lifts dark photos (their
  intent cannot be read from the pixels); a chosen level or the editor's
  `brighten` intent does. A request that names a retired capability (the
  `opencv_baseline` comparison profile) is refused with the reason, never run as
  another algorithm; earlier results, edits, downloads and comparisons are
  unchanged.
- **Why:** none of them was enabled by default except Zero-DCE and EDSR, three had
  non-commercial licences or private-permission constraints, generative face
  restoration cannot be shown to keep the person, none was validated on CUDA, and
  together they cost about 1.1 GB of checkpoints, a 1.3 GB second Python environment
  and a second queue.
  YuNet and SFace, which the identity features need, were kept; PyTorch is not
  needed by anything retained.
- **Consequence:** very dark, blurred or low-resolution photos are improved less
  (or reported as beyond the pipeline) than the learned models improved them. The
  recovery path is `docs/enhancement/retired-capabilities.md`.
