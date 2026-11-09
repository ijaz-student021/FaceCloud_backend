# 4x-UltraSharp and Real-ESRGAN general: model and licence record (detail restoration)

Owner approval: approved

> Recorded 2026-11-03 from the project owner's own statements in the working session: on 2026-11-02 ("4x_Ultrasharp ... can we use that model? if yes then download it and apply on") and
> again on 2026-11-03 (the same recommendation pasted, with "chahey koi bhi model lagao"). The owner decided; this record only writes it down and is not legal advice. If a model, its
> checksum or its source changes, change the line above to `pending` until the owner decides again.

## 4x-UltraSharp (optional, off by default: `ULTRASHARP_ENABLED`)

| | |
|---|---|
| Model | 4x-UltraSharp (Kim2091), a 4x ESRGAN-architecture upscaler |
| Source | https://huggingface.co/Kim2091/UltraSharp ; OpenModelDB entry for 4x-UltraSharp |
| Licence | **CC BY-NC-SA 4.0**: non-commercial, attribution, share-alike (read from the Hugging Face README and the OpenModelDB entry on 2026-11-02) |
| File | `4x-UltraSharp.pth`, 66,961,958 bytes, SHA-256 `a5812231fc936b42af08a5edba784195495d303d5b3248c24489ef0c4021fe01` (matches the official Hugging Face repository) |
| Network | the same RRDBNet as Real-ESRGAN x4plus in the original ESRGAN key layout; `hd_restoration.convert_esrgan_keys` renames the keys and the load is strict (702 of 702 tensors) |

The production guard lists it as a non-commercial model: production refuses it unless `NONCOMMERCIAL_MODELS_ACKNOWLEDGED=True` (as for Zero-DCE, CodeFormer and GFPGAN). Attribution: "4x-UltraSharp by Kim2091".
The results are a derivative of the photograph, not of the model; whether share-alike reaches them is the owner's position to hold.

## realesr-general-x4v3 (default fast engine, on with `HD_RESTORATION_ENABLED`)

| | |
|---|---|
| Model | `realesr-general-x4v3`, the compact SRVGGNetCompact network of Real-ESRGAN |
| Source | https://github.com/xinntao/Real-ESRGAN (release v0.2.5.0) |
| Licence | **BSD-3-Clause** (the repository's licence; the same one as the Real-ESRGAN x4plus model already in use) |
| File | `realesr-general-x4v3.pth`, 4,885,111 bytes, SHA-256 `8dc7edb9ac80ccdc30c3a5dca6616509367f05fbc184ad95b731f05bece96292` (the file the owner downloaded on 2026-10-17; **not compared with a published hash: none was found**) |
| Code | `hd_restoration._build_srvgg` is the published network (32 conv + PReLU blocks, 4x pixel shuffle, nearest-neighbour base), written for this project; it loads the checkpoint strictly (101 tensors) |

## What was not checked
The licence of the training data of either model; the weights of 4x-UltraSharp beyond the hash; whether the compact model's file equals the published release (see above).
