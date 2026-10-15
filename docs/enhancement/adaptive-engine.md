# The adaptive enhancement engine

One photograph in, **one** final photograph out. The engine analyses the photograph, runs only the operations its defects call
for, judges every candidate against hard gates (quality, colour, artifacts, identity), and returns the best acceptable image
or, when nothing safe exists, the original. It is selected with `ENHANCEMENT_RENDER_POLICY=adaptive_v1`; the staged pipeline
(`lightweight_v1`, the default) is unchanged. Code: `enhancement/adaptive/`.

## 1. Audit of the existing code (before this work)

| Question | Finding |
|---|---|
| Django apps involved | `uploads` (ingest, standardise), `validation` (face detection), `enhancement` (pipeline, jobs, API), `identities` (embeddings), `privacy` (consent), `operations` (health), `config/storage*` (encrypted storage) |
| Existing enhancement API | `POST /api/enhancements/images/{id}/` queues a job (202), `GET /api/enhancements/results/{id}/` polls, `GET .../download/` returns the file, with ownership checks. Batches, previews, saved edits and the advisor sit beside it. **It already is the "Auto Enhance" endpoint**; no duplicate was added |
| Is inference real? | Yes, verified: every adapter loads a checksum-pinned checkpoint and ran on real photographs (Retinexformer, NAFNet-SIDD/GoPro, Real-ESRGAN, CodeFormer, GFPGAN on MPS). The `algorithm` field names only what ran; an OpenCV fallback reports itself as `opencv_adaptive_restoration_router_v2`, never as AI |
| Silent fallbacks to basic filters? | None found. A model that cannot run is recorded (`fallback_reason`, `stage_reports`). But under the default policy **no learned model runs at all**, which is by design and was not obvious to a user |
| Quality analysis | `analyze_image_quality` used one brightness threshold (a normal night portrait was `low_light`), one blur verdict, no blur *type*, no per-face quality. Replaced for this engine by `ImageQualityAnalyzer` |
| Candidate handling | The staged pipeline proposes one candidate per stage and keeps or reverts it. There was **no comparison of alternatives** and no overall outcome label |
| Face detection | YuNet (default), Haar; RetinaFace existed only inside the GFPGAN worker environment. Now vendored, so it runs in the main environment |
| Identity | SFace only. ArcFace added as a selectable provider with its own thresholds |
| Duplicate / conflicting code | `EDSR`, `PureVision`, `PureScale`, colourisation removed (retired-capabilities.md); the GFPGAN and CodeFormer face paths now share one gate |
| Device selection | `runtime.resolve_device`: CUDA, then MPS, then CPU; an explicitly configured missing device is an error |
| Tests | 1,800 before; adaptive engine tests added (see section 9) |

Reused unchanged: the job/claim/retry system, uploads and storage, the identity guard internals, the denoise guard, the
model adapters and their tiling and out-of-memory handling.

## 2. The flow

```
original ──> ImageQualityAnalyzer ──> AdaptiveEnhancementRouter ──> candidates for ONE operation (1-2 variants)
   ^                                        ^                                     │
   │                                        └── re-analyse the selected image ────┤
   │                                                                              v
   └──────────── nothing acceptable? the output IS the original ◄── FinalImageQualityValidator (gates, then preference)
```

* The **router** is asked again after every accepted operation (a lift reveals noise; a denoise changes how blur looks), so
  there is no fixed pipeline: a clean photograph runs nothing, a dark noisy one runs two operations.
* **Operations and engines** (first available is used; a failed or rejected engine falls back to the next, at most two per
  operation): low light `retinexformer -> zero_dce -> opencv_exposure`; denoise `nafnet_sidd -> opencv_nlm`; deblur
  `nafnet_gopro` (no classical fallback); face `codeformer -> gfpgan`; upscale `realesrgan`.
* **Variants**: a full-strength candidate and a conservative blend (lift 1.0 / 0.65, denoise 1.0 / 0.65, deblur 1.0 / 0.6,
  CodeFormer fidelity 0.85 / 0.65). The validator may prefer the conservative one: it fixes the defect for the least change.
* **Bounds**: at most `ADAPTIVE_MAX_CANDIDATES` (8) images, a `ADAPTIVE_TIME_BUDGET_SECONDS` (180) budget after which no new
  operation starts, output at most `ADAPTIVE_MAX_OUTPUT_EDGE` (4096) on the long side, and the adapters' tiling.

## 3. The analyzer (`analyzer.py`)

Several independent signals, each with measured thresholds (`scripts/calibrate_analyzer.py`,
`docs/evaluation/2026-10-15-analyzer-calibration.md`):

* **Exposure** is judged **on the face** when there is one (luminance range and crushed-shadow share, not the mean, to avoid
  penalising dark skin) and on the frame otherwise. `low_light_subject` and `low_light` are defects; `dark_scene` (a visible
  subject in a dark scene) is not.
* **Blur** has a detail-retention signal and a gradient-direction signal that separates **motion** from **defocus**.
* **Noise** is the flat-region estimate banded by thresholds measured at known noise levels.
* **Faces**: size, detail (Laplacian variance at 128 px), luminance range, and a verdict `good / acceptable / poor /
  too_small`. A face under 48 px is never restored.
* Also resolution, compression artifacts and colour cast, plus `recommended_operations` and `limitations`.

## 4. The validator (`validator.py`)

A candidate must pass every **gate**: geometry, data consistency (shrunk back and softened it must still look like its parent,
the main defence against hallucination), colour (low-frequency chroma may not drift), artifacts (overshoot/halos, highlight
clipping, noise growth), **identity** (per-face embedding similarity at or above the provider's threshold, and landmarks that
did not move) and an operation-specific **does it fix the defect** test. A generative face restoration with no verifiable
identity is **never** accepted. Among candidates that pass, `improvement - 2 x change` decides, never "sharpest" or
"brightest". Limits and their provenance are in `Limits`. PSNR, SSIM and optional LPIPS exist for reference-based tests
(`reference_metrics`); there is no reference in the wild, and no number here replaces looking at the photograph.

Multi-face photographs: after CodeFormer, each face is composited back **only if its own identity check passes**.

## 5. Outcomes and the API contract

`GET /api/enhancements/results/{id}/` now also returns:

| Field | Values |
|---|---|
| `enhancement_outcome` | `enhanced`, `partially_enhanced`, `original_preserved`, `failed`, or `null` (a staged-pipeline result) |
| `enhancement_summary` | one plain sentence, safe to show: "Enhanced: brightened the dark subject, reduced noise. Not fully corrected: Defocus blur cannot be recovered reliably." |

plus the existing `status` (`queued/processing/completed/failed`), `output_width`, `output_height`, `download_url`,
`identity_status`. `original_preserved` means the output file holds the original pixels; the UI must not call it enhanced.
`applied_parameters.adaptive` has the full record (analysis, stages, skipped operations with reasons); intermediate images are
**not** stored in production.

## 6. Development comparison mode

`ADAPTIVE_DEBUG_ARTIFACTS=True` writes, through the default (encrypted, optionally S3) storage, under
`debug/enhancement/<run>/`: `original.png`, `01_quality_analysis.json`, `02_low_light.png`, `03_denoised.png`,
`04_deblurred.png`, `05_face_restored.png`, `06_upscaled.png`, `07_final.png`, `candidates.json` (every candidate, rejected ones
too, with model, checkpoint, device, timing, quality and identity verdict). Only stages that ran and were accepted have an image.
No endpoint serves them. `python manage.py cleanup_enhancement_debug` deletes runs older than
`ADAPTIVE_DEBUG_RETENTION_HOURS`. `scripts/build_model_sheet.py` makes a side-by-side sheet of every model.

## 7. Frontend

The contract above is all the frontend needs: upload (existing), `POST /api/enhancements/images/{id}/` with
`{"level": "auto", "style": "natural"}` (202), poll `GET /api/enhancements/results/{id}/` until `status` is `completed` or
`failed`, then show `download_url` (the one final image), `output_width x output_height`, and `enhancement_summary`. Show the
real `status` while it runs, never a timer. For `original_preserved` say "no safe improvement was found" (the summary does).
Do not show per-model outputs. The frontend repository is not writable from this project; nothing there was changed.

## 8. Performance

Models load once per process and are cached; `torch.inference_mode`/`no_grad` and `eval()` throughout; one heavy inference at a
time per process (a shared lock); tiled inference with out-of-memory retry at smaller tiles (`runtime.py`); MPS, CUDA or CPU
chosen per adapter. **Not every operator is verified on MPS**: Retinexformer, NAFNet, Real-ESRGAN and CodeFormer ran on MPS
here; GFPGAN needs its worker environment. No CUDA run was possible on this machine.

**Memory is the real constraint on a small machine.** On the 8 GiB M1 used for development, with the editor, a browser and the
dev servers open, the system sat at about 18 GiB of swap. Loading NAFNet-SIDD (464 MB), CodeFormer (377 MB), Retinexformer,
Real-ESRGAN and the detectors together made single calls take 100 s instead of 1-5 s, and two model-sheet runs hung inside a Metal
call (a stalled GPU command, not a bug in a model; the same GPU ran a trivial job instantly once the process was killed).
So `ENHANCEMENT_MODEL_RESIDENCY` (`auto` by default) keeps **one large model resident at a time** on a machine with under 12 GiB:
the engine loads models one operation at a time, so unloading the others costs a reload and frees hundreds of megabytes. On a
larger machine every model stays loaded. A deployment should still give the worker its own memory budget and not share it with a
desktop.

## 9. Tests and what they do not prove

`enhancement/test_adaptive_engine.py` (analyzer, router, validator, candidate pool, service, job and API integration, debug mode),
`enhancement/test_adaptive_real_models.py` (real checkpoints, skipped when absent), plus the suites for each adapter. Run:
`python manage.py test enhancement`. A passing test shows a decision was made correctly; it does not show a photograph looks
better. `scripts/benchmark_adaptive.py` measures the whole engine against clean references per defect category;
`docs/evaluation/2026-10-15-adaptive-benchmark.md` has the results and their limits.

## 10. Remaining limitations

Real motion blur differs from the synthetic blur the type signal was measured on; defocus and heavy blur are reported, not
fixed; a face under 48 px is not restored; the thresholds come from a small, demographically narrow set of photographs and
have not been validated on a diverse one; identity similarity is evidence, not proof; ArcFace thresholds are starting values;
CodeFormer and the ArcFace weights are non-commercial; the engine has not been run on CUDA; visual quality has been checked by
eye on a few photographs, not by a panel.
