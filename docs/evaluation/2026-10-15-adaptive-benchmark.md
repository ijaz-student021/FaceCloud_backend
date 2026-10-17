# Adaptive pipeline benchmark, 2026-10-15

`python scripts/benchmark_adaptive.py --images <3 phone photographs> --max-edge 640 --out ...`: the *whole* adaptive engine (analysis, routing,
candidates, validation, selection) on 3 photographs x 7 degradations, scored against the clean photograph. Apple M1, 8 GiB, MPS, one model
resident at a time, SFace identity gate. LPIPS is AlexNet-based (lower is better). Synthetic degradations, 3 photographs: a demonstration,
not a proof. The photographs are private and are not in the repository.

3 photographs x 7 categories, synthetic degradations, scored against the clean photograph.

| Category | Runs | Outcomes | PSNR before -> after | SSIM before -> after | LPIPS after | Identical to input | Seconds (median) | ArcFace / SFace similarity after |
|---|---|---|---|---|---|---|---|---|
| clean | 3 | {'original_preserved': 3} | 361.202 -> 361.202 | 1.0 -> 1.0 | 0.0 | 3/3 | 0.2 | 1.0 / 1.0 |
| dark | 3 | {'partially_enhanced': 1, 'enhanced': 2} | 9.63 -> 16.399 | 0.374 -> 0.795 | 0.29 | 0/3 | 4.0 | 0.862 / 0.881 |
| noisy | 3 | {'enhanced': 3} | 23.28 -> 26.522 | 0.714 -> 0.84 | 0.422 | 0/3 | 8.0 | 0.824 / 0.841 |
| motion | 3 | {'partially_enhanced': 1, 'original_preserved': 2} | 27.545 -> 29.26 | 0.825 -> 0.839 | 0.331 | 2/3 | 9.9 | 0.561 / 0.537 |
| defocus | 3 | {'partially_enhanced': 2, 'original_preserved': 1} | 28.606 -> 28.904 | 0.829 -> 0.844 | 0.383 | 1/3 | 3.4 | 0.714 / 0.742 |
| lowres | 3 | {'partially_enhanced': 2, 'original_preserved': 1} | 31.265 -> 30.992 | 0.905 -> 0.904 | 0.305 | 1/3 | 3.1 | 0.77 / 0.821 |
| face | 3 | {'partially_enhanced': 1, 'original_preserved': 2} | 29.487 -> 29.549 | 0.866 -> 0.866 | 0.346 | 2/3 | 3.3 | 0.76 / 0.78 |

Peak resident memory of the process: 1726.0 MB. Errors: none.

Reading it: `clean` should be identical to its input (nothing was changed that did not need to be); `defocus` should be preserved or partial, never claimed fixed; PSNR/SSIM are blunt (see the module docstring).

## What it shows

* **Clean photographs are left alone**: 3 of 3 identical to the input, in 0.1-0.2 s. (An earlier run wasted 13 s trying and rejecting a
  super-resolution on a clean photograph because its face was small; the router no longer proposes that.)
* **Dark and noisy photographs are enhanced with identity intact** (mean ArcFace similarity to the clean face afterwards 0.86 for dark, 0.82 for noisy):
  PSNR 9.6 -> 16.4 and 23.3 -> 26.5, SSIM 0.37 -> 0.80 and 0.71 -> 0.84.
* **Motion blur**: one photograph improved a lot (PSNR 32.6 -> 37.7, identity similarity 0.77 -> 0.91); on the other two every candidate was rejected
  and the originals were returned (outcome `original_preserved`). That is the safe fallback working, not a failure.
* **Defocus and low resolution** are not fixed (PSNR change within +-0.5 dB); the engine says so (`partially_enhanced` or `original_preserved`).
* **Face restoration is the risky one.** It was accepted in 5 of the 18 degraded runs and, in the four cases where nothing else explains the change, moved the face away from the *clean* photograph by 0.07-0.13
  in ArcFace similarity (e.g. 0.94 -> 0.81); the exception is the motion-blur case, where it brought the face closer (0.77 -> 0.91) while the gate, which can only see the degraded input, measured 0.85-0.91 against a 0.70 threshold. Making the
  gate ArcFace instead of SFace changed no decision on these photographs. **A gate cannot protect against drift it cannot observe**, and the 0.70 threshold is
  uncalibrated (five data points). Anyone who needs strict identity guarantees should raise `ARCFACE_GUARD_MIN_SIMILARITY`, or leave face restoration off.

## Problems this benchmark found, and what was done

| Found | Cause | Fix |
|---|---|---|
| A blurred copy of a well-exposed night photograph was brightened (PSNR 24.4 -> 18.1, identity 0.2) | blur narrows a face's tonal range, which read as under-exposure | a subject is only low-light when the frame is dark too (median < 90); a dark face in a bright frame is `backlit_subject` and left alone |
| 13 s wasted on a rejected 2x upscale of a clean photograph | any small face triggered super-resolution | only a *poor* small face does |
| First real low-light run crashed | the chroma helper returns a dict, not a number | fixed, covered by a real-model test |
| Runs hung or took 100 s | 8 GiB machine at 18 GiB of swap, all models resident | one large model resident at a time on small machines (`ENHANCEMENT_MODEL_RESIDENCY`) |

## Not verified

CUDA; photographs of other skin tones, ages and cameras; real (not synthetic) blur and noise; multi-face photographs through the real models (covered by stand-in tests only);
any human judgement of the results; GFPGAN inside the engine (it is off in this benchmark; CodeFormer is the face restorer).
