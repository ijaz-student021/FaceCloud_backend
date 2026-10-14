# Zero-DCE checkpoint provenance

Established 2026-10-14. Records where `.runtime/models/zero_dce_epoch99.onnx` comes
from and why two copies of it with different checksums compute the same thing.

## The file

| | |
|---|---|
| Expected SHA-256 | `e5cb6f921c7997f89105f6aa86242f30ff3f5379f5c6cd5282eb2aada14dae14` (unchanged from `docs/gpu/model_manifest.json`, written 2026-09-26) |
| Size | 320,301 bytes |
| Weights | Official Zero-DCE `Epoch99.pth` from github.com/Li-Chongyi/Zero-DCE, `Zero-DCE_code/snapshots/Epoch99.pth`, SHA-256 `a4395acb874f320375d9704997cef874eaaaaa26a1777ceb29a92b70f74c3612` |
| Exporter | `scripts/export_zero_dce_onnx.py`: `torch.onnx.export`, TorchScript exporter (`dynamo=False`), opset 17, input `image` with dynamic height and width, output `curve_parameters` |
| PyTorch | 2.13.0 |

## How it was established

Exporting the official weights with the script above reproduces each known file
byte for byte; only the PyTorch version changes the bytes:

| PyTorch | SHA-256 of the export | Which file it is |
|---|---|---|
| 2.13.0 | `e5cb6f92…dae14` | the manifest's file |
| 2.14.1 | `b7f7d34a…bc46e` | the copy found in `/Users/Ali/Projects/FaceCloud_backend/.runtime/models` (exported there on 2026-10-03) |
| 2.14.0 | `16d860c1…94eeb` | neither |
| 2.12.1, 2.12.0 | `289543b1…`, `37cec67b…` | neither |

The manifest file and the 2.14.1 copy differ in exactly two bytes, offsets 17 and
19: the `producer_version` string `2.13.0` versus `2.14.1`. Everything else,
including every weight, is identical.

## Inference comparison

Both files, run through OpenCV DNN (as the pipeline runs them), against the
official PyTorch weights, on five images (a portrait, the same portrait
darkened, two sample photographs and random dark noise) at four input sizes each
(256x256, 512x384, 1024x683, 333x517):

| Comparison | Largest absolute difference in the 24 curve maps |
|---|---|
| manifest file vs official PyTorch weights | 5.96e-07 |
| 2.14.1 copy vs official PyTorch weights | 5.96e-07 |
| manifest file vs 2.14.1 copy | 0 (bit-identical) |

## Conclusion

The mismatch was a serialization difference (the exporter's version string), not
a model difference. The manifest's checksum was kept; the file was replaced with
a reproduced export that matches it. The 2.14.1 copy is kept in
`.runtime/models/provenance/` for comparison. Zero-DCE now has a pinned checksum
(`ZERO_DCE_MODEL_SHA256`) like every other model, so a file that runs but is not
the expected file is no longer accepted silently.

## The private curve model

`facecloud_curve_best_opencv.onnx` (manifest SHA-256 `f4805a62…a9f220`) is
FaceCloud's own model trained with `scripts/train_facecloud_curve_model.py`. It
was not found on this machine. It is not required by the default configuration:
`ZERO_DCE_MODEL_PATH` points at the public Epoch99 export, and the private model
is an alternative used only if that setting (and `ZERO_DCE_MODEL_SHA256`) are
pointed at it.
