# FaceCloud AI Backend: Defense Readiness Report

Prepared 2026-10-15 for the 2026-10-20 defence. Branch `feat/focused-model-set`, Draft PR #72 (base `feat/lightweight-enhancement`, stacked on #71).
Nothing is merged or deployed. Implementation is frozen except for verified critical defects; one was found and fixed (section 5).

## 1. Continuous integration (PR #72)

All three jobs **passed** on the final code commit `5f5edc6` (GitHub Actions run 38761435937, 2026-10-15):

| Job | Result | Duration |
|---|---|---|
| Tests and schema (full suite, migrations check, OpenAPI warnings gate, ruff) | **pass** | 5 min 5 s |
| Dependency audit and static analysis (pip-audit, bandit) | **pass** | 27 s |
| Production configuration starts | **pass** | 35 s |

The earlier run on the PR's first commit also passed all three jobs. The only commit after `5f5edc6` is this report (documentation only).

Local, on the final code: **1,883 tests OK, 10 skipped** (real-model and moto tests skip when PyTorch, a checkpoint, moto or the optional
sample-photo folder is absent); `ruff` clean; OpenAPI schema valid with no warnings; no pending migrations; `bandit -ll` no medium/high findings.

## 2. Architecture in one page

```
POST /api/images/upload/single/ ──> standardise ──> POST /api/enhancements/images/{id}/  (202, queued job, claim/retry/ownership)
                                                          │
                              ENHANCEMENT_RENDER_POLICY=adaptive_v1 (opt-in; default lightweight_v1 unchanged)
                                                          v
  original ──> ImageQualityAnalyzer ──> Router ──> candidates (1-2 variants of ONE operation) ──> FinalImageQualityValidator
        ^        exposure on the face,    asked again       Retinexformer | NAFNet-SIDD |            gates: size, data consistency,
        │        blur typed motion/       after every       NAFNet-GoPro | Real-ESRGAN |             colour, halos, noise, identity;
        │        defocus, noise, faces    accepted step     CodeFormer (off by default)              prefers the smallest sufficient change
        └────────────── re-analyse the selected image ─────────────────────────────────────────────────┘
                                                          v
        GET /api/enhancements/results/{id}/  ──> status, enhancement_outcome, enhancement_summary, output size, download_url
        GET .../download/                    ──> the ONE final image (or the original pixels)
```
Storage is encrypted at rest (AES-256-GCM) and can be a private S3 bucket (`STORAGE_BACKEND=s3`, untested on real AWS). Details: `docs/enhancement/adaptive-engine.md`.

## 3. The API returns one final image and reports the outcome correctly (verified)

`scripts/verify_adaptive_api.py` pushed 6 photographs through the **real endpoints** (upload, enhance, poll, download) with the **real models**, in
production mode, on a throwaway database. Every case passed every check: HTTP 202 then `completed`; exactly **one** output file stored; the downloaded
image decodes at the reported size; **no debug or intermediate image stored**; an `original_preserved` result is **pixel-identical** to its input; the
engine took the same decision with debug mode on. **All three outcomes occurred**: `enhanced` (1), `partially_enhanced` (3), `original_preserved` (2).

| Case | Outcome | Summary the API returns |
|---|---|---|
| dark portrait | enhanced | "Enhanced: brightened the dark subject." |
| noisy portrait | partially_enhanced | "Enhanced: reduced noise. Not fully corrected: Noise remains." |
| motion-blurred portrait | partially_enhanced | "Enhanced: reduced motion blur. Not fully corrected: a degraded face remains that could not be restored safely." |
| dark and defocused | partially_enhanced | "Enhanced: brightened... Not fully corrected: ... Defocus blur cannot be recovered reliably." |
| clean photograph | original_preserved | "The photograph did not need enhancement, so it was left as it is." |
| badly motion-blurred, second person | original_preserved | "No enhancement passed the safety checks, so the original photograph was kept." |

## 4. Three demonstrations, measured against the clean photograph

(Synthetic degradation of real phone photographs; side-by-side images: `~/FaceCloud_defense_demo/defense_demonstrations.jpg`, local only.)

| Demonstration | Models that executed | PSNR | SSIM | LPIPS (lower is better) | ArcFace / SFace to the clean face |
|---|---|---|---|---|---|
| **Dark portrait** (enhanced) | Retinexformer | 12.7 -> 16.1 | 0.41 -> 0.78 | 0.44 -> 0.31 | 0.94 -> 0.95 / 0.92 -> 0.92 |
| **Motion-blurred portrait** (partially enhanced) | NAFNet-GoPro | 32.6 -> 40.6 | 0.92 -> 0.97 | 0.30 -> 0.17 | 0.77 -> 0.98 / 0.81 -> 0.99 |
| **Fallback**: badly blurred second portrait (original preserved) | NAFNet-GoPro ran, both candidates rejected | unchanged | unchanged | unchanged | unchanged |

Other measured cases: noisy portrait PSNR 23.4 -> 24.6 (SSIM 0.65 -> 0.72); dark and defocused 12.7 -> 23.8 (identity similarity fell 0.87 -> 0.81, see section 6).
Times: 0.7 s (clean) to 9 s per photograph at 640 px on an 8 GiB M1.

**Visual honesty:** the dark-portrait result is brighter and sharper in structure but **paler in colour** than the original (the brown shirt turns grey-brown);
PSNR rewards it, a person may not. No one has judged these outputs by eye except the author.

## 5. Safe fallback, and the one defect found while verifying

* **Fallback behaviour (measured):** on the second portrait the deblurring model ran and **both** candidates were rejected (`halo_or_ringing`, `face_geometry_changed`,
  `identity_changed`), so the user receives the original, byte for byte, with an honest sentence. Clean photographs return identical in 0.1-0.7 s. An earlier
  benchmark run also showed a blurred photograph wrongly lifted as "low light"; that was fixed before this report.
* **Defect found by the API verification and fixed (commit 5f5edc6):** the outcome logic counted whatever the analyzer found in the *result* as unresolved. A brightened
  photograph reads as smoother, was flagged as defocus blur, and the summary told the user defocus could not be fixed on a photograph that was never blurred; `enhanced`
  was unreachable. A defect is now unresolved only if the **original** had it. Tests added.

## 6. Unresolved identity limitations (the main risk)

* **Face restoration (CodeFormer/GFPGAN) is OFF by default** and the engine reports it as skipped. Do not claim face restoration in the demonstration.
* The identity gate can only compare a result with the **degraded input**. In the benchmark it saw 0.85-0.91 where the similarity to the true clean face had dropped by
  0.07-0.13. It cannot observe that drift, and the 0.70 ArcFace threshold rests on five data points; raising it alone is not a guarantee.
* When the original face is too dark to embed (the dark cases), identity **cannot be verified** for the lift; in the dark-and-defocused case similarity to the clean
  face fell 0.06-0.08 and the engine could not check it.
* The two identity models can disagree about the same restoration (SFace up, ArcFace down). One embedding model is evidence, not proof.

## 7. What is implemented and verified, and what is not

| Verified (with evidence) | Implemented but NOT validated | Not implemented |
|---|---|---|
| One-image API contract and all three outcomes, real models, real endpoints | Face restoration quality and identity safety (off by default) | Frontend Auto Enhance screen (deliberately untouched) |
| Clean photographs untouched; unsafe candidates rejected; original returned | Behaviour on other skin tones, ages, cameras, real (not synthetic) blur and noise | Any CUDA run |
| Retinexformer, NAFNet-SIDD and GoPro, Real-ESRGAN, CodeFormer, GFPGAN, ArcFace, RetinaFace each ran on real photographs (model sheet, benchmark) | Multi-face photographs through real models (stand-in tests only) | Per-user storage quotas |
| Ownership checks, one stored output, no debug leakage | Private S3 storage on a real AWS bucket (simulated S3 only) | A real-noise (not synthetic) denoise evaluation |
| 1,883 tests, CI, lint, schema, security scan | OpenAI advisor (never called the real service) | A human visual evaluation |
| Model sheet, calibration, benchmark with PSNR, SSIM, LPIPS | Large-image (multi-megapixel) throughput and memory | |

Models **executed in the three-case demonstration:** Retinexformer, NAFNet-SIDD, NAFNet-GoPro. **Not executed there:** Real-ESRGAN (not needed), CodeFormer and GFPGAN
(off), Zero-DCE (off, a fallback), RetinaFace (opt-in; YuNet detects by default). All of them ran elsewhere (model sheet, `docs/evaluation/2026-10-15-model-sheet.md`).
SFace gated identity in the demonstrations; ArcFace measured it.

## 8. Licences

Academic use only as configured: CodeFormer (S-Lab), ArcFace/InsightFace weights and Zero-DCE are non-commercial. The Zero-DCE and GFPGAN permission documents are not in the
repository. See `docs/model-licenses/model-set-2026-10.md`.

## 9. What to say, and what not to say, on October 20

Say: it analyses a photograph, runs only the models its defects call for, validates every result against quality and identity checks, returns one image, and **returns the
original when it cannot improve it safely**; it reports honestly which defects remain. Show: dark (enhanced), motion blur (large measured gain), and the preserved fallback.
Do not say: that identity is guaranteed, that every photograph improves, that blur and low resolution are solved, that it is production-ready, or that the S3 or advisor paths
were tested against the real services.

## 10. Before merging (not done, needs your approval)

Back up the database; merge in order **#67 -> #68 -> #71 -> #72**, retarget #72 to `main` after #71; run `manage.py check_storage` on a real bucket if S3 is to be used;
frontend work by its owner from `docs/enhancement/adaptive-engine.md` section 7.
