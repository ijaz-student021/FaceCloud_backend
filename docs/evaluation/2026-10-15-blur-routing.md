# Blur routing: false positives, false negatives, and what the model actually gains

Reproduce: `python scripts/evaluate_blur_routing.py --images <photos> --out <dir> --run-model` (16 phone photographs at 640 px, 23 degraded
copies each; 12 are the development set, 4 were never used for calibration and are the **held-out set**). Date 2026-10-15, Apple M1.
The degradations are *not* the ones the analyzer was calibrated on (other blur lengths and angles, camera-shake paths, subject-only
motion, other defocus radii, blur with noise, JPEG, naturally soft photographs).

## Reading it correctly

The first two tables of each split below count errors against how the copy was *made* (a defocus copy "should not" run the motion model).
That is the wrong yardstick on its own, so the model was also run on every copy and the table at the end of each split gives what it gained.
Measured against "did the model improve this copy by more than 0.5 dB", the router's decisions are:

| Split | Copies where the model helps | Router runs on them (recall) | Router runs and the model helps (precision) | Router runs where the model hurts | Share of the total gain captured |
|---|---|---|---|---|---|
| development (228 non-sharp) | 104 | 59 (57 %) | 59 of 104 runs (57 %) | 25 | 68 % |
| **held-out (76 non-sharp)** | 34 | 25 (74 %) | 25 of 38 runs (66 %) | 9 | 90 % |

Mean gain when the router runs: +1.93 dB (development) and +2.43 dB (held-out); when it skips: +0.52 and -0.09 dB. It runs on 1 of 16
sharp held-out copies, and the model loses quality on 15 of 16 sharp copies, so a run on a sharp photograph is a real cost.

What the model does per kind of copy (all 16 photographs, mean PSNR gain): linear motion of 7 / 15 / 27 px **+5.3 / +7.6 / +5.0 dB** (helps
in 16/16, 16/16, 14/16), 35 px +0.7 dB (7/16), mixed motion and disk +2.5 dB (16/16), **camera-shake paths -1.1 dB (helps in 0/32)**,
subject-only motion +0.6 dB (16/32), small defocus (disk 2, Gaussian 1.2) -1.4 / -0.6 dB (0/16), large defocus (disk 7 and 9, Gaussian
5.0) **+1.5 / +1.5 / +0.9 dB (helps in 16/16, 16/16, 15/16)**, and blur combined with noise 0.0 dB (its own fidelity guard returns the
input unchanged).

## Findings

1. **Calibration over-stated the typing.** On the calibration degradations motion was typed as motion 96/120 and defocus 45/48. On these
   held-out copies defocus is sent to the motion model 10 of 32 times and motion is missed 16 of 40 times (by how the copies were
   made). Camera shake, subject-only motion and motion with noise at sigma 12 are the weak spots.
2. **But "missing" shake is not a loss and "running" on large defocus is not a harm**: the model does not help shake (0/32) and does help
   large defocus. The router's decisions track where the model helps better than the labels suggest: it captures 90 % of the held-out gain.
3. **No threshold change was made.** A rule searched on 12 development photographs and applied to 4 held-out ones would be fitted to the
   data it is judged on; the held-out numbers above are for the thresholds as they were, and they are acceptable (the validator is the
   second gate, and rejects a result that loses structure or adds halos). The honest summary is: typing is approximate; the router plus
   validator together are net-positive on this data; they are not proven on real camera shake or subject motion.
4. **Open weakness:** blur is measured over the whole frame, so a blurred subject on a sharp background reads as no blur (4 of 8
   held-out subject-motion copies read `none/none`). A face-region blur measure would address it; not done because the model's mean
   gain on those copies is small (+0.6 dB).

## Generated tables


`runs deblur` is the router's decision. Wrong decision = runs on sharp/defocus (false positive) or does not run on motion (false negative). `mild_motion` (7 px) is neutral.

## development set (12 photographs, 276 degraded copies)

| Variant | n | runs deblur | analyzer says (status/type) |
|---|---|---|---|
| sharp | 12 | 0/12 | {'low/defocus': 1, 'none/none': 11} |
| sharp_noise4 | 12 | 0/12 | {'low/defocus': 1, 'none/none': 11} |
| sharp_jpeg70 | 12 | 0/12 | {'low/defocus': 1, 'none/none': 11} |
| soft_resampled | 12 | 1/12 | {'high/defocus': 1, 'low/defocus': 5, 'none/none': 5, 'low/motion': 1} |
| motion_7 | 12 | 6/12 | {'high/defocus': 2, 'low/motion': 3, 'high/mixed': 1, 'none/none': 1, 'low/mixed': 2, 'high/motion': 2, 'low/defocus': 1} |
| motion_15 | 12 | 11/12 | {'high/defocus': 1, 'high/motion': 7, 'low/motion': 3, 'high/mixed': 1} |
| motion_27 | 12 | 12/12 | {'high/motion': 8, 'low/motion': 4} |
| motion_35 | 12 | 12/12 | {'high/motion': 9, 'low/motion': 3} |
| shake | 24 | 16/24 | {'high/mixed': 13, 'high/defocus': 5, 'high/motion': 3, 'low/mixed': 2, 'low/defocus': 1} |
| subject_motion | 24 | 1/24 | {'high/defocus': 2, 'low/mixed': 3, 'none/none': 18, 'low/motion': 1} |
| defocus_disk_2 | 12 | 1/12 | {'high/defocus': 3, 'low/mixed': 1, 'low/defocus': 7, 'high/motion': 1} |
| defocus_disk_4 | 12 | 3/12 | {'high/defocus': 9, 'high/mixed': 3} |
| defocus_disk_7 | 12 | 4/12 | {'high/defocus': 8, 'high/motion': 1, 'high/mixed': 3} |
| defocus_disk_9 | 12 | 6/12 | {'high/defocus': 6, 'high/motion': 1, 'high/mixed': 5} |
| defocus_gauss_1.2 | 12 | 1/12 | {'high/defocus': 3, 'low/mixed': 1, 'low/defocus': 7, 'high/motion': 1} |
| defocus_gauss_2.8 | 12 | 3/12 | {'high/defocus': 9, 'high/motion': 2, 'high/mixed': 1} |
| defocus_gauss_5.0 | 12 | 5/12 | {'high/defocus': 7, 'high/motion': 2, 'high/mixed': 3} |
| mixed_motion15_disk3 | 12 | 9/12 | {'high/defocus': 3, 'high/motion': 6, 'high/mixed': 3} |
| motion15_noise6 | 12 | 9/12 | {'low/defocus': 1, 'high/motion': 6, 'high/defocus': 2, 'low/motion': 3} |
| motion15_noise12 | 12 | 3/12 | {'none/none': 7, 'low/motion': 3, 'low/defocus': 1, 'low/mixed': 1} |
| defocus4_noise6 | 12 | 2/12 | {'high/defocus': 10, 'high/mixed': 2} |

* False positives (ran on sharp or defocus): **26/144** (sharp 1/48, defocus 25/96).
* False negatives (did not run on motion): **47/120**.

With the real NAFNet-GoPro run on every copy (PSNR against the clean photograph, mean over copies; the `sharp` row starts from identical images, so its PSNR before is not meaningful, the loss count is):

| Truth | n | PSNR before | PSNR after | gain | copies where it lost quality |
|---|---|---|---|---|---|
| sharp | 48 | 116.77 | 35.35 | -81.43 | 39/48 |
| defocus | 96 | 26.92 | 27.20 | +0.29 | 29/96 |
| mild_motion | 12 | 29.56 | 35.02 | +5.47 | 0/12 |
| motion | 120 | 24.48 | 25.92 | +1.43 | 41/120 |

## held out set (4 photographs, 92 degraded copies)

| Variant | n | runs deblur | analyzer says (status/type) |
|---|---|---|---|
| sharp | 4 | 0/4 | {'low/defocus': 1, 'none/none': 3} |
| sharp_noise4 | 4 | 0/4 | {'low/defocus': 1, 'none/none': 3} |
| sharp_jpeg70 | 4 | 0/4 | {'low/defocus': 1, 'none/none': 3} |
| soft_resampled | 4 | 1/4 | {'high/mixed': 1, 'low/defocus': 2, 'none/none': 1} |
| motion_7 | 4 | 4/4 | {'high/mixed': 3, 'low/motion': 1} |
| motion_15 | 4 | 4/4 | {'high/motion': 4} |
| motion_27 | 4 | 4/4 | {'high/motion': 3, 'low/motion': 1} |
| motion_35 | 4 | 4/4 | {'high/motion': 4} |
| shake | 8 | 4/8 | {'high/motion': 3, 'high/mixed': 1, 'high/defocus': 2, 'low/defocus': 2} |
| subject_motion | 8 | 2/8 | {'high/motion': 2, 'low/defocus': 2, 'none/none': 4} |
| defocus_disk_2 | 4 | 1/4 | {'high/mixed': 1, 'high/defocus': 2, 'low/defocus': 1} |
| defocus_disk_4 | 4 | 1/4 | {'high/motion': 1, 'high/defocus': 3} |
| defocus_disk_7 | 4 | 1/4 | {'high/motion': 1, 'high/defocus': 3} |
| defocus_disk_9 | 4 | 2/4 | {'high/motion': 1, 'high/defocus': 2, 'high/mixed': 1} |
| defocus_gauss_1.2 | 4 | 1/4 | {'high/mixed': 1, 'high/defocus': 2, 'low/defocus': 1} |
| defocus_gauss_2.8 | 4 | 1/4 | {'high/motion': 1, 'high/defocus': 3} |
| defocus_gauss_5.0 | 4 | 2/4 | {'high/motion': 1, 'high/defocus': 2, 'high/mixed': 1} |
| mixed_motion15_disk3 | 4 | 4/4 | {'high/motion': 4} |
| motion15_noise6 | 4 | 2/4 | {'high/defocus': 2, 'low/motion': 1, 'high/motion': 1} |
| motion15_noise12 | 4 | 0/4 | {'high/defocus': 1, 'none/none': 3} |
| defocus4_noise6 | 4 | 1/4 | {'high/motion': 1, 'high/defocus': 3} |

* False positives (ran on sharp or defocus): **11/48** (sharp 1/16, defocus 10/32).
* False negatives (did not run on motion): **16/40**.

With the real NAFNet-GoPro run on every copy (PSNR against the clean photograph, mean over copies; the `sharp` row starts from identical images, so its PSNR before is not meaningful, the loss count is):

| Truth | n | PSNR before | PSNR after | gain | copies where it lost quality |
|---|---|---|---|---|---|
| sharp | 16 | 117.17 | 34.35 | -82.82 | 15/16 |
| defocus | 32 | 27.96 | 28.08 | +0.13 | 10/32 |
| mild_motion | 4 | 30.25 | 35.23 | +4.97 | 0/4 |
| motion | 40 | 25.10 | 26.72 | +1.62 | 14/40 |

## Limits

Synthetic blur of five or six kinds on 16 photographs of one phone and mostly one person; PSNR is the only quality measure here; the
held-out set is only 4 photographs (92 copies) so each percentage has a wide interval; no real shake, subject movement or lens defocus.
