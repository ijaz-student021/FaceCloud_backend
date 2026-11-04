# Face restoration: is there a better model, and why most restorations were thrown away (2026-11-04)

**The owner's question.** "Any upgrade model to get the best enhanced face?" The pipeline restores a degraded face with CodeFormer; on the owner's photographs it rarely showed.

## 1. Is there a clearly better model? No (survey, read from the repositories on 2026-11-04; nothing downloaded or run)

| candidate | code licence | weights licence | size | fits an 8 GB M1? | verdict |
|---|---|---|---|---|---|
| CodeFormer (in use) | S-Lab 1.0, non-commercial | same | 377 MB | yes | keep |
| GFPGAN v1.4 (installed, off) | Apache-2.0 + StyleGAN2 third-party terms | not stated (private permission on record) | 349 MB | yes | cannot be run here: its `basicsr` / `facexlib` packages are not installed and pin old torch; not measured |
| RestoreFormer++ / VQFR v2 | Apache-2.0 | not stated | 334 MB (VQFR) | likely | no identity advantage published; stale dependency pins |
| GPEN | no licence file now (the non-commercial line was deleted 2026-03-10) | none stated | unverified | CPU per README | not usable without a licence |
| DifFace / PGDiff | S-Lab, non-commercial | same | 640 MB | slow (100-250 steps) | not for a laptop |
| PMRF (ICLR 2025) | MIT | MIT card, but its bundled SwinIR stage is DifFace's (non-commercial) | 923 MB | unverified; its `natten` is CUDA-oriented and `inference.py` hard-codes `.cuda()` | a post-defence spike on a GPU |
| DiffBIR v2.1, OSDFace, OSEDiff, InstantIR, SUPIR, HYPIR | various | various | 1.4-16 GB | no | too large or fragile; diffusion also adds hallucination risk |

Published numbers (each paper's own CelebA-Test, **published, not measured by us**, never comparable across papers): the identity-degradation gaps among CodeFormer, VQFR, DifFace and DiffBIR are small in one paper (OSDFace Table 1: CodeFormer 62.20, DiffBIR 61.02, OSDFace 60.07) and the same method scores 62.2 in one paper and 37.3 in another. No candidate has an M1 timing. The cheapest improvements need no new model.

## 2. What the knob does (CodeFormer fidelity `w`; higher keeps more of the input), on the owner's photographs

6 of the owner's photographs with a main face of at least 100 px (1000 px long edge), degraded as `scripts/demo_set.py` does, CodeFormer at each `w`; identity = SFace similarity to the CLEAN face, detail = Laplacian variance of the face crop relative to the clean face (1.0 = as sharp). The upload's own score is the "degraded" row.

| degradation | degraded | w 1.0 | w 0.95 | w 0.85 | w 0.7 | w 0.65 | w 0.5 | w 0.3 |
|---|---|---|---|---|---|---|---|---|
| blur: identity / detail | 0.73 / 0.03 | **0.79 / 0.39** | 0.78 / 0.39 | 0.77 / 0.38 | 0.74 / 0.37 | 0.73 / 0.37 | 0.71 / 0.37 | 0.67 / 0.37 |
| motion | 0.61 / 0.21 | **0.68 / 0.52** | 0.68 / 0.51 | 0.67 / 0.50 | 0.66 / 0.49 | 0.66 / 0.48 | 0.65 / 0.47 | 0.62 / 0.46 |
| small | 0.97 / 0.36 | **0.94 / 0.77** | 0.93 / 0.76 | 0.92 / 0.74 | 0.91 / 0.71 | 0.90 / 0.70 | 0.86 / 0.67 | 0.80 / 0.64 |

A lower `w` bought no more detail and lost identity at every step; blending 50 % of the input back kept identity about where `w` 0.85 had it and halved the detail (blur 0.12). The router used to try 0.85 and then 0.65, and every attempt costs a CodeFormer pass (about 2 s on an M1 GPU, about 8 s on a CPU).

## 3. Why most restorations never reached the owner

In the whole engine (6 photographs, blurred copy and motion-blurred copy, 20 face candidates) **18 were refused, and 17 of those for `identity_changed`**: the restored face is compared with the face in the UPLOAD (SFace similarity limit 0.80), and a blurred upload's own embedding is poor, so a faithful restoration scores 0.58-0.72 against it. Measured on the same 24 restorations per `w` (similarity to the upload, which the gate sees, against similarity to the clean face, which it never has):

| gate limit | `w` 1.0: accepted | accepted but WORSE than the upload (clean-similarity lower by more than 0.05) | refused although at least as close to the clean face as the upload | `w` 0.85: accepted / worse / refused-good |
|---|---|---|---|---|
| 0.80 (before) | 15 of 24 | 1 | **9** | 14 / 3 / 10 |
| 0.70 | 20 | 1 | 4 | 19 / 3 / 5 |
| **0.65 (now)** | 22 | 1 | 2 | 22 / 3 / 2 |
| 0.60 | 24 | 1 | 0 | 24 / 3 / 0 |

At `w` 1.0 the restored face was at least as close to the clean face as the upload in 19 of 24 (blur: 0.79 against 0.73; motion 0.68 against 0.61); no limit between 0.5 and 0.8 caught more bad ones than any other, so the limit only refused good results. The same shape on 12 people of LFW (24 photographs, `scripts/evaluate_identity_drift.py`, 432 restorations across `w` 0.5 / 0.65 / 0.85): accepted-but-not-fine 68 at a limit of 0.40, 62 at 0.80, 0 only at 0.90, where 201 fine results are refused.

## 4. What changed

* `SFACE_RESTORE_GUARD_MIN_SIMILARITY` (default **0.65**; 0.80 restores the earlier behaviour): a face restoration, and the saved photograph's identity state when one was applied, is held to this limit; every other operation keeps 0.80. It is never stricter than the guard, and another embedding model (ArcFace) keeps its own guard. An unverifiable restoration (no embeddable upload face: small, dark) is still never accepted.
* `ADAPTIVE_FACE_FIDELITIES` (default **1.0**, was 0.85 then 0.65): every value costs a pass and a second attempt never rescued a refusal in these runs.

Whole engine, natural / auto, the same 6 photographs (single runs): restorations kept **blur 1 -> 2 of 6, motion 0 -> 2 of 4**; identity against the clean face unchanged (blur 0.754 -> 0.755, motion 0.732 -> 0.732); the face step 3.2 s -> 2.0 s and the whole run 15.8 s -> 13.8 s (blur). The other gates (face geometry, "face not clearer") still refuse the rest; noise copies never reach the face step.

## Not verified

* **6 photographs, possibly fewer distinct people, 24 pairs per `w`.** SFace similarity is evidence, not proof (it cannot see a changed expression or skin); the "worse" count of 1 comes from one pair. The 0.65 limit is read from the data, not proven safe for every face.
* GFPGAN was not measured (its package is not installed); no new model was tried, so the survey above is licence and published-number reading only.
* The face-detail number is a crude sharpness ratio; the sheets were looked at for four photographs only. Not run on a real host, not run on a CPU.
