# Slider-driven enhancement controls (Google Photos-style)

## Status

Backend-complete; no frontend exists in this repository yet (a separate
frontend integration is planned — see Feature 47 in `docs/FEATURES.md`).
Two independent slider concepts are supported:

1. **Style presets** at enhancement-creation time (`natural` / `dynamic` /
   `ai_max`) — a small number of discrete, one-tap choices.
2. **Manual/auto gallery-editor sliders**, rendered after enhancement
   completes, including a continuous 0-100% "how much AI enhancement"
   intensity slider.

## 1. Style presets

`POST /api/enhancements/images/<image_id>/` accepts `{"level": "...",
"style": "natural" | "dynamic" | "ai_max"}`. `level` (low/medium/high/auto)
and `style` are independent axes:

- **`natural`** (default) — the existing pipeline, unchanged.
- **`dynamic`** — runs the normal pipeline, then applies a bounded, guarded
  vivid contrast/saturation/warmth boost as a final step (reuses
  `apply_gallery_adjustments`'s existing safety loop, so it cannot blow out
  highlights or crush contrast beyond the same limits manual edits respect).
- **`ai_max`** — forces `level=high` and force-enables adaptive denoise for
  this one request, regardless of the platform's `ADAPTIVE_DENOISE_ENABLED`
  default. It is the strongest *local* correction: the restoration models it used
  to be able to call on (face restoration, super-resolution) were retired, and
  the result says so (`applied_parameters.ai_max`).

`applied_parameters.style` and `applied_parameters.dynamic_style` record
what actually happened, for auditability.

## 2. Gallery editor sliders

`GET /api/enhancements/editor/configuration/` returns every available
control. As of this feature:

| Control | Range | Default | What it does |
|---|---|---|---|
| `brightness` | -100..100 | 0 | LAB luminance shift |
| `contrast` | -100..100 | 0 | LAB luminance contrast around the median |
| `shadows` | -100..100 | 0 | Lifts/crushes dark tones |
| `highlights` | -100..100 | 0 | Lifts/crushes bright tones |
| `saturation` | -100..100 | 0 | LAB a/b channel scale |
| `warmth` | -100..100 | 0 | LAB b-channel shift (blue-yellow / colour balance) |
| `intensity` | 0..100 | 100 | Blend between the original preprocessed photo (0) and the fully AI-enhanced result (100) |

`preview_strategy: "client_side_preview_then_server_render"` is the
intended UX for all of these: the frontend renders a live local preview
(canvas/CSS) as the user drags, and only calls
`POST /api/enhancements/results/<result_id>/edits/` once, when the user
confirms — matching the existing non-destructive `ImageEditVersion` model
(each confirmed edit is a new, separate file; the enhanced original is
never overwritten).

`intensity` is the one control that isn't a tone curve: it blends the
*fully rendered* manual/auto edit back toward the original preprocessed
image (`_read_preprocessed_baseline`), resizing if AI restoration changed
the resolution. This is the continuous "how much enhancement" slider.

## Safety

- All tone controls (including the new `warmth`) go through the same
  bounded, iterative safety loop already used for brightness/contrast/etc.
  (`apply_gallery_adjustments`'s clipping/contrast-floor guard).
- `dynamic` style reuses that exact same guarded function rather than
  applying an unguarded fixed boost.
- `intensity` never changes pixel dimensions; it is a pure per-pixel blend.
