# Running the heavy face models on a free Kaggle GPU (face bake-off kit)

**What this is.** A way to run CodeFormer (our current model), GFPGAN v1.4, DiffBIR and PMRF on the same degraded faces on Kaggle's free GPU, and to score the results here with the identity model the
application uses. **What it is not:** a web service. A Kaggle notebook is a batch job: it queues, runs for minutes to an hour, and cannot be called from the upload button. The heavy models
(DiffBIR, PMRF) need an NVIDIA GPU and do not run on an 8 GB laptop or the VPS, so the realistic uses are (1) evidence for the defence ("the better models, measured") and (2) pre-computed
before/after pictures. A live "best quality" button would need a GPU server running a worker (the repo already has the isolated-worker design, `scripts/start_gfpgan_worker.sh`); Kaggle cannot be that worker.

**Status.** The kit's preparation, bridge and scoring were run here (the scoring on results made by our own CodeFormer); `kernel/run_models.py` has **not been run on Kaggle**. It uses each model's own
command from its README (read 2026-11-04), but Kaggle's Python, torch and CUDA versions move, so expect the first run to need a fix. Each model runs on its own and everything is logged to `log.txt`,
so a failure costs minutes, not the session. PMRF is the likeliest to fail (its README pins torch 2.3 / CUDA 11.8 and a matching `natten` wheel).

## 1. One-time setup on Kaggle

1. Verify your phone number on Kaggle (Settings): Kaggle requires it for GPU and for internet access in notebooks, and the notebook needs internet to clone the models and download their weights.
2. Settings -> API -> create a token; save it as `~/.kaggle/kaggle.json` (`chmod 600`). **Never paste it into a chat, a commit or a screenshot.**
3. `pip install kaggle` (any Python; the project's venv is fine).
4. Free GPU time is a weekly allowance (about 30 hours is reported, Kaggle publishes no fixed number); this kit needs a fraction of an hour per model.

## 2. Run it

```bash
# 1. The faces. A PUBLIC set is the default choice (see Privacy). ~40 faces x 4 degradations (blur, motion, small, noise), aligned to 512 px.
python scripts/kaggle_gpu/prepare_inputs.py --photos ~/lfw/lfw --count 40 --out ~/kaggle_face_kit
#    add  --models codeformer,gfpgan   to leave out the heavy ones for a first, cheaper try

# 2. Upload the kit (a PRIVATE dataset) and start the private GPU notebook.
python scripts/kaggle_gpu/kaggle_run.py push --kit ~/kaggle_face_kit --username YOUR_KAGGLE_NAME

# 3. Wait for it (Kaggle queues GPU notebooks; minutes to an hour).
python scripts/kaggle_gpu/kaggle_run.py status --username YOUR_KAGGLE_NAME --wait

# 4. Bring the results back (read log.txt first: it says what failed and why).
python scripts/kaggle_gpu/kaggle_run.py fetch --username YOUR_KAGGLE_NAME --out ~/kaggle_face_results

# 5. Score them here (SFace identity, face detail, LPIPS if installed) and write a table and montages.
python scripts/kaggle_gpu/score_results.py --kit ~/kaggle_face_kit --results ~/kaggle_face_results --out ~/kaggle_face_report
```

`--dry-run` on `push`, `status` and `fetch` prints the Kaggle commands without running them. **Without the command line:** upload the kit folder as a dataset on kaggle.com, create a notebook,
paste `scripts/kaggle_gpu/kernel/run_models.py` as a script, turn on GPU and Internet, attach the dataset, run, and download the output.

If a model fails, send `log.txt`: the fix is usually one line (a package version, a path) and the next run costs minutes.

## 3. Privacy

The default is a public benchmark set (for example Labeled Faces in the Wild, which the project already used for the identity thresholds): nothing private is uploaded. A Kaggle dataset lives on Kaggle's
servers. Using your own photographs, or anyone else's, is **your decision and theirs** (`docs/GPU_SESSION_RUNBOOK.md` says the same about a rented GPU): if you do, keep the dataset private, use only
people who agreed, and delete the dataset and the notebook on Kaggle afterwards. The kit writes numeric ids, never file names.

## 4. What the numbers mean (read before trusting a table)

* **Identity (SFace similarity to the clean face) is the number to rank on.** It is evidence, not proof.
* A public set such as LFW is 250 px, so its "clean" faces are already soft at 512 px. **Face detail and LPIPS against a soft "clean" face are misleading** (a model that restores real detail looks
  "worse" by LPIPS and 14 times "sharper" by the detail ratio; this was seen with our own CodeFormer). For a sharpness judgement use sharp faces (a high-resolution public set such as FFHQ or CelebA-HQ, whose
  terms you must read, or faces you have the right to use) and look at the montages.
* The faces are aligned crops, which is what these models were built for; the application restores faces inside whole photographs.
* Published papers' numbers are not comparable with these (different test sets); see `docs/evaluation/2026-11-04-face-fidelity.md`.

## 5. What each model runs

| Output folder | Command (from the model's README) | Notes |
|---|---|---|
| `codeformer_w1.0`, `codeformer_w0.7` | `inference_codeformer.py -w W --has_aligned` | baseline; S-Lab licence, non-commercial |
| `gfpgan_v1.4` | `inference_gfpgan.py -v 1.4 -s 1 --aligned` | weights from the v1.3.0 release page; code Apache-2.0 |
| `diffbir_v2` | `inference.py --task face --version v2 --steps 50 --captioner none --cfg_scale 4.0` | Apache-2.0 code; weights download themselves; Stable Diffusion 2.1 weights carry an OpenRAIL++ licence |
| `pmrf_k25` | `inference.py --ckpt_path ohayonguy/PMRF_blind_face_image_restoration --num_flow_steps 25` | experimental; its SwinIR stage is DifFace's (non-commercial) |

All of them are fine for this academic project; none of the heavy ones changes the application by being measured.

## 6. Using the result

* If a model is clearly better on identity **and** looks better on sharp faces: say so in the defence and show the montages ("measured on a GPU, not part of the live demo").
* To use it for real the project would need a GPU server with a worker on its own queue (the design exists for GFPGAN: `scripts/start_gfpgan_worker.sh`, `manage.py check_gfpgan`), plus a decision on cost
  (a rented RTX 4090 is roughly $0.3-0.8 an hour on RunPod or Vast.ai, i.e. a few hundred dollars a month if always on; prices from aggregator sites, check live) and on licences. That is out of scope for the
  defence week.
* Importing Kaggle's results into the application as an extra "GPU batch" version of a photograph is not built.
