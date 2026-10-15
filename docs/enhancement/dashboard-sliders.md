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
lines 686-692) and a canvas filter for the snapshot. Nothing is sent to the backend, and no model runs. `EnhancementControls.tsx`
is a second copy of three of them and is not wired either. This is a frontend task; the backend does not receive these values until the
frontend calls it.

## What the backend does with sliders

The backend's editor contract (`enhancement/controls.py`, served at `GET /api/enhancements/editor/configuration/`) is
the one place that defines a slider. It is rendered by a deterministic tone renderer (OpenCV and NumPy, in LAB), **not by a learned
model**. A slider never switches Zero-DCE, EDSR or any other model on; that is decided by the rendering policy
(`ENHANCEMENT_RENDER_POLICY`, see `optional-models.md`).

Previews: `POST /api/enhancements/results/{id}/previews/` with `{"revision": n, "mode": "manual", "adjustments": {...}}` returns a
bounded preview image; `.../previews/{id}/save/` keeps one as a version.

## Mapping the five sliders (the frontend converts)

| Dashboard slider (0-100) | Backend control | Conversion | Range sent |
|---|---|---|---|
| Enhancement Strength | `intensity` | `value` | 0 to 100 (100 neutral) |
| Brightness Correction | `brightness` | `(value - 50) * 2` | -100 to 100 |
| Contrast Boost | `contrast` | `(value - 50) * 2` | -100 to 100 |
| Skin Detail Recovery | `sharpness` | `(value - 50) * 2` (below 50 softens) | -100 to 100 |
| Noise Reduction | `denoise` (new) | `value` | 0 to 100 (0 off) |

The dashboard's default positions therefore send `intensity 75, brightness 0, contrast 20, sharpness -10, denoise 40`.
The backend also has `shadows`, `highlights`, `saturation` and `warmth` that the dashboard does not show yet; the frontend can
add them without a backend change. `intensity` blends the result with the unenhanced photo and needs an unenhanced baseline.
Without one it is reported as not applied rather than skipped silently.

## What was added for this

* **`denoise` (Noise reduction)**, a new manual control. Lightness is smoothed with an edge-preserving bilateral filter, the colour
  channels with a larger-radius one, then blended with the original by `value / 100`. It runs before the other controls and its
  radius is proportional to the image's long edge, so a preview and a full-resolution save look alike (tested; the
  difference allowed is slightly wider than for the global controls, because a neighbourhood filter does not commute with shrinking exactly).
* It is **manual only**: the local recipe and the OpenAI advisor are held to a range of 0 for it, so they never set it. Tests cover the range,
  the validation (a value outside 0 to 100 is a field error) and an end-to-end preview with the dashboard mapping.

## Limits (be exact about these)

* **Not a live video feed.** The backend edits a saved photo through a preview request; it does not process a webcam stream frame by frame.
  A preview of 1024 px renders in about 80 ms with noise reduction on (36 ms without), measured on the M1 CPU, plus the request time. A
  webcam "live" view would remain a browser filter, and the screen should say it is an approximation.
* **Noise reduction does little on photos that are already clean.** The phone photos tested here had estimated noise of 0 to 1; the effect on them
  is faint. It matters on grainy low-light photos.
* A slider does not make a learned model run. If the panel is to show "Zero-DCE" or "GFPGAN", that is a policy decision, not a slider.
* The frontend still has to call the API; until then its sliders are a simulation.
