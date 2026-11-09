# DDColor (photograph colourization): model and licence record

Owner approval: approved

> Recorded 2026-11-02 from the project owner's own statement in the working session ("ddcolor k lia han", answering whether DDColor may be run and used): the owner decided;
> this record only writes it down. It is not legal advice. If the model, its checksum or its source changes, change the line above to `pending` until the owner decides again.

## What it is

| | |
|---|---|
| Model | DDColor, "DDColor: Towards Photo-Realistic Image Colorization via Dual Decoders" (Kang, Yang, Qiao, Gu, Yang; ICCV 2023) |
| Code | https://github.com/piddnad/DDColor, licence **Apache-2.0** (the `LICENSE` file of the repository; copied to `enhancement/archs/ddcolor/LICENSE`) |
| Weights | Hugging Face `piddnad/ddcolor_paper` (ConvNeXt-L, "large") and `piddnad/ddcolor_paper_tiny` (ConvNeXt-T, "tiny"); both carry the model-card tag `license:apache-2.0` |
| Used for | `enhancement/colorize.py`: colourizes a photograph whose colour channels are empty, for the `dynamic` and `ai_max` styles only |
| Vendored | `enhancement/archs/ddcolor/` (5 files, inference only). Changes: relative imports instead of `basicsr.archs.ddcolor_arch_utils`, and timm's `trunc_normal_` / `DropPath` replaced by torch's own initialiser and a four-line DropPath. |

## Files (downloaded once from Hugging Face, hash verified against the hash Hugging Face lists, then loaded from a local folder only)

| File | Bytes | SHA-256 |
|---|---|---|
| `ddcolor_paper` `pytorch_model.bin` (**large, the default**; the application verifies this checksum) | 911,914,869 | `5c77db8292398a2ec1397003f0a90266c28e97b013515d5e9a32e73a4c54a67d` |
| `ddcolor_paper_tiny` `pytorch_model.bin` | 220,372,845 | `8a1277bc90a1bfbb6d2d83933a9a6bc821931879ca93e26e4fcec12165d41fce` |

The checkpoint is loaded with `torch.load(..., weights_only=True)` (no pickled code) and with a strict key check: a partial load (which the authors' own `strict=False` would accept
silently, running a partly random network) is refused.

## What was and was not checked

* Checked: the licence statements above were read from the sources named; the vendored files contain no network, subprocess, `eval`, `pickle` or file access (read before they were run);
  the hashes match.
* Not checked: the licence of the training data (ImageNet) and what it implies for use of the weights; this is the owner's position to hold, as for every model in this project.
* A colourization is a guess at colours that a black-and-white photograph does not contain. It is labelled "colourized" in the result, never "restored"; the original is never changed.
