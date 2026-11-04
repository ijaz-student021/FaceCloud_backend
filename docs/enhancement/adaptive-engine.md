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
the main defence against hallucination), colour, artifacts (overshoot/halos, highlight clipping, noise growth), **identity**
(per-face embedding similarity at or above the provider's threshold, and landmarks that did not move) and an operation-specific
**does it fix the defect** test. A generative face restoration with no verifiable identity is **never** accepted. Among candidates that
pass, `improvement - 2 x change` decides, never "sharpest" or "brightest" (the factor 2 is a heuristic, not a fitted value). Limits and
their provenance are in `Limits`. PSNR, SSIM and optional LPIPS exist for reference-based tests (`reference_metrics`); there is no
reference in the wild, and no number here replaces looking at the photograph.

*Colour.* Most operations: the low-frequency CIELAB chroma may not drift. A **low-light lift** changes brightness by design, so it is
judged on chromaticity in linear light instead (hue shift, and *saturation ratio* against its parent, limits 0.85-1.18): a lift that
turns colours grey (`colour_washed_out`), over-saturates (`colour_oversaturated`) or rotates hue (`colour_drift`) is rejected. The lift
itself takes its colours from the photograph and only its brightness from Retinexformer
(`adaptive/colour.py`, measured in `docs/evaluation/2026-10-15-lowlight-colour.md`).

*Noise.* `noise.level` is the larger of two measures (the 3x3 residual and a flat-region grain measure that sees the coarse grain a
lift leaves), 1.0 = "moderate". A lift may raise it up to 1.0 (the denoise stage takes over from there) and at most 3x beyond that.
Multi-face photographs: after CodeFormer, each face is composited back **only if its own identity check passes**.

## 4a. Face restoration is off by default, deliberately

`CODEFORMER_ENABLED` and `GFPGAN_ENABLED` default to `False`, so under `adaptive_v1` the engine does low light, denoise, deblur and
upscaling but **does not restore faces** until an operator switches a face restorer on, and it says so in `operations_skipped`.
Reason: the identity gate can only compare a restored face with the *degraded input* it was given. In the benchmark it measured
0.85-0.91 where ArcFace similarity to the true clean photograph had fallen by 0.07-0.13, so the gate cannot see the drift that
matters, and the 0.70 ArcFace threshold rests on five data points. Raising the threshold alone would not make that guarantee.
Turn restoration on only after validating it on a larger, more diverse set of faces than was available here.

## 4b. Routing decisions that were measured

* **Motion deblurring** is routed from the blur type; on held-out degradations the typing is approximate (defocus sent to the model
  10/32, motion missed 16/40 by how the copy was made), but the router captured 90 % of the model's gain and the validator is the
  second gate. Measured in `docs/evaluation/2026-10-15-blur-routing.md`; no threshold was changed.
* **Super-resolution** runs only for a small photograph (short edge under 480 px) at x2. A small face alone no longer enlarges the
  whole photograph (`ADAPTIVE_FACE_UPSCALE_BELOW=0`; the router records "A small face does not justify enlarging the whole
  photograph"): that is several times the compute for pixels nowhere near the face, with no measured benefit. Pixel metrics rate Real-ESRGAN
  1-3 dB *below* bicubic (model sheet), so it is a visual-quality option, not a measured gain.
* **Denoising** offers blend 1.0 and 0.65 and lets the validator choose, because 65 % beat full strength on light-to-moderate noise and
  full strength won on heavy noise (`docs/evaluation/2026-10-15-denoising.md`).

## 5.0 Every photograph is enhanced: the final layer (`ADAPTIVE_ALWAYS_ENHANCE`, on by default; 2026-11-03)

The owner's rule: the model used for light photographs is the **last layer of every photograph**, whether anything needed correcting or not ("just apply it"). After the router has nothing
more to do (the corrective steps, then colourization), the service asks for two more things (`router.final_layer_step`, `router.finish_step`):

1. **The final layer**: the compact `realesr-general-x4v3` network (`general` only; the slow networks are not its fallback) at about 1.0 and 0.7 of its own output, stacked on whatever the
   earlier steps accepted (a lifted dark photograph, a deblurred and face-restored one, an enlarged one). It is skipped when the router's own detail step already accepted the same model,
   when the compact model is off, or when the photograph is over `ADAPTIVE_DETAIL_MAX_PIXELS` (4 MP: the model's time and memory grow with the pixels). It is **applied unless** it smooths the
   photograph by more than `ADAPTIVE_REFINE_MIN_GAIN` allows (structure energy x0.75; on a brightened dark photograph the model cleans amplified noise and JPEG blocks, which reads as 0.6-0.9; a
   defocused portrait is smoothed to 0.94), adds an invented amount (> 1.6), changes nothing visible (mean change under `ADAPTIVE_REFINE_MIN_CHANGE`, 0.5 of 255), a face grows past
   `max(260, 1.1 x its detail before)`, or any other gate (identity, face geometry, halo, clipping, colour) refuses it. 2-9 s for a 1400 px photograph. What is left unresolved is judged
   before this layer (its smoothing can read as a blur to the analyzer).
2. **The finishing polish, as the fallback**, only when nothing at all was accepted (also when a corrective step was refused, and without `ADAPTIVE_POLISH_ENABLED`): `ADAPTIVE_FINISH_STRENGTH` (3)
   times the gentle share, vibrance `ADAPTIVE_FINISH_VIBRANCE` (15 percent instead of 6) and a ladder of five variants, strongest first (finish, finish blended 0.65, finish with soft edges,
   finish gentle, finish colour: no sharpening at all). Kept if every safety gate passes, no measure falls below `ADAPTIVE_FINISH_TOLERANCE` (0.97) and the photograph really changes
   (mean change of at least `ADAPTIVE_FINISH_MIN_CHANGE`, 1.5 of 255). The gentle polish needed a 5 percent gain, which a balanced, sharp photograph does not give, so 26 of the owner's 48
   results had been returned unchanged. The main face's detail ratio is not asked to hold here (the detector can pick another face after a small change); a face that gets too sharp is still
   refused by the ceiling.
3. A refused candidate releases its pixels and no longer counts against `ADAPTIVE_MAX_CANDIDATES` (the total ever proposed is capped at 6 times the limit): refused lift and model candidates
   used to fill the pool, so the polish was only offered its first one or two (strongest) variants.

Evidence (`docs/evaluation/2026-11-03-always-enhance.md`): of 14 photographs, the final layer was applied to 8 (4 of them stacked on a lift, a deblur and face restoration, or a
super-resolution); 5 got the finishing polish; 1 stayed as it was because every candidate was refused by the identity, face-geometry and highlight gates.

## 5a. Polish: a gentle improvement for a photograph with nothing to fix (`ADAPTIVE_POLISH_ENABLED`, off by default)

Without it, a well-lit, sharp photograph runs no operation and ends as `original_preserved`. With `ADAPTIVE_POLISH_ENABLED=True` the
router may offer one more operation, **polish**, and only for a photograph on which nothing else was found:

* the analyzer recommends no operation, none was tried (a rejected low-light lift, denoise, ... also blocks polish) and none was applied;
* the scene is not a `dark_scene` left as photographed;
* the style is `natural` (CLAHE share 25 %, unsharp amount 0.25) or `dynamic` (55 %, 0.50); `ai_max` is unchanged and never polished;
* the engine is OpenCV and NumPy only (`opencv_polish`): no model, no licence, nothing to install.

What it does, on CIELAB lightness first: (a) local contrast, CLAHE (clip 1.5, 8 x 8 tiles) blended in at the style's share, keeping
only its mid-scale band (the full CLAHE change also lifted grain and JPEG blocks: noise estimate x1.17 and landmark jitter of 1.9 %
on two compressed portraits, measured in `docs/evaluation/2026-10-24-adaptive-polish.md`); (b) a lightness-only unsharp mask (sigma 1.2
at the 1024 px working scale, soft threshold so grain and flat regions are not amplified), lower inside a face and none at all for a
face that is already sharp (detail 119 to 175 is the measured range of a sharp face); (c) a levels stretch of at most 8 %, only when the
1st to 99th percentile of lightness covers under 85 % of the range, never into clipping; (d) at most 6 % more chroma, on low-chroma
pixels only. Two variants (`blend 1.0`, `blend 0.65`) are proposed, like the other operations.

The result is kept only if **all** of these hold (`Limits.polish_*` in `validator.py`): the main face's detail or the frame's contrast
(standard deviation of luminance) rises by at least 5 % and none of face detail, frame contrast, local contrast and frame sharpness gets
worse; a face does not leave the sharp range (detail 175, or 10 % above a face that was already above it); no more than 0.5 percentage
points of newly clipped highlights; the noise estimate is not more than 10 % worse; the mean CIELAB distance from the original is at
most 3; face landmarks move by at most 2 % of the face width; and the existing gates (data consistency, colour, halos, identity) pass.
Otherwise the output is the original pixels, `original_preserved`, and `skipped_stages` carries
`{"stage": "polish", "reason": "Already balanced and sharp: nothing to improve."}`. A polished result is `enhanced` (or
`partially_enhanced` when a defect the engine cannot fix remains, as for any operation), with the summary "Polished: balanced the
contrast and sharpened lightly." Polish never changes a photograph that has a defect, and with the flag off the engine is
byte-for-byte what it was (`enhancement/test_adaptive_polish.py`, and the golden before/after run in the evaluation note).

## 5. Outcomes and the API contract

`GET /api/enhancements/results/{id}/` now also returns:

| Field | Values |
|---|---|
| `enhancement_outcome` | `enhanced`, `partially_enhanced`, `original_preserved`, `failed`, or `null` (a staged-pipeline result) |
| `enhancement_summary` | one plain sentence, safe to show: "Enhanced: brightened the dark subject, reduced noise. Not fully corrected: Defocus blur cannot be recovered reliably." |

plus the existing `status` (`queued/processing/completed/failed`), `output_width`, `output_height`, `download_url`,
`identity_status`. `original_preserved` means the output file holds the original pixels; the UI must not call it enhanced. A failed job
returns `enhancement_outcome: "failed"` and the summary "The photograph could not be enhanced. Your original photograph is unchanged."
(never an internal error text).

`applied_parameters.identity_verification` (additive, existing keys unchanged) now carries `state`, the plain answer:
`verified` (every face in the result compared with the upload and held), `uncertain` (a comparison was attempted but was not
reliable), `not_verifiable` (no comparison was possible: no consent, no identity model, or the uploaded face too dark or small to embed;
`notice` says so), `not_applicable` (no face, or the photograph was not changed). `status` keeps its earlier values. An unavailable
comparison is never reported as passed, and `verified` only means similar to the uploaded photograph, not a guarantee.
Each run also writes one structured log line (`adaptive_enhancement {...}`: outcome, operations, per-stage seconds, models, rejection
reasons; no pixels, paths or user data) and `python manage.py adaptive_report --days 7` aggregates outcome rates, P50/P95 latency, queue
wait, rejection reasons and models run from the stored job records.
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
