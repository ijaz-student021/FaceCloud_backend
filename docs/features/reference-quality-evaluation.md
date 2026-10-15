# Reference-quality evaluation

`scripts/evaluate_reference_quality.py` answers a question that comparing configurations on unlabelled photos
cannot: does enhancement recover the *original* face, and only that face? It takes a
clean, undamaged face from a reference dataset, damages it in a controlled way
(darkens, blurs, shrinks), runs it through the real pipeline, and checks whether the
result still matches the untouched original — and, separately, whether two different
people are clearly less similar than that. See its module docstring for full usage.

```powershell
python scripts/evaluate_reference_quality.py --count 40
```

**Biometric data handling:** face embeddings are computed only in memory to produce a
similarity *score*; the tool never writes an embedding vector to disk, to
`results.jsonl`, or anywhere else. A test (`config/test_reference_quality.py`,
`NoBiometricDataPersistedTests`) asserts every persisted record contains only plain
JSON-safe values.

**Why it measures the enhanced output against the clean original directly, not the
pipeline's own internal identity guard's readout:** the guard only ever compares the
final output to whatever was *fed into* `enhance_bgr` — for a darkened photo, that is
the dark image, not the clean original — so on a badly degraded input it reports "too
dark to judge" even when the final, brightened output is perfectly judgeable against
the real reference. This tool detects and embeds the output fresh and compares it to
a reference embedding computed once per photo before degrading it. The pipeline's own
guard readout is still recorded per record (`pipeline_guard`), as a diagnostic, not
the headline number.

## Measured result (2026-09-28, CPU, N=6, `Dataset/generated_photos` faces)

A first small run (`--count 6 --severities clean,dark,dark_blur`) found, consistently
across all 6 photos:

| Severity | Same-person mean similarity | Same-person min similarity | Identity judged |
|---|---|---|---|
| `clean` (no damage) | 0.9548 | 0.9397 | 6 / 6 |
| `dark` (darkened only) | — | — | **0 / 6 — no face detectable in the output** |
| `dark_blur` (darkened + blurred) | 0.3795 | 0.2393 | 6 / 6, **all below the 0.80 threshold** |

Different-person baseline (15 pairs of clean, undamaged different faces): mean
similarity 0.3351, highest seen 0.5289 — so on `dark_blur`, several of the "same
person" results scored close to or below the highest score seen for two *different*
people. Safety margin (lowest same-person similarity minus highest different-person
similarity): **-0.2896 (negative — no margin)**.

**What this does and does not show.**

- It is a real, consistently reproduced result (6/6, not one outlier photo), not
  noise, and it's exactly the kind of failure this tool exists to catch: today's
  identity guard, running with every heavy restoration stage disabled, does not
  reliably preserve identity through this style of severe darkening.
- **It is not proof the shipped pipeline is unsafe for real users.** The current
  pipeline already refuses to fully trust its own output when it cannot verify a
  face (`identity_guard`'s `skipped`/`inconclusive` states, which are why
  `apply_identity_guard` blends destructive candidates back toward the source rather
  than shipping them outright) — the missing piece this result points at is that a
  face too degraded for automatic re-verification is a case the lightweight pipeline
  does not try to repair: the retired restoration models (GFPGAN, PureVision, HD
  restoration) were the only thing that could have helped, and generative face
  restoration was retired because it cannot be shown to keep the person.
- **N=6 is small.** Treat these exact numbers as a first signal, not a final
  measurement. A larger run (`--count 40`+) firms this up; at roughly 40-55 seconds
  per photo per severity on this CPU (that timing was measured with the learned
  stages on; the classical pipeline is much faster), a large run is realistic overnight.
- **The synthetic darkening (multiply + Gaussian noise) is cruder than a real camera's
  low-light noise**, and its specific high-frequency grain pattern may be part of why
  `dark` (no blur) failed detection more often than `dark_blur` (the incidental
  smoothing from blur seems to make the face edges *easier* to detect than the raw
  grain does) — a real interaction worth understanding, not necessarily representative
  of every real dark photo.
- **"No face detectable" is a stricter bar than "unrecognisable to a person."** The
  identity model's own confidence gates may reject a face a human would still clearly
  recognise; this tool measures what the automated guard can verify, not human
  perception.

**Update 2026-10-15:** the restoration stages this section proposed re-running
(PureVision, HD restoration, GFPGAN) were retired instead
([retired-capabilities.md](../enhancement/retired-capabilities.md)). Re-run the tool on
the lightweight pipeline to refresh the numbers.
