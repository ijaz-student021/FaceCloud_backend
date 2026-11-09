# Why a small sunset photograph came back "not enhanced" (2026-11-04)

**The owner's report.** A 452 x 300 sunset photograph with a silhouetted woman (`4.jpg`, result 123, natural / auto) came back with "Enhanced: refined the fine detail ... The photograph is small and could not be enlarged safely", the warning "HD restoration is not available on this server", and no visible change.

## What the stored result says

* The analyzer found `low_resolution`, `compression_artifacts` and `face_too_small`, and recommended `super_resolution`.
* `super_resolution` (Real-ESRGAN x2) was **rejected for `face_geometry_changed`**, not unavailable. Only the final layer (the compact detail model, blend 0.7) ran afterwards: the output differs from the upload by **1.8 of 255** on average, which is why nothing could be seen.
* The "face" is the silhouetted head: box 43 x 66 px, quality `too_small` (the analyzer's limit is 48 px). On the enlarged candidate its five landmarks moved by **0.1236 of the face width (5.3 px of the original)**; the limit for an enlargement is 0.06 (2 px of floor on top, for jitter). On a silhouette the landmarks are guesses, so the check measured the detector, not the picture. (A 21 px rooftop silhouette had the same problem earlier and got the 2 px floor; a bigger silhouette needs more than a bigger floor.)

## The change

The face-geometry gate ignores a face the analyzer calls `too_small` for every operation except face restoration (`ADAPTIVE_GEOMETRY_IGNORES_TINY_FACES`, default on; `False` restores the earlier behaviour; the verdict records `tiny_faces_not_checked`). Such a face is not restored and its identity cannot be checked anyway. A face that is large enough, in the same photograph or alone, is held to the same limits as before; face restoration is never relaxed; a photograph whose only face is tiny has nothing left to check.

## Measured

* The sunset photograph, natural / auto: before `detail_restoration` only, outcome `partially_enhanced`, output 452 x 300; after `super_resolution` + `detail_restoration`, outcome `enhanced`, output **904 x 600**. At 100 % the enlarged version has sharper cloud edges and hair line where a bicubic x2 of the upload is soft (viewed side by side for two crops).
* The owner's whole library, 46 distinct originals, natural / auto, whole engine, setting off against on: **one output changed (this photograph); the other 45 are identical.**

## Not verified

* **One photograph is the only one affected**, so the effect on photographs with a tiny real face (a distant person in a group) is untested in the library; the enlarger's blend (REALESRGAN_OUTPUT_BLEND 0.35) keeps structure, and a distorted tiny face would show in the viewing, not in a gate. The setting undoes it.
* The server's reason text was honest ("Restoration was needed but not completed: upscale (the result did not pass the safety checks: face_geometry_changed)"); the **frontend replaces it with a fixed sentence**, "HD restoration is not available on this server" (`src/components/features/EnhancementReport.tsx:114` of the frontend repository, not edited here), which is wrong for a refused enlargement: it should show `hd_unavailable_reason`.
* The other part of "no effect": when the final layer is the only accepted step a photograph can change by under 2 of 255. That policy is unchanged here.
