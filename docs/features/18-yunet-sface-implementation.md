# Feature 18: YuNet and SFace Implementation

## Status

Implemented and enabled for local evaluation. Commercial approval is recorded by
owner attestation. Production acceptance still requires threshold calibration on
a representative FaceCloud evaluation set.

## Model inventory

| Stage | Provider | Input/output |
|---|---|---|
| Detection | `yunet_s_dynamic.onnx` through `cv2.FaceDetectorYN` | Dynamic BGR image to face box, five landmarks, and confidence |
| Alignment and recognition | `face_recognition_sface_2021dec.onnx` through `cv2.FaceRecognizerSF` | YuNet-aligned 112×112 face to 128-value embedding |

Checksums are enforced before model construction. The license/approval record is
[`feature-18-yunet-sface-attestation.md`](../model-licenses/feature-18-yunet-sface-attestation.md).

## Processing flow

1. Preprocessing creates a conservative standardized image.
2. YuNet detects every face and returns five standardized-image landmarks.
3. Validation independently evaluates up to five supported faces.
4. SFace uses the complete YuNet geometry with `alignCrop()`.
5. SFace creates a finite 128-dimensional vector, which is L2-normalized.
6. The protected vector is compared only with compatible groups owned by the same
   user.
7. A clearly separated high score joins an existing group; a low score creates a
   neutral group; borderline or ambiguous scores enter review.
8. Only confirmed memberships update protected group centroids.

## Configuration

```dotenv
FACE_DETECTOR_PROVIDER=yunet_v1
YUNET_MODEL_PATH=.runtime/models/yunet_s_dynamic.onnx
YUNET_MODEL_SHA256=daede24002cc8590f457998cc5e6c9e8b82f45f6bd969506a8913cd4ed10a2d3
IDENTITY_PROVIDER=sface_2021dec_v1
IDENTITY_MODEL_PATH=.runtime/models/face_recognition_sface_2021dec.onnx
IDENTITY_MODEL_SHA256=0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79
IDENTITY_EMBEDDING_DIMENSION=128
IDENTITY_AUTO_ANALYZE=True
```

Production must also set an independent `IDENTITY_EMBEDDING_KEY`; it must not be
committed to source control.

## Privacy guarantees

- Raw embeddings and centroids never appear in serializers, admin lists, logs,
  reports, or downloads.
- Model files remain outside source control under `.runtime/models`.
- Every lookup and candidate comparison is owner scoped.
- Account and image deletion cascade to their biometric-processing artifacts.
- Similarity means probable visual correspondence and is never represented as
  verified identity.

## Evaluation gate

Before enabling this configuration in a customer production environment, evaluate
same-person and different-person score distributions across lighting, pose, skin
tones, age ranges, camera quality, and group images. Record false-match,
false-non-match, and uncertain-review rates, then replace the candidate thresholds
with approved values and advance `IDENTITY_POLICY_VERSION`.
