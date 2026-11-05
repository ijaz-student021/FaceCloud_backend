# The steps after an accepted face restoration are judged against the restored face (2026-11-05)

**The report.** When the engine accepted a face restoration, the enlargement and the detail layer that should follow it were sometimes refused for `identity_changed` by a hair, and the photograph came back restored but small and without its detail layer. The identity limit for a step was 0.80 against the UPLOAD; the restoration itself, and the saved photograph's final identity check, are held to the restoration limit (0.65, `SFACE_RESTORE_GUARD_MIN_SIMILARITY`, PR #132). The steps after a restoration were therefore held to a stricter limit than the restoration they follow and than the verdict on the result.

## What was measured (real models, the whole engine, the live `.env`)

The library's 448 x 296 portrait (digest prefix `67c29a14`), `auto` / `natural`. Each line is one candidate the validator judged; the similarity is the SFace similarity with the upload, the limit is the one that applied.

| step | candidate | similarity | limit | before | after |
|---|---|---|---|---|---|
| face restoration (CodeFormer) | 448 x 296 | 0.8072 | 0.65 | accepted | accepted |
| enlargement | 896 x 592 | 0.7993 | 0.80 / 0.65 | **refused** (`identity_changed`, short by 0.0007) | accepted |
| detail layer, first candidate | 448 x 296 before, 896 x 592 after | 0.7887 before, 0.7873 after | 0.80 / 0.65 | refused | accepted |
| detail layer, second candidate | 448 x 296 before, 896 x 592 after | 0.7964 before, 0.7891 after | 0.80 / 0.65 | refused | accepted |
| **result** | | | | `denoising`, `face_restoration`; **448 x 296**; final identity 0.8072 (limit 0.65) | `denoising`, `face_restoration`, `super_resolution`, `detail_restoration`; **896 x 592**; final identity 0.7873 (limit 0.65) |

(The detail candidates of the two runs are different images: before, they were made on the small photograph because the enlargement had been refused. The similarity shown after is the comparison with the least room, see below.)

* **A restored face at 0.807 left a later step at 0.799 refused, and a photograph that was restored lost its enlargement and its detail layer by 0.0007.** The same photograph's final check then reads 0.8072 against 0.65: the engine was willing to save a face at 0.65 but not to enlarge one at 0.80.
* **The step did not lose the face; the restored face was already below the step's limit.** Every later step starts from the restored face, so a step that keeps that face unchanged scores about what the restoration scored against the upload (0.807 here). A restoration accepted anywhere between 0.65 and 0.80 therefore left every later step refused under the 0.80 limit, whatever the step did.

## Why two limits, not one

A step after an accepted restoration is held to both:

1. **against the UPLOAD at the restoration limit (0.65)**: it can never leave a face the final check would refuse, and this is the limit the restoration itself, and the saved photograph, are held to;
2. **against the RESTORED face at the ordinary limit (0.80)**: the step may not change the restored face more than any step may change its parent.

* *Only the first limit* would let a chain of steps drift anywhere between the restored face and 0.65 against the upload, with nothing between steps. The second limit keeps each step as close to its parent as steps normally are (tested: `test_a_step_that_changes_the_restored_face_is_refused_whatever_the_upload_says`).
* *Only the second limit* would let the drift accumulate step after step, each within 0.80 of the one before; the first limit bounds the total.
* The comparison reported in the verdict is the one with the least room (`similarity - limit`); a failure has a negative room, so it decides.

## The change

`RestoredBaseline` (`enhancement/adaptive/service.py`): once a `face_restoration` step is accepted, the engine embeds the restored faces once and swaps the validator's identity judge for `RestoredBaseline`, which asks the upload at the restoration limit and the restored face at the ordinary limit. `final_identity` still uses the ordinary `IdentityChecker` (the saved photograph is judged at the restoration limit against the upload, as before). The identity gate's verdict records which comparison decided (`baseline`: `upload` or `restored face`).

* **Setting:** `ADAPTIVE_BASELINE_AFTER_FACE_RESTORATION` (default `True`); `False` gives the earlier behaviour exactly.
* **Nothing changes for a photograph without an accepted face restoration**: the judge is swapped only after one is accepted.
* **An unavailable restored embedding is tolerated**: the restored faces may be too small or soft to embed; then only the first limit applies (tested).
* **An inconclusive comparison does not decide**: a comparison that could not be made says nothing and the other one decides (tested); if neither could be made the result is the upload's, as before.
* **Cost:** one more embedding pass over the restored faces, once per photograph, only when a restoration was accepted.

## The whole library, before and after (real models)

The library's 27 distinct originals with a face and at most 2 megapixels, `auto` / `natural` and `high` / `ai_max` (54 runs), through the whole engine with the new code, `ADAPTIVE_BASELINE_AFTER_FACE_RESTORATION` = `False` (the earlier judge) and `True`. The `True` run is made only when the `False` run accepted a face restoration, since the setting cannot matter otherwise: **44 of the 54 runs accepted none (unchanged by construction), 10 did (5 photographs, two requests each).**

| photograph | request | earlier judge | now | final identity (limit 0.65) |
|---|---|---|---|---|
| `be34b90c` | auto / ai_max | face restoration, detail | identical to the last pixel | 0.9688 / 0.9704 |
| `3c8fcf43` | auto / ai_max | face restoration, enlargement, detail | identical | 0.8751 / 0.8751 |
| `aa35bb77` | auto / ai_max | face restoration, detail / + motion deblur, enlargement | identical | 0.8978 / 0.8833 |
| `1cb4974b` | auto / ai_max | low light, face restoration, detail | identical | 0.8644 / 0.8644 |
| `67c29a14` | auto / natural | denoise, face restoration (448 x 296) | **+ enlargement + detail, 896 x 592** | 0.7873 |
| `67c29a14` | high / ai_max | the same five steps | the same five steps and size, **different pixels** | 0.7996 |

**8 of the 10 are identical, 2 differ (one photograph), no photograph lost an operation, and every final identity is verified.** The photographs whose final identity is high are untouched: the earlier judge was not the limiting one for them. The change reaches only the photographs whose restored face is between 0.65 and about 0.80 against the upload.

The `high` / `ai_max` difference: the earlier judge refused the first detail candidate at 0.7996 (limit 0.80, short by 0.0004) and kept the second (0.8046); the new judge accepts the first (0.7996 against 0.65). The two requests end with the same operations, so the difference is which candidate of the final layer was kept.

Looked at side by side with a plain enlargement of the earlier result, the new `auto` / `natural` output of `67c29a14` keeps the face (hairline, brow, eyes and mouth in place), which is cleaner (less compression blocking) and softer, and the branches are sharper; the foliage and the sky show some painterly smoothing from the detail network.

## Tests

`enhancement/test_baseline_after_face_restoration.py`, 20 tests: the judge on its own (both limits, a failure deciding whichever comparison has less room, an inconclusive comparison, an unavailable restored embedding) and the engine end to end with stub models (the enlargement follows the restoration with the baseline on and is refused for identity with it off, the upload limit still refuses a step that leaves the upload, a step that changes the restored face is refused whatever the upload says, the verdict records the baseline). Nine mutations of the new code were tried (the old upload limit, the restored face asked at the restoration limit, `max` instead of `min`, the judge never swapped, the setting ignored, the restored face not embedded, an unavailable restored embedding not tolerated, the baseline not recorded, an inconclusive comparison deciding): **all nine are caught.** Two things were corrected along the way: one mutation of the first draft was an equivalent mutant (the code it altered could not matter, so the code was simplified and the mutation replaced by `max` instead of `min`), and one survived because the test stub did not behave like the real checker when the restored embedding is unavailable (the stub now fails if it is asked).

## Not verified

* **The geometry gate is not changed.** After an accepted restoration, later steps are still judged against the ORIGINAL's landmarks (the placement inherited from the restoration is what the small-face fix, 2026-11-05, handles for enlargements); this change is about identity only.
* **The two similarities are not stored separately**: the verdict reports the comparison with the least room, so for an accepted step the other comparison is known only to have at least as much room.
* **Drift is bounded, not zero.** A later step can now leave the upload at 0.65 where it was refused below 0.80; that is the limit the restoration and the saved photograph already use, and the 10 final identities above are all well over it (lowest 0.7873), but the change allows what the earlier judge forbade.
* **One photograph carries the gain.** Five photographs of the library accepted a face restoration, one of them changed; this is not a proof for every photograph.
* **SFace only.** Another identity model needs its own restoration limit (`thresholds_for(provider)["restore_guard"]`).
* The photograph was looked at on one request (`auto` / `natural`); the `ai_max` output of the same photograph was not looked at.
