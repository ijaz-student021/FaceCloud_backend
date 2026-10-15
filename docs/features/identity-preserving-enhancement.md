# Identity-preserving enhancement

## Status

Implemented and enabled by default (`IDENTITY_GUARD_ENABLED=True`). It only
ever reduces enhancement strength on faces it can judge, needs no new model,
and skips with a recorded reason whenever it cannot judge.

## What it does

After every other stage, the final image's faces are compared with the
source's faces using the same YuNet detector and SFace embeddings that
identity grouping (Feature 18) already uses:

1. Detect the largest faces in the source (up to `IDENTITY_GUARD_MAX_FACES`),
   ignoring faces smaller than `IDENTITY_GUARD_MIN_FACE_EDGE` px or darker than
   `IDENTITY_GUARD_MIN_FACE_LUMINANCE`.
2. Embed them, then detect and embed the faces in the enhanced image (compared
   on the source pixel grid, so upscaled outputs are handled).
3. Match faces by box overlap; the **worst** matched face decides.
4. If similarity is at least `IDENTITY_GUARD_MIN_SIMILARITY` (default `0.80`),
   the result is untouched. Otherwise it is blended back toward the source
   along `IDENTITY_GUARD_STRENGTHS` (`1.0, 0.85, 0.7, 0.55, 0.4`) until the
   check passes. This retry is cheap: no enhancement model runs again.

The outcome is recorded in `applied_parameters.identity_guard`: `status`
(`preserved`, `strength_reduced`, `unresolved`, `inconclusive`, `skipped`,
`disabled`), the similarity at every attempt, the final strength, and the
threshold. `unresolved` (no strength met the threshold) also adds a warning.

## Measured on real photos

Similarity between source and enhanced faces, using the production detector and
SFace model:

| Photos | Similarity |
|---|---|
| Well-lit portraits, natural / dynamic style | 0.96 - 0.999 |
| Same portraits, AI Enhance (heaviest remaining variant; measured earlier with PureVision, since retired) | 0.94 - 0.98 |
| A different person's face pasted into a photo (forced identity change) | 0.15 |
| Very dark night frames (face luminance 6-22 / 255), faithful brightening only | 0.45 - 0.87, and 3 of 12 faces were not re-detected at all |

The default of `0.80` sits well above the drift seen on genuine enhancement of
well-lit faces and well below a different identity. It is close to the
identity-grouping threshold (`IDENTITY_MATCH_THRESHOLD`, 0.82) on purpose: an
enhanced face should still be recognisable as the same person by the rule that
groups people. **Neither threshold is calibrated on a consented evaluation set
yet** (Feature 18 / T-001); the similarity of every attempt is stored so it
can be calibrated later.

## Known limits (by design)

- **Very dark faces are not judged.** SFace embeddings of a barely visible
  face are unreliable, so a faithful brightening scores low without any
  identity change. Judging them would make the guard weaken correct
  enhancement on exactly the low-light photos this project targets. Matching
  the source's exposure to the enhanced image before comparing only raised the
  mean from 0.49 to 0.53, so it does not fix this. The result is recorded as
  `skipped` ("source face is too dark").
- **A face that cannot be re-detected is inconclusive, not a failure.** On
  grainy brightened frames the detector routinely misses a face that is
  visibly there; treating that as identity loss would force the weakest
  enhancement. The image is left unchanged and `status` is `inconclusive`.
- **The ladder floor is 0.4.** A completely different face (the forced test)
  is detected (similarity 0.15) but cannot be reconciled by blending alone: the
  result is `unresolved` with a warning.
- **Cost:** about 1.1 s on a photo with a judged face, about 55 ms when
  skipped. Almost all of it is the existing SFace provider re-hashing its
  38 MB model on every call; caching that check would cut it.

## Settings

`IDENTITY_GUARD_ENABLED`, `IDENTITY_GUARD_MIN_SIMILARITY`,
`IDENTITY_GUARD_STRENGTHS`, `IDENTITY_GUARD_MAX_FACES`,
`IDENTITY_GUARD_MAX_EDGE`, `IDENTITY_GUARD_MIN_FACE_EDGE`,
`IDENTITY_GUARD_MIN_FACE_LUMINANCE`.
