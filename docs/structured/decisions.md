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

- **Status:** Accepted
- **Decision:** Real-ESRGAN is implemented behind `HD_RESTORATION_ENABLED` with
  tiling, output limits, and conservative source-grid blending.
- **Consequence:** It remains disabled by default and should run on a GPU queue.

## ADR-010 — NAFNet remains disabled after failed visual QA

- **Status:** Accepted
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
- **Consequence:** A redelivered task waits for the lease instead of running
  immediately, so recovery from a dead worker takes up to the lease (30 minutes
  by default) rather than the stuck-job reaper's 65.

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

