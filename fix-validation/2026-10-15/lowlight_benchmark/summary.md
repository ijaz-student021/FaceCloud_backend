# Low-light benchmark (CPU)

12 photos x 3 darkness levels (0.4, 0.28, 0.18); each dark copy has sensor-like noise; scored against the clean photo.

| Configuration | Exposure error (lower) | Structure SSIM (higher) | Colour error (lower) | Noise | Seconds |
|---|---|---|---|---|---|
| lightweight, level high | 101.04 | 0.9387 | 9.58 | 1.97 | 0.89 |
| lightweight + chroma pre-filter, high | 101.36 | 0.9542 | 9.06 | 1.5 | 1.11 |
| lightweight + NLM pre-filter, high | 101.37 | 0.9495 | 8.91 | 1.22 | 1.2 |
| balanced (Zero-DCE, CPU), medium | 95.49 | 0.9509 | 8.19 | 1.11 | 1.93 |
| balanced (Zero-DCE, CPU), high | 86.92 | 0.9418 | 7.84 | 1.11 | 1.82 |
| automatic (default policy) | 117.75 | 0.9599 | 9.36 | 0.89 | 0.9 |
