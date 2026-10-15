# Lightweight enhancement pipeline (Phases A and B)

Status: implemented and tested on branch `feat/lightweight-enhancement`.
Phase A is sections 1-4; Phase B (previews, full-resolution saving, versions)
is section 5. Phase C (optional OpenAI advisor) is documented in
`openai-advisor.md`.

## 1. Rendering policy

`ENHANCEMENT_RENDER_POLICY` selects which stages may run.

| Policy | Behaviour |
|---|---|
| **`lightweight_v1`** (default) | OpenCV and Pillow only. Never runs Zero-DCE, EDSR, NAFNet, Real-ESRGAN, GFPGAN, PureVision, PureScale or colourisation, whatever their own settings say. |
| `learned_v1` | The earlier behaviour: each model's own setting (`ZERO_DCE_ENABLED`, ...) decides. |

Adapters, checkpoints and their settings are untouched, so going back is one
setting. A stage the policy keeps off reports *"disabled by the lightweight_v1
rendering policy"* and is **skipped**, never a "fallback": nothing is tried and
nothing substitutes for a model. Every result records `render_policy` (name,
description, which configured stages the policy overrode,
`heavy_restoration_available`).

YuNet and SFace are not enhancement models and keep working: face detection,
the face-fidelity check and consented identity features. With no face, or no
consent or model, the identity status is `inconclusive` or `unavailable`, as
before.

**Pipeline under `lightweight_v1`:** immutable source → shared colour-managed
decoding → OpenCV measurements → exposure (LAB gamma + local contrast, only for
a measured lighting defect) → measured denoising → conditional sharpening →
quality guards → face-fidelity and identity checks → validated encoding. Each
stage runs at most once.

### AI Max

AI Max keeps its meaning: level `high` and denoising where noise is measured.
It does **not** add heavy restoration under `lightweight_v1`. The result carries
an `ai_max` block and a warning saying so; face restoration and super-resolution
exist only under `learned_v1`. Severe blur and low resolution are reported as
beyond the lightweight pipeline, not as a missing model.

### Automatic mode and dark photos

A dark photo is either underexposed or dark by design, and pixels cannot say
which. Across 498 real photos the 128 with mean luminance below 115 show
highlight levels (p95) spread from 0 to 255 with no gap, so any automatic
threshold would be a guess. Therefore, under `lightweight_v1`, level `auto`
with the natural style:

* does **not** lift a dark photo and records a `lift_withheld` routing decision
  (the plan says *"Choose a level"*);
* does not derive a high level from darkness (a dark photo gets level `low`);
* still recovers blown highlights, flat contrast, noise and measured softness.

Choosing a level (`low`, `medium`, `high`) or a style is the request for the
lift and works as before. `learned_v1` keeps the earlier automatic behaviour.

## 2. Editing controls

Defined once in `enhancement/controls.py`; rendered by `enhancement/tone.py`.
All controls are signed with **0 neutral**. Pillow factors have **1.0
neutral**: `factor = 1 + k * value`.

| Control | Manual range | Automatic range | Renderer (CIE LAB, 0-255 lightness scale) | Pillow `k` | Measured difference from Pillow* |
|---|---|---|---|---|---|
| brightness | -100 to 100 | -16 to 16 | L += value x 0.35 | 0.35/128 (Brightness) | 4.1 levels |
| contrast | -100 to 100 | -8 to 20 | L scaled about its median by 1 + value x 0.005 | 0.005 (Contrast) | 2.7 |
| shadows | -100 to 100 | 0 to 18 | L += value x 0.45 x (1 - L/255)^2 | none | n/a |
| highlights | -100 to 100 | -18 to 0 | L += value x 0.40 x (L/255)^2 | none | n/a |
| saturation | -100 to 100 | -8 to 8 | a, b scaled by 1 + value x 0.004 | 0.004 (Color) | 0.7 |
| warmth | -100 to 100 | -8 to 8 | b += value x 0.30 | none | n/a |
| sharpness | -100 to 100 | 0 to 20 | L += value/100 x (L - blur(L)); radius sigma 0.68 px at 1024 px, proportional to the longer edge, at least 0.5 px | 0.01 (Sharpness) | 0.2 |
| intensity | 0 to 100 | n/a | blend with the unenhanced photo; 100 is neutral | n/a | n/a |

\*Mean absolute difference in grey levels on 40 real photographs at 1024 px. The
renderer works in LAB, not on RGB as Pillow does, so the factor is an
*approximation*, except contrast and sharpness, which are the same operator on
lightness. Brightness is additive in LAB but multiplicative in Pillow's RGB.
Hard-edged synthetic charts are a worst case (sharpness then differs by 2.6).

* **Manual vs automatic ranges are separate.** The local recipe (and later the
  advisor) can never leave the automatic range; a person may use the manual one.
* **Reset is neutral:** all controls 0, intensity 100. A render with neutral
  controls returns the source **exactly** (the previous renderer changed 38 %
  of real pixels, by up to 20 levels, even then). **Auto and presets are
  separate requests**, never a side effect of reset.
* **Intent** (auto mode only): `auto` fixes clear technical faults and never
  lifts or darkens overall exposure; `brighten` also lifts a dark photo;
  `preserve` leaves tonality alone.
* **Validation:** finite numbers only. Booleans, NaN, infinity, malformed
  strings, out-of-range values, non-objects and **unknown control names** are
  field errors, reported together.
* **Recorded on every edit:** requested, validated and applied values, each
  guard reduction (`guard_reductions`), `fallback_reasons`, `renderer_version`
  (`local_tone_renderer_v2`), `render_policy`, and for auto the recipe's
  `action` (`apply` / `no_change`), `rationale` and `caution_flags`.

## 3. Evidence

Environment: Apple M1 (CPU for all stages; MPS and CUDA not used here). Real
checkpoints were present but, by policy, unused. No model ran in either
configuration for these photos; the difference is Zero-DCE (OpenCV DNN on CPU)
under `learned_v1`.

### Held-out real photographs (16, evaluation half of an earlier split)

`learned_v1` against `lightweight_v1`, level `auto`:

| | `learned_v1` | `lightweight_v1` |
|---|---|---|
| Photos changed | 5 lifted by Zero-DCE, 2 small highlight tweaks | 2 small highlight tweaks (`bright__13`, `textured__10`; mean luminance moved by 0.1 to 0.2) |
| Dark photos (mean luminance) | 38.6 to 68.7, 65.7 to 82.1, 68.1 to 85.3, 79.2 to 91.5, 93.4 to 102.9 | unchanged |
| SSIM to the source, dark photos | 0.74 to 0.97 | 1.000 |
| Total time, 16 photos | 9.0 s | 5.3 s |
| Peak process memory | 1252 MB | 625 MB |

**Composition (an honest correction):** the five dark photos are *not*
low-key portraits. They are a dark-mode chat screenshot, a product poster, a
food photo, an identity document on a black background and a product shot,
chosen by luminance statistics alone. They show why a dark image cannot be
assumed underexposed (lifting a dark-mode screenshot is wrong), but the set
**contains no faces**, so it says nothing about portraits. Nine of the 16 came out
unchanged under both policies.

With an explicit level, `lightweight_v1` lifts less than Zero-DCE on very dark
photos (the darkest: 38.6 to 42.9 at `medium`, 46.7 at `high`, against 68.7).
That is the cost of the policy; the editor's `brighten` intent and the
brightness and shadows controls can lift further.

### Development photographs (7, used in earlier evaluations)

| Photo | Source mean | `learned_v1` | `lightweight_v1` | SSIM to source, learned / lightweight | Faces | Identity |
|---|---|---|---|---|---|---|
| a_portrait_lowkey | 72.7 | 87.8 | 72.1 | 0.946 / 0.995 | 1 | verified |
| b_portrait_harsh_sun | 103.0 | 112.1 | 103.0 | 0.975 / 1.000 | 1 | verified |
| c_portrait_studio | 169.1 | 167.8 | 167.8 | 0.998 / 0.998 | 1 | verified |
| d_hands_dark | 39.4 | 64.5 | 39.4 | **0.616** / 1.000 | 0 | inconclusive |
| e_highkey_white | 234.2 | 234.2 | 234.2 | 1.000 / 1.000 | 0 | inconclusive |
| f_still_life | 179.3 | 179.3 | 179.3 | 1.000 / 1.000 | 0 | inconclusive |
| g_closeup (16 MP) | 103.9 | 111.2 | 103.9 | 0.991 / 1.000 | 1 | verified |

`d_hands_dark` is the earlier recorded regression (a deliberately dark photo
lifted to flat grey, SSIM 0.616): it is no longer touched.

### Limits and what was not tested

* Most clean photos receive **no change** by design: the pipeline corrects
  measured faults and does not "beautify".
* No new visual-quality claim: nothing here shows the lightweight output is
  *better*, only that it is more conservative, faster and lighter, and no
  longer alters photos whose darkness may be intended.
* Severe blur, low resolution and noise-limited photos are not recovered.
* The preview-versus-full-resolution agreement is tested on generated charts
  (every control within 1.5 levels); real-photo preview parity belongs to Phase B.
* No CUDA or MPS timing; no paid or network call of any kind was made.
* Image evaluation outputs are not committed: the sets include personal
  images (one is an identity document).

## 4. Configuration and compatibility

* New setting: `ENHANCEMENT_RENDER_POLICY` (`lightweight_v1` default;
  `learned_v1` restores the earlier behaviour). Invalid values refuse to start.
* No migration. New response fields are additive: `render_policy`, `ai_max`,
  the `sharpness` control, `intent`, `renderer_version`, `validated`,
  `applied`, `guard_reductions`, `fallback_reasons`, `outcome`, `auto`.
* Behaviour changes to know about: automatic mode no longer lifts dark photos;
  the editor rejects unknown control names; the old `fallback` stage status is
  now `skipped` for stages the policy keeps off; system health no longer
  reports a missing checkpoint for a stage that cannot run.
* Historical results are unchanged and keep their stored parameters.

## 5. Editor previews and saved versions (Phase B)

### Flow

```
enhanced result (immutable) --source_version--> bounded lossless copy (once per version)
   slider move -> POST previews/ {revision, mode, adjustments}   -> small JPEG/PNG, same renderer
   Apply       -> POST previews/{id}/save/                       -> full-resolution edit version
                                                                     (Celery task, claim, protected download)
```

* **Stable source.** `source_version` (hash of the result id, finish time, file
  size, name and dimensions) names the exact enhanced image. A preview or save
  that names another version is refused with `409 stale_source`, which carries
  the current version. The editor always renders from the *enhanced* image,
  never from an earlier edit, so edits do not stack.
* **Bounded.** A preview is at most `ENHANCEMENT_PREVIEW_MAX_EDGE` (1600) on its
  longer edge (`size` 128-max, default 1024) and is rendered from a cached
  lossless copy of the enhanced image, so moving a slider never decodes the full
  file. Previews use the same renderer as the full-resolution save; the test
  suite checks the two agree (mean difference below 2.5 levels, JPEG included).
* **Revision-specific, never overwritten.** Each preview has its own id, file
  and `revision` (a client counter). A late answer to an older revision is stored
  and reported `is_latest: false`; it cannot replace a newer preview. Repeating
  a revision with the same settings returns the stored preview without
  rendering (`200`); the same revision with different settings is
  `409 revision_conflict`.
* **Atomic.** The file is written first and its row is added under a lock on
  the result; if the row cannot be added the file is removed. Only the newest
  `ENHANCEMENT_PREVIEW_KEEP_PER_RESULT` (10) previews are kept; older files are
  deleted with their rows.
* **Closing the browser cancels nothing.** A render finishes and is stored; the
  client re-requests the same revision to fetch it.
* **Apply / Save.** `POST previews/{id}/save/` (or `results/{id}/edits/` with an
  optional `source_version`) creates a normal edit version through the existing
  Celery task. It re-renders at full resolution from the enhanced result with
  the preview's validated settings. At most one edit per result is active
  (`400` otherwise). A save is refused (`409`) if the result changed after the
  preview.
* **Edit task hardening (found and fixed in this phase, each demonstrated by a
  failing test first):** edits of a PNG with transparency lost the alpha channel;
  an edit deleted while rendering was re-created by the worker; a duplicate
  delivery of the task could render twice. Edits now keep alpha, hidden
  transparent pixels are not changed, and a claim token with a resume token
  serialises workers.
* **Compression.** A saved edit is one more encode of the enhanced image (itself
  one encode of the pipeline output). Edits do not stack, so it is never a chain
  of re-compressed JPEGs, but the second generation exists and is documented.
* **Transparency.** PNG results keep their alpha through every preview and edit.
  A JPEG result has none: a source with transparency was composited at
  preprocessing onto `PREPROCESS_TRANSPARENT_BACKGROUND` (white by default),
  before enhancement. `GET editor/configuration/` states this under `transparency`.
* **Retention.** Previews expire after `ENHANCEMENT_PREVIEW_RETENTION_HOURS`
  (24); the Beat task `enhancement.cleanup_previews` (every two hours) deletes expired
  previews and unused cached sources. It never touches originals, enhanced
  results or saved edits, which have their own retention. A saved edit survives
  the deletion of the preview it came from (`from_preview` becomes null). The
  orphan-file scanner knows the `previews` folder.
* **Authorisation.** Every preview endpoint is limited to the owner and returns
  `404` to anyone else, `401` unauthenticated. Preview creation has its own
  throttle.

### Migration

`0009_edit_previews_and_edit_claims` adds two tables and additive columns to
`ImageEditVersion`. It was applied only to disposable test databases, never to
an existing one.

### Evidence and limits

* Tests: `enhancement/test_previews.py` (26) and `test_edit_lifecycle.py`.
* Preview/full-resolution parity is tested on a generated photo-like texture at
  900x1400, not on a library of real photographs.
* Revision ordering under truly concurrent requests is serialised by a row lock
  on PostgreSQL; the SQLite test database cannot show that, so it is checked
  separately (see the tracker).
