# Every photograph enhanced: what the conditions were and what changed (2026-11-03)

## The conditions before

The engine corrected only what the analyzer measured as a defect (low light, noise, motion blur, a degraded face, low resolution, softness or heavy compression). A photograph
with none was offered only the gentle polish, which had to raise the frame's contrast or the main face's detail by **5 percent**; a balanced, sharp photograph does not, so the
result was refused as `no_measurable_improvement` and the original pixels came back with "did not need enhancement". On the owner's database: **26 of 48** completed results.

## What changed (`ADAPTIVE_ALWAYS_ENHANCE`)

See `docs/enhancement/adaptive-engine.md` section 5.0: no-defect photographs go through the light-photo model (refine), then a stronger finishing polish; refused candidates no
longer fill the candidate pool. Every safety gate still applies.

## Measured

20 photographs of the owner's library that had been returned unchanged, reduced to a 1400 px long edge, the whole engine (`natural` / `auto`):

| Route | Photographs | Time |
|---|---|---|
| the light-photo model (refine) accepted | 3 (74, 61, 57 after deblur and face restoration) | 2-9 s |
| the finishing polish accepted (the model added no structure, or was refused for clipping or halo) | 14 | 4-9 s |
| super-resolution (small photographs) | 2 | 7-8 s |
| **kept as they were**: lift, model and finish all refused (identity changed, face geometry changed, highlight clipping) | 1 (a screenshot of an interface with faces) | 8 s |

Mean change of the 17 polished or refined photographs: 1.7-5.6 grey levels of 255 (contrast +1-6 percent, more colour in muted areas). It is a visible but moderate change, side by
side; it is not a different photograph. Four photographs were looked at at 100 percent (a portrait refined by the model, three polished): more contrast and colour, no halo or
damage seen; the striped collar of the refined portrait shows a slight waviness (a known tendency of the compact network on fine stripes; the artifact gate accepted it).

Why the model is not used more: on these photographs it adds 0-12 percent structure and the 260 face-detail ceiling refused several sharp faces; on a defocused portrait it lowers structure
(0.94x), so the finish takes over. With a wrong model path in a first measurement the engine fell back to the 70 s Real-ESRGAN x4plus for every photograph; the refine step now uses
the compact model only.

## Not verified

* 20 photographs, one person's eye for the 100 percent crops; no group photographs, no black-and-white photographs and no `dynamic` / `ai_max` styles were measured for this change.
* Photographs over 4 MP skip the model and are finished only (timings for those were not measured here).
* "Visible" is a mean change of at least 1.5 of 255; whether that is visible enough is the owner's call (`ADAPTIVE_FINISH_STRENGTH`, `ADAPTIVE_FINISH_MIN_CHANGE`).
