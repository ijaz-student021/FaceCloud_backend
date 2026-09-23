# Feature 18 YuNet and SFace Model Attestation

## Approval record

The FaceCloud project owner confirmed on 2026-09-14 that the project has obtained
the required license approval for local use of the exact YuNet and SFace artifacts
listed below. The underlying license documents remain in the owner's controlled
records and are intentionally not committed to this repository.

This file records the engineering decision and artifact identity. It is not a
replacement for the underlying legal documents or legal advice.

## Approved artifacts

| Purpose | Artifact | SHA-256 | Recorded upstream license |
|---|---|---|---|
| Face detection and five landmarks | `yunet_s_dynamic.onnx` | `daede24002cc8590f457998cc5e6c9e8b82f45f6bd969506a8913cd4ed10a2d3` | YuNet source distribution license retained in owner records |
| Face-similarity embedding | `face_recognition_sface_2021dec.onnx` | `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79` | Apache License 2.0 in the OpenCV Zoo SFace directory |

## Deployment restrictions

- Models run locally; production workers must never download weights dynamically.
- The artifacts are not exposed through APIs, reports, logs, or downloads.
- SFace output is used for probabilistic similarity grouping, not legal identity
  verification or authentication.
- Comparisons remain owner/tenant scoped.
- Production distribution must preserve all required license and attribution files.
- Thresholds remain candidate values until the evaluation report is approved.

## Review

Revalidate this record whenever either model file, checksum, source, deployment
method, or commercial use changes.
