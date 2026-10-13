# GPU session runbook

A GPU is rented by the second, so everything below is prepared and tested on this
machine first. The session itself should only be: copy, verify, run the comparison,
look at the results, decide, tear down.

**What the session decides:** which of the already-built heavy stages (PureVision,
PureScale, Real-ESRGAN HD restoration, NAFNet deblur, GFPGAN face restoration) are
switched on by default, using measurements and pictures instead of guesses. The two
tools that make that quick (`gpu_preflight.py`, `evaluate_gpu_stages.py`) are already
in `scripts/` and have been run on the CPU here.

Anything marked **untested** has not been run on a GPU or on Linux; treat its first run
as part of the session.

## 1. Before renting (on this machine)

| # | Item | Why | State on 2026-09-26 |
|---|---|---|---|
| 1 | Model files (`.runtime/models`, 11 files, ~1 GB (992 MB)) | Every stage loads local, checksum-pinned weights; runtime downloads are refused | All present. The 8 checksums pinned in settings all match (two older models have none pinned). |
| 2 | Model manifest | Proves the copy on the GPU box is byte-identical | `docs/gpu/model_manifest.json` is committed (13 files: 11 models plus the 2 GFPGAN weights). |
| 3 | **GFPGAN detector and parser weights** | GFPGAN refuses to run without them, and will not download them | **Done 2026-09-26.** `gfpgan/weights/detection_Resnet50_Final.pth` (109 MB) and `parsing_parsenet.pth` (85 MB) are in place and both match their pinned SHA-256. Only needed for the GFPGAN step. |
| 4 | Evaluation sample | The same photos must go through every configuration | `evaluate_gpu_stages.py --export-sample` copies 60 dark photos and 20 face photos (47 MB, not the 4.1 GB `Dataset/`). |
| 5 | Consent for the face photos | The photos go to a rented machine, and GFPGAN stays disabled until a consented evaluation set passes QA | **Your call.** Confirm the `People/` photos may be used, and use only those. The Generated Photos academic dataset (synthetic faces, no consent issue) may also go to the rented machine: its provider allows it "as long as you have control over it and delete the files afterward" (see [DATASET_TERMS.md](DATASET_TERMS.md)). |
| 6 | Redis locally | Not needed for the session, but your local server cannot run background jobs without it | Not running here. Use `CELERY_TASK_ALWAYS_EAGER=True` or start Redis. |

**The two GFPGAN weight files (item 3) are already in place here**; the note below is for re-creating them on another machine. They are the public `facexlib`
face-detection and face-parsing checkpoints. I have not verified a download source
from here, so obtain them yourself and put both in `gfpgan/weights/`. You can prove
they are the right files: their SHA-256 values are pinned in `.env.example`
(`GFPGAN_DETECTION_MODEL_SHA256`, `GFPGAN_PARSING_MODEL_SHA256`) and the pipeline
rejects any file that does not match. Then re-run
`python scripts/model_manifest.py write` so the manifest includes them.

**Licences.** PureScale and PureVision are CC BY-NC-SA 4.0 (non-commercial); fine to
evaluate for the project, but they need a licence from the author before any commercial
deployment (`docs/features/purescale-purevision.md`). Check the terms of the other
weights before deployment too.

**Take the sample:**

```powershell
python scripts/evaluate_gpu_stages.py --count 60 --faces-dir People --face-count 20 --export-sample gpu_sample
python scripts/model_manifest.py write
```

## 2. Renting

- RunPod or Vast.ai, billed per second. 8 to 12 GB of VRAM is enough for every model
  here; 24 GB is comfortable for tiling. At least 50 GB of disk.
- Pick an image with a CUDA-enabled PyTorch already installed, and note the CUDA
  version it reports.
- One machine runs everything (PostgreSQL, Redis, Django, Celery), as decided earlier.
  Splitting the web app and worker across machines adds shared-storage and Redis
  authentication work that this session does not need.
- Stop the instance the moment you have downloaded the results (section 7).

## 3. Set up the machine (untested on the GPU box)

```bash
git clone https://github.com/ijaz-student021/FaceCloud_backend.git && cd FaceCloud_backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
# requirements.txt pins torch 2.14.0 / torchvision 0.29.0. If the image's own CUDA build differs,
# install the CUDA wheel for the machine's driver instead and confirm with the smoke test below.

# copy .runtime/models, gfpgan/weights and gpu_sample from your machine (scp or rsync), then:
python scripts/model_manifest.py verify        # must print "missing: 0   different: 0"
cp .env.example .env                            # edit: DB password, SECRET_KEY, keys. Never commit .env.
python manage.py migrate
python scripts/gpu_preflight.py --expect-gpu --gpu-smoke
```

`gpu_preflight.py` prints PASS, WARN or FAIL for the Python, PyTorch and GPU in use,
every model file and its checksum, which stages are on, PostgreSQL, Redis and disk
space. Do not continue until it prints `Result: PASS` or only warnings you understand.
Here it currently reports FAIL for "no CUDA" and "Redis unreachable" (expected on this
machine); every model file passes its checksum.

**GFPGAN needs its own environment** (its old dependency stack conflicts with the main
one), as described in `docs/features/10-12-image-enhancement.md`:

```bash
python3.11 -m venv .runtime/venvs/gfpgan
.runtime/venvs/gfpgan/bin/pip install <CUDA build of torch 2.1.2 and torchvision 0.16.2>
.runtime/venvs/gfpgan/bin/pip install -r requirements-gfpgan.txt
.runtime/venvs/gfpgan/bin/python manage.py check_gfpgan --load-model
```

## 4. The comparison (what the numbers are for)

Run these from the main environment. Each writes to `.runtime/gpu_eval/<time>/` with
`summary.md`, `summary.json`, `results.jsonl` and a side-by-side `montage.jpg`. A
stopped run continues with `--resume --out <that folder>`.

```bash
# A. Sanity: the GPU baseline should match the CPU numbers except for speed
python scripts/evaluate_gpu_stages.py --configs baseline --images-dir gpu_sample/images --count 60

# B. The restoration stages, one at a time and together
python scripts/evaluate_gpu_stages.py --configs baseline,purevision,purescale,hd,hd_deblur --images-dir gpu_sample/images --count 60

# C. GFPGAN on faces it is designed for (small, blurred, compressed) plus the identity guard
python scripts/evaluate_gpu_stages.py --configs baseline,gfpgan --images-dir gpu_sample/images --count 0 \
    --faces-dir gpu_sample/faces --face-count 20 --degrade-faces --degrade-edge 320

# D. Everything together
python scripts/evaluate_gpu_stages.py --configs baseline,combo --images-dir gpu_sample/images --count 60

# E. Reference-quality: does enhancement recover the TRUE original face, on a bigger
# sample than the CPU smoke test could afford (see docs/features/reference-quality-evaluation.md
# for the CPU baseline this compares against -- a real, repeatable finding: on darkened+
# blurred synthetic faces, identity similarity was 0.24-0.52, every one below the 0.80
# threshold, with GFPGAN/PureVision/HD restoration all disabled)
python scripts/evaluate_reference_quality.py --images-dir gpu_sample/faces --count 40 --pair-count 200
# then repeat with GFPGAN_ENABLED=True (and the others) in .env to see whether any of
# them close the gap found on CPU
```

Configurations: `baseline` (today's defaults), `purevision`, `purescale`, `hd`,
`hd_deblur`, `gfpgan`, `combo`, and `no_colour_gain` (the dark-photo colour boost off).
The report also lists every stage that **fell back and why**, which is how a missing
weight or a disabled flag shows up: a stage that silently did not run would otherwise
look like "no improvement".

Notes from the CPU dry run:

- GFPGAN only runs on photos flagged as blurry or low resolution, and ordinary portraits
  are not. That is why step C degrades the faces. Without `--degrade-faces` the
  `gfpgan` configuration is identical to the baseline.
- The identity guard skips faces that are too small or too dark. If step C reports
  "No face large enough", raise `--degrade-edge` (for example 400).
- PureScale skips very dark photos on purpose, and it will not run when HD restoration
  already upscaled the photo.

## 5. Deciding

**Look at the pictures first.** The numbers mislead: sharpness and "detail" rise when noise
rises, so a grainier result can look better on paper. Open `montage.jpg` and the outputs
before trusting a table.

Proposed rules for switching a stage on by default (adjust them, but write down what you
chose and why):

1. No errors, and no image changed size unexpectedly.
2. It helps on the thing it exists for: mean noise down by at least 10%, or the share of
   photos with visible noise down by at least 10 points, without lowering colourfulness.
3. On the face photos where the identity guard ran, mean similarity at least 0.90 and no
   photo below the guard's threshold (0.80 by default).
4. Time per photo you can live with on the GPU (decide the ceiling before you look).
5. For PureScale and PureVision: the licence question above is answered.

Nothing is enabled by the harness. It only reads.

## 6. Turning a stage on

Set the flag in `.env` (and restart the web app and worker), one stage at a time:

| Stage | Flags |
|---|---|
| PureVision | `PUREVISION_ENABLED=True` |
| PureScale | `PURESCALE_ENABLED=True` |
| HD restoration | `HD_RESTORATION_ENABLED=True` (and `NAFNET_DEBLUR_ENABLED=True` for the deblur pass) |
| GFPGAN | `GFPGAN_ENABLED=True`, `GFPGAN_QUEUE_ENABLED=True`, and a worker on its queue: `scripts/start_gfpgan_worker.sh cuda` (**untested on Linux**; it translates the PowerShell launcher used on Windows) |

Rollback is setting the same flags to `False`. Run `python manage.py test config.test_end_to_end --tag slow`
afterwards to confirm the whole journey still works.

## 7. Afterwards and tear-down

1. Copy `.runtime/gpu_eval/<run>/` back to this machine.
2. Delete the instance's disk. It held the face photos, **the Generated Photos dataset (its terms require deleting it afterwards)** and your `.env` keys.
3. Add the measured results to `docs/features/10-12-image-enhancement.md`, as the denoise and
   colour sections there do. Do not commit any photo or output image of a person; `.runtime/`
   is ignored by Git.
4. Then the GPU work that needs a live model: background blur/removal, face parsing for
   region-aware editing, CodeFormer as a second face restorer, and Feature 50 (face-based
   visual search), in the order recorded in the project notes.

## Known risks

- **Untested on a GPU.** The stages pick CUDA automatically, but none has been run on one in
  this project yet. The preflight smoke test and step A are there to catch that early.
- **Linux is untested** for the launcher and the setup commands above.
- **Torch versions differ between the two environments** by design (main 2.14.0, GFPGAN 2.1.2).
  Do not install one environment's requirements into the other.
- **Face photos on rented hardware.** Use only consented photos and delete the disk afterwards.
- **The Generated Photos dataset** may be copied only to a machine you alone control, and must be deleted there when the session ends. Never commit it, never share it, and do not show individual images to others without asking the provider (`DATASET_TERMS.md`).

## Appendix: validating the staged pipeline with real checkpoints (not yet run)

Added 2026-10-13. Everything about the staged pipeline (`staged_candidate_pipeline_v4`)
has so far been verified in two ways only: tests with mocked models, and real
CPU processing with no learned model. **No checkpoint has been run through it,
on any device.** This appendix is the missing third tier.

Use real portraits and real group photographs you have consent to process
(section 1, item 5). Run each line and keep the output folder.

```bash
# 1. Baseline: no learned model, for comparison.
python scripts/evaluate_pipeline_cases.py --images gpu_sample/faces --cases original \
    --limit 20 --name gpu-0-classical

# 2. One model family at a time, so a problem can be attributed.
python scripts/evaluate_pipeline_cases.py --images gpu_sample/faces --cases original \
    --enable HD_RESTORATION_ENABLED --limit 20 --name gpu-1-realesrgan
python scripts/evaluate_pipeline_cases.py --images gpu_sample/faces --cases original \
    --enable GFPGAN_ENABLED --limit 20 --name gpu-2-gfpgan
python scripts/evaluate_pipeline_cases.py --images gpu_sample/faces --cases original \
    --enable GFPGAN_ENABLED --style ai_max --limit 20 --name gpu-3-gfpgan-aimax

# 3. Group photographs, and the degraded cases with models on.
python scripts/evaluate_pipeline_cases.py --images gpu_sample/groups --cases original \
    --enable HD_RESTORATION_ENABLED,GFPGAN_ENABLED --name gpu-4-groups
python scripts/evaluate_pipeline_cases.py --images gpu_sample/faces \
    --enable PUREVISION_ENABLED,HD_RESTORATION_ENABLED --limit 8 --name gpu-5-degraded
```

Each `summary.md` opens with the evidence tier; it must read `real_gpu_models`.
If it reads `cpu_classical_only`, no checkpoint loaded: read "Stages that fell
back" for the reason before going further.

What to record from each run, and what would count as a failure:

| Check | Where | Failure |
|---|---|---|
| Before/after pictures | `<photo>__original.jpg`, `sheet.jpg` | Waxy skin, halos, a face that reads as a different person, colour shift |
| Identity outcome | "Identity, devices and memory" table | `failed`; `inconclusive` on a clear portrait; similarity near the 0.80 minimum |
| GFPGAN without identity | run 2 with `IDENTITY_PROVIDER=disabled` | Any face restored (the gate must refuse) |
| AI Max bypass | run 3 | A restored, unverified face with no `identity_notice` |
| Face-fidelity thresholds | `results.json` → `face_fidelity` | Faces adjusted on results that look right (thresholds too tight), or none adjusted on results that look wrong |
| Latency and memory | same table | GPU peak near the card's limit; any `memory_retries` above 0 |
| Fallbacks | "Stages that fell back" | Any model stage falling back when its checkpoint is installed |
| Out-of-memory path | set `HD_RESTORATION_TILE_SIZE=2048` and repeat run 1 | A crash, instead of a retry with smaller tiles or a recorded fallback |

Also run the real-tensor tests on the GPU box, where they exercise CUDA instead
of MPS: `python manage.py test enhancement.test_runtime`.

