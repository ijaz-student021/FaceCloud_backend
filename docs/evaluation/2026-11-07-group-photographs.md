# Group photographs: what the engine does with many faces (2026-11-07)

A brief for "the wedding photograph edge case" said: a photograph of 10 to 50 faces makes the Pro preset run CodeFormer on every face (5 to 15 minutes, out of memory), the backend has a 240 s `ENHANCE_TIMEOUT_MS`,
low-light group photographs are refused because `detail_ratio_vs_bicubic < 1.0`, and proposed a 12-face cap, an "all faces fall back if one does" mode, a 480 s timeout and a per-face progress line. This note is what was
measured and what was changed. Real models (YuNet, NAFNet, CodeFormer, SFace, UltraSharp) on an Apple M1 (MPS); the engine called directly (`AdaptiveImageEnhancementService.enhance`, Pro = `high` / `ai_max`, the production
time budget of 180 s), **not** through the API or the UI. **No real group photograph was available** (the owner's library has no photograph with more than 3 faces; the sandbox has no internet), so the groups are synthetic:
portraits from the owner's library tiled on a canvas, softened with a Gaussian blur, the same person repeated (mirror images for the second half) with faces of equal size.

## 1. What the premises got wrong

* **There is no backend `ENHANCE_TIMEOUT_MS`.** The 240 s in the brief is the frontend's wait: `UploadSingle.tsx` (160 polls of 1.5 s), `batchEnhance.ts:52` and `liveSnapshot.ts:72` (`Date.now() + 240_000`), all counted from the
  request, so the queue is included, and the dev worker runs one job at a time (`--concurrency=1`, solo pool). The backend's own limits: the engine's time budget `ADAPTIVE_TIME_BUDGET_SECONDS` (180 s; it also stops a
  model between two tiles: `runtime.deadline_until`), the Celery soft/hard limits (3300 s / 3600 s).
* **CodeFormer does not run on every face.** Three caps, all five: `CODEFORMER_MAX_FACES` (the largest first), the analyzer's `max_faces` and `IDENTITY_GUARD_MAX_FACES`. The face step cost 6.0 to 7.5 s for 5 faces on canvases of
  8, 16 and 32 faces (up to 1 MP; on the 48-face canvas no face was large enough to judge), 13 to 14 s on a 2.2 MP canvas of larger faces.
* **No out-of-memory.** Peak resident memory of the process: 1.1 GB (up to 48 faces, 1.6 MP), 1.15 GB (12 faces, 12 MP), 1.28 GB (24 faces, 12 MP).
* `detail_ratio_vs_bicubic` is the super-resolution gate, not NAFNet's. The deblur gates are `face_not_clearer_or_over_sharpened` and `edges_not_restored_or_over_sharpened`.

The cost of a group photograph is its **pixels**, not its faces: on a 12 MP canvas (4000 x 3000, faces of about 190 px) NAFNet took 88 to 112 s of a 101 to 119 s run, the face step did not run at all ("No face large
enough to judge was found in the source": the identity guard reduces the photograph to a long edge of 1024 px and judges faces of 40 px and more, so a face under about 4 percent of the long edge is not judged) and the detail
step was skipped (over 4 MP).

The owner's 171 timed results (live database): median 15 s, 90th percentile 82 s, 95th 132 s, longest 284 s (result 81, 3 November, an Auto/Natural photograph); 10 over 120 s, 4 over 180 s, 1 over 240 s. So the
frontend's 240 s is exceeded now and then even by a single photograph, and the screen then reports "did not finish" although the job goes on.

## 2. What was wrong: `face_lost` on a group photograph

On the group canvases the engine refused candidates with `face_geometry failed (face_lost). landmark_shift=None`. `landmark_shift` matches each of the original's faces to the nearest face of the candidate's report; the
original's report keeps its five largest faces and the candidate's report keeps **its** five largest, so among faces of nearly the same size (a row of a group photograph) the two sets differ without any face being
lost, and a face that is the sixth largest in the candidate is "no longer found".

Experiment (`YuNet` only): six harmless candidates of each group canvas (a 0.5 px and a 1 px blur, an unsharp mask, a JPEG pass at quality 80, a 5 percent brighter and a 6 percent more contrasty copy: no face moves or
disappears), the validator's own `landmark_shift`, with the candidate's five largest faces and with every face the detector found:

| canvas | faces | `face_lost` with the five largest | with every detected face | landmark shift with every face |
|---|---|---|---|---|
| 1800 x 1200 | 8 | 0 of 6 | 0 of 6 | 0.0007 to 0.0108 |
| 1800 x 1200 | 14 | 3 of 6 | 0 of 6 | 0.0015 to 0.0073 |
| 4000 x 3000 | 12 | 2 of 6 | 0 of 6 | 0.0004 to 0.0023 |
| 4000 x 3000 | 24 | 6 of 6 | 0 of 6 | 0.0004 to 0.0023 |
| | | **11 of 24** | **0 of 24** | limit 0.04 |

Faces of equal size are the worst case for this (the order among them is decided by a pixel of box width), so the rate on a real group photograph is not known; it is not zero where faces of a row are within a few
percent of each other.

## 3. The change

`QualityReport.other_faces` (not stored, not counted: `face_count` and `as_dict()` are unchanged) keeps the faces the detector found beyond the report's five; the observations were made anyway, so it costs nothing.
`_identity_gate` asks for them **only when the strict match reports a face lost** (`ADAPTIVE_MATCH_FACES_BEYOND_REPORT`, default True, False restores the earlier behaviour): the original's faces are then matched against
the report's faces and the others together. Every limit is unchanged (0.04, 0.06 for an enlargement, 0.02 for polish, the strict 0.04 for a face restoration, the identity embedding and its thresholds); a measured shift over the limit
is still refused (`face_geometry_changed`); a face that is gone from every detected face is still `face_lost`; a measured shift is never replaced. 15 tests (`enhancement/test_adaptive_group_faces.py`), 14 mutations of the
change all caught; the `enhancement` and `config` suites: 1727 tests OK, 41 skipped.

## 4. The same canvases, the switch off and on (real engine, Pro, 180 s budget)

| canvas | switch | wall | what was accepted | what was refused |
|---|---|---|---|---|
| 14 faces, 1800 x 1200 | off | 112 s | deblur (21 s), detail (82 s) | **face restoration** |
| | on | 46 s | deblur (23 s), **face restoration (9 s)**, detail (8 s) | |
| 8 faces, 1800 x 1200 | off | 133 s | deblur (24 s) | **face restoration**, detail (97 s, both variants) |
| | on | 41 s | deblur (19 s), **face restoration (10 s)**, detail (7 s) | |
| 24 faces, 4000 x 3000 | off | 128 s | polish (4 s) | **deblur (115 s)** |
| | on | 110 s | **deblur (106 s)** | |

One run of each (the same canvas gave 176 s and 112 s with the switch off on two occasions: a refusal that depends on which of several equal faces the detector puts fifth is not repeatable). With the switch on
the face restoration is applied to the five largest faces, each kept only if it passes the per-face identity check (`_per_face_recomposition`): in the 14-face picture those five are visibly crisper than the nine that
are not restored, which stay as the deblur and detail steps left them. That difference is the price of the cap of five.

## 5. What was not done, and why

* **A 12-face cap for Pro** (the largest 12 restored): the cap is already 5 in the three places above; raising it to 12 would only add cost (CodeFormer 1.2 to 2.9 s a face here) and needs the analyzer and the identity
  guard raised with it.
* **"One face falls back, all fall back"**: the candidate is already judged as a whole by the geometry gate (a lost face refuses the whole face restoration), and each restored face is checked on its own against the
  identity limit; an all-or-nothing identity rule would discard faces that were verified because another was not.
* **A per-face progress line**: the API reports `queued`, `processing`, `completed`; there is no per-face progress to show, and a made-up one would be untrue.
* **A 480 s timeout for photographs of more than 6 faces**: there is no such backend setting; the engine stops at its own budget. The place to change is the waiting screens (frontend, the three constants in section 1); the backend needs nothing for it.

## 6. Limits of this evidence

Synthetic canvases (one person repeated, faces of equal size, no real wedding photograph); one machine (M1, MPS); one run per cell above except where stated; the engine called directly, not through the API.
