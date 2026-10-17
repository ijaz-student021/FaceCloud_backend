# Cleanup proposal — 2026-10-14 (nothing removed yet)

Nothing has been deleted. After you approve, this would be one separate commit:
consolidate, update links, run the checks again. Ignored and untracked files are
first copied to a dated archive (`~/FaceCloud_archive/2026-10-14/`, outside the
repo) before removal. "Refs" counts mentions in tracked files, excluding the
file itself.

## Tracked documents

| File | Purpose | Evidence | Refs | Proposed |
|---|---|---|---|---|
| `docs/12_DAY_COMPLETION_PLAN.md` | September schedule | Dates passed; superseded by tasks/ADRs | 1 | Archive (`docs/archive/`) |
| `docs/USER_DASHBOARD_REMAINING_FEATURES.md` | Dashboard gap list, 09-17 | Partly outdated | 1 | Consolidate into `docs/FEATURES.md`, then archive |
| `docs/PRD_6_DIGIT_SIGNUP_EMAIL_VERIFICATION.md` | PRD for an implemented feature (661 lines) | Implemented and tested | 1 | Archive |
| `docs/SECURITY_AND_PERFORMANCE_REVIEW.md` | 09-19 review | Superseded by this audit | 1 | Archive, link from this audit |
| `docs/MANUAL_TESTING_COMMANDS.md` | Manual test commands | Still referenced | 2 | Keep; refresh commands |
| `docs/structured/tasks.md` | Task list | T-003 stale (G) | – | Keep; update T-003 |
| `docs/structured/agents.md` | Agent rules | Unreferenced | 0 | Consolidate into README or delete (your call) |
| `docs/structured/prd.md`, `architecture.md` | Overview docs | Referenced once | 1 | Keep |
| `docs/features/24-identity-group-zip-manual-testing.md` | Manual test notes | Referenced | 1 | Keep |
| `docs/features/18-yunet-sface-implementation.md` | Detector notes | Unreferenced, content still accurate | 0 | Keep, add link from FEATURES.md |
| `docs/features/47-private-model-training.md` | Private model training | Unreferenced; the model is missing | 0 | Keep (provenance of the missing model) |
| `docs/features/hd-deblur-super-resolution-pipeline.md` | HD design | Contradicts ADR-022 | – | Keep; update |
| `docs/FEATURES.md` | Feature status | Overclaims private model (G) | – | Keep; correct |

## Tracked scripts

| File | Purpose | Refs | Proposed |
|---|---|---|---|
| `scripts/evaluate_enhancement_samples.py` | Old sample evaluator, reads `.runtime/dataset_review` | 0 | Archive; superseded by `evaluate_pipeline_cases.py` |
| `scripts/measure_upscaler_memory.py` | Produced ADR-014 memory figures | 0 | Keep; reference from ADR-014 |
| `scripts/audit_image_dataset_zip.py` | Dataset ZIP checker | 0 | Keep if the training dataset is still used, otherwise archive (your call) |
| `scripts/evaluate_gpu_stages.py`, `evaluate_reference_quality.py` | GPU and reference evaluation | 3 each, one has tests | Keep |
| `*.ps1` launchers, notebooks, `loadtest/`, `requirements-*.txt` | Run/deploy aids | Referenced | Keep |

## Ignored / untracked

| Path | Size / purpose | Proposed |
|---|---|---|
| `.venv-training/` | 12 MB, unused training venv | Archive, then remove |
| `scripts/__pycache__/`, `.ruff_cache/` | Caches | Remove (regenerated) |
| `db.sqlite3` | Local dev DB, 91 migrations behind | **Keep** (may hold your data); back up before any migrate |
| `.runtime/models`, `gfpgan/weights`, `provenance/` | Checkpoints and provenance | **Keep** |
| `.runtime/model_validation`, `visual_eval`, `queue_integration` | Evidence | **Keep** |
| `.runtime/venvs/` | Worker env | **Keep** |
| `audit-output/` | This audit | Keep; decide whether to commit it (git currently ignores `.log` files in it) |
| `.env` | Local secrets/config | **Keep**, never commit |
