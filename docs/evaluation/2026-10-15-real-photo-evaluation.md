# Real camera photos, 2026-10-15

16 photographs taken on a phone (Redmi Note 12; portraits, mostly at night or indoors; 900-1204 x 1600), supplied by the
project owner. Each was run through five configurations on the CPU (Apple M1, no GPU): automatic mode, and the
levels `medium` and `high` under `lightweight_v1` and under `balanced_v1`. The photos are not committed and
are not uploaded anywhere. Only the numbers below are recorded. The photos have no ground truth, so this table
reports what changed and a visual check, not a quality score.

## What the photos are

Mean luminance 68 to 152 (0 to 255). Estimated noise 0 to 1 on every photo: the phone's night mode already denoised
them. These are already-processed photos, not raw low-light captures, so there is little left for a
conservative pipeline to repair.

## Results

| Configuration | What it did on these 16 photos |
|---|---|
| automatic (default) | **Pixel-identical output on all 16.** The lift was withheld on the 9 darkest ones (luminance 68 to 112); on the other 7 nothing needed fixing. |
| lightweight, level high | Brightness -1 to +8. The 9 darker photos moved by +3 to +8 (for example 68 to 76, 80 to 89). Well-exposed photos were not brightened. 0.3 to 1.2 s per photo. |
| balanced (Zero-DCE, CPU), level high | The 9 darker photos moved by +6 to +24 (68 to 89, 73 to 97, 80 to 101, 87 to 106). Well-exposed photos were treated as the lightweight pipeline treats them (the lift is skipped). 1.1 to 2.0 s per photo. |

## Visual check (side by side, 4 photos at high level)

* **Faces stay natural.** Skin tone, beard and clothing colour are preserved in both policies. No halos, no plastic skin.
* **`balanced_v1` gives the more visible improvement** on the dim indoor selfie and on the night street scenes.
* **Night skies are lifted too.** In the two street scenes the black sky becomes a lighter grey-violet; some would
  call that less true to the scene. This is a property of a global lift, and the face-aware exposure that might
  avoid it was deliberately not built (it would bias skin tones).
* **One photo is sharpened visibly harder than the others:** the golden-stone photo (`4a5343a3`), whose laplacian
  sharpness went from 205 to 329 (+60 %) and estimated noise from 1 to 2 at `high`. At 1:1 the stone
  texture looks crisper but slightly crunchy. This is the setting a reviewer would most reasonably question; a
  smaller sharpening cap for already-sharp photos would be the fix. It is not changed here.

## Honest conclusions

1. On already-processed phone photos the default pipeline correctly does almost nothing. That is the
   intended behaviour, but it also means automatic mode shows no visible benefit on this set.
2. A chosen lift helps the darker photos moderately (`balanced_v1` more than `lightweight_v1`). It does not
   change them into well-lit photos.
3. Nothing was measured against a real ground truth. The earlier benchmark
   (`2026-10-15-lowlight-benchmark.md`) uses synthetic darkening and is the only scored comparison.
4. The unfinished items remain: sharpening cap, Zero-DCE licence check, real OpenAI advisor validation.
