# Analyzer calibration, 2026-10-15

Produced by `python scripts/calibrate_analyzer.py --images <12 of the project's own phone photographs> --out ...`: each photograph
degraded by a known, synthetic amount, then analysed by `enhancement/adaptive/analyzer.py`. The thresholds in `Thresholds` come from
these numbers. **Read the limits at the bottom first**: synthetic defects, 12 photographs of one phone and mostly one demographic.
Exposure is judged on the face; the dataset's faces are all of one skin-tone range, so the behaviour on darker or lighter skin is
unmeasured (the design uses the face's luminance *range* and crushed-shadow share, not its mean, to avoid the obvious bias, but that
is a design argument, not a measurement).


12 photographs, synthetic degradations, YuNet face detector. Faces found per photograph: [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1].

## Exposure (what the analyzer called each darkening)

* original: {'normal': 10, 'dark_scene': 2}
* dark x0.7: {'low_light_subject': 2, 'normal': 6, 'dark_scene': 4}
* dark x0.5: {'low_light_subject': 8, 'normal': 3, 'dark_scene': 1}
* dark x0.35: {'low_light_subject': 12}
* dark x0.25: {'low_light_subject': 12}
* dark x0.15: {'low_light_subject': 12}

## Noise estimate by added Gaussian sigma (median, p10-p90) and the status it maps to

* sigma 0: median 1.00 (p10 0.00, p90 1.90) -> {'none': 10, 'low': 2}
* sigma 3: median 2.00 (p10 1.00, p90 2.00) -> {'none': 5, 'low': 7}
* sigma 6: median 3.00 (p10 2.00, p90 3.00) -> {'low': 11, 'moderate': 1}
* sigma 10: median 4.00 (p10 4.00, p90 5.00) -> {'moderate': 12}
* sigma 15: median 6.00 (p10 5.00, p90 6.00) -> {'moderate': 12}
* sigma 25: median 9.00 (p10 9.00, p90 9.90) -> {'moderate': 1, 'high': 11}

## Blur: true class -> (status, type) the analyzer reported

* none: {('low', 'defocus'): 1, ('none', 'none'): 11}
* defocus: {('high', 'defocus'): 38, ('high', 'mixed'): 7, ('high', 'motion'): 3}
* motion: {('high', 'mixed'): 16, ('high', 'defocus'): 4, ('high', 'motion'): 62, ('low', 'motion'): 34, ('none', 'none'): 4}

Motion blur identified as motion: 96/120.
Defocus identified as defocus or mixed: 45/48.
Sharp photographs called blurred: 1/12.

Limitations: synthetic defects, a small set of photographs, one demographic. Re-run this on new data before changing a threshold.
