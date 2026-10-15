# The dashboard sliders and the backend

Frontend read at `622f2a4` (`ali4347/facecloud-ai-platform`, read only for the backend team).

## What the dashboard has today

`src/components/features/LiveEnhancementPanel.tsx` shows five sliders, each 0 to 100:

| Slider (label) | Default | Ends |
|---|---|---|
| Enhancement Strength | 75 | Natural to Strong |
| Brightness Correction | 50 | Low to High |
| Contrast Boost | 60 | Soft to Sharp |
| Skin Detail Recovery | 45 | Smooth to Detailed |
| Noise Reduction | 40 | Off to Clean |

**These sliders currently change only a CSS filter in the browser** (`brightness(...) contrast(...) saturate(...) blur(...)`,
lines 686-692) and a canvas filter for the snapshot. Nothing is sent to the backend, and no model runs.
`EnhancementControls.tsx` is a second copy of three of them and is not wired either. This is a frontend task; the
backend does not receive these values until the frontend calls it.

## What the backend does with sliders

The backend's editor contract (`enhancement/controls.py`, served at `GET /api/enhancements/editor/configuration/`) is
the one place that defines a slider. It has two halves:

* **Tone controls** (brightness, contrast, shadows, highlights, saturation, warmth, sharpness, and `intensity`), a
  deterministic OpenCV/NumPy renderer in LAB.
* **Model-backed controls** (`lowlight`, `denoise`, `deblur`, `face_restore`), each 0-100: the share of a model's
  result blended into the photo. They run first, in that order, then the tone controls. The models are
  Retinexformer (fallback Zero-DCE, then an OpenCV lift), NAFNet-SIDD (fallback a bilateral filter), NAFNet-GoPro
  (fallback an unsharp mask) and CodeFormer (no fallback: reported as not applied). `enhancement/model_controls.py`
  documents the contract. Each model's candidate is cached under its input's hash, so the first move of a slider is
  as slow as the model and moving it again, or moving a later slider, is a blend.

The sliders are explicit user requests, so a model slider needs only that model's own setting (`RETINEXFORMER_ENABLED`
and so on), not the automatic pipeline's `ENHANCEMENT_RENDER_POLICY`.

Previews: `POST /api/enhancements/results/{id}/previews/` with `{"revision": n, "mode": "manual", "adjustments": {...}}`
returns a bounded preview image; `.../previews/{id}/save/` keeps one as a version. The response's `render` block lists
`model_controls` (which engine ran for each moved slider, whether it was cached) and any `fallback_reasons`.

## Which engine serves each slider

`GET /api/enhancements/editor/configuration/` returns, for every control, `model_backed`, `engine`, `available` and
`uses_model`. A client can label a slider "Retinexformer" or grey it out. Example with every model off:
`lowlight` -> `opencv_gamma`, `denoise` -> `opencv_bilateral`, `deblur` -> `opencv_unsharp`, `face_restore` ->
`engine: null, available: false`.

## Mapping the dashboard's five sliders (the frontend converts)

| Dashboard slider (0-100) | Backend control | Conversion | Range sent |
|---|---|---|---|
| Enhancement Strength | `intensity` | `value` | 0 to 100 (100 neutral) |
| Brightness Correction | `brightness` | `(value - 50) * 2` | -100 to 100 |
| Contrast Boost | `contrast` | `(value - 50) * 2` | -100 to 100 |
| Skin Detail Recovery | `sharpness` | `(value - 50) * 2` (below 50 softens) | -100 to 100 |
| Noise Reduction | `denoise` | `value` | 0 to 100 (0 off) |

The dashboard's default positions therefore send `intensity 75, brightness 0, contrast 20, sharpness -10, denoise 40`.
Controls the dashboard does not show yet, which the frontend can add without a backend change: `lowlight`, `deblur`,
`face_restore` (the three other model sliders), and `shadows`, `highlights`, `saturation`, `warmth`. `intensity` blends
the result with the unenhanced photo and needs an unenhanced baseline; without one it is reported as not applied
rather than skipped silently.

## Limits (be exact about these)

* **Not a live video feed.** The backend edits a saved photo through a preview request; it does not process a webcam
  stream frame by frame. A preview of 1024 px renders the tone controls in about 40 ms; a model slider costs the model's
  time on its first move (Retinexformer about 1.5 s warm on the M1 at 1024 px, NAFNet and CodeFormer several seconds)
  and a blend afterwards. A webcam "live" view would remain a browser filter, and the screen should say it is an
  approximation.
* **Noise reduction does little on photos that are already clean.** The phone photos tested had estimated noise of 0 to 1.
* **A model slider is a blend, not a guarantee.** Face restoration invents plausible detail; the identity gate protects
  the automatic pipeline, but a slider at 100 is the model's raw candidate.
* The frontend still has to call the API; until then its sliders are a simulation.
