# Third-party dataset: Generated Photos Academic Dataset

Record of where this dataset came from and the conditions it was given under, so the
project keeps to them. **The dataset itself is never committed, published or shared.**

## What it is

- 10,000 AI-generated (synthetic) face photos, 256x256, from Generated Photos
  (https://generated.photos), described by the provider as covering gender, age, ethnicity,
  eye colour, hair colour, hair length and emotion.
- The provider confirmed (2026-09-26) that it contains **images of different people**, so
  there is one image per person, "although there may be some images that look very
  similar".
- Our copy has faces only, on white backgrounds.

## Permission (from the provider's emails, 2026-09-26)

Requested through the academic dataset form on the provider's page, for this final-year
project. Their replies, in summary:

1. A supervised final-year project **qualifies as academic use**.
2. We may **publish results, as long as we cite them**. Citation wording they gave:
   "Academic dataset by Generated Photos", with a link to their website (the wording may be
   adjusted to the project's formatting requirements).
3. **Condition:** academic purposes only, and the dataset itself is **not transferred or made
   publicly available**.
4. A second reply confirmed the described uses are fine: evaluating enhancement (degrade
   clean faces and compare the result to the original), testing face detection and quality
   checks, and measuring how often face matching wrongly matches different people. It said
   we "can store the images in the rented cloud as long as you have control over it and
   delete the files afterward".

The original emails are kept by the project owner as proof of these terms. If anything
here differs from those emails, the emails win.

## The dataset's own terms (its `Readme.txt`)

The download includes a terms file. In summary:

- It may be used and adapted for any research purpose, provided we (a) cite it in the paper
  and (b) link to the Generated Photos website when publishing the paper, results or a related
  article. Their example attribution line: `Academic Dataset by Generated Photos
  https://generated.photos/datasets`.
- It may be **redistributed within the university**, indicating any changes made and making
  sure the fellow student or teacher receiving it knows the terms.
- The photos are 100% synthetic, model-released and royalty-free, usable for any research
  purpose that does not break the law.
- They would appreciate hearing about the research outcome.

This is more permissive than the provider's later email ("not transferred or made publicly
available"). Until the provider says otherwise, follow the stricter reading: sharing inside
the university is allowed by the terms file only with those two conditions, so ask first.

## What is in the download

- `generated.photos/`: the JPG images (256x256).
- `generated.photos_metadata/`: one JSON file per image with **face landmarks** (mouth,
  eyebrows, eyes, nose, jaw) and attributes (head pose, gender, makeup, emotion, facial hair,
  hair, occlusion, ethnicity, eye colour, smile, age). Not every image has a metadata file, so
  check coverage before relying on it. The landmarks are ground truth for checking our face
  detector's landmark accuracy.
- The copy on this project's machine was placed at `faceimage/` in the repository root, which
  Git does not ignore by default. It is excluded locally (`.git/info/exclude`); move it to
  `Dataset/generated_photos/` (already git-ignored) or outside OneDrive-synced folders.

## Rules for this project

| Do | Do not |
|---|---|
| Keep it in `Dataset/generated_photos/` (git-ignored) or another local folder outside the repository | Commit it, push it, attach it to a pull request or a GitHub release |
| Use it to evaluate enhancement, face detection, quality checks and false matches between different people | Use it in the shipped product, in a commercial deployment, or to train models that are shipped |
| Publish aggregate results (averages, tables, charts) with the citation | Publish the dataset or large parts of it |
| Copy it to a rented GPU machine that only the project owner controls, and **delete it there when the session ends** | Leave it on a rented machine, or give anyone else access to it |
| Cite: "Academic dataset by Generated Photos" plus the link | Share the download link or hand copies to teammates without asking the provider |

**Not confirmed by the provider** (so treat as not allowed until they say yes): showing
individual sample images to a supervisor or examiners, and including sample images in a
report. Aggregate results are covered. Ask before using individual images.

## What it can and cannot be used for here

- **Good for:** reference-based enhancement testing (degrade a clean face, run the
  pipeline, compare with the original; measure on the face region, because a white
  background would inflate the scores), and checking that the identity guard separates a
  face from a different person's face.
- **Not for:** calibrating the face-matching threshold (Feature 18 / T-001), because each
  person appears only once, so there are no genuine same-person pairs. Its near-lookalikes
  also make "different person" scores look worse than they are.
- **Not a substitute for real photos.** Backgrounds are plain white and faces are large and
  centred, so detection looks easier than in real photos. Keep evaluating on real photos too.

## Where it lives and how to handle it

- Folder: `Dataset/generated_photos/` (`Dataset/` is git-ignored). The project folder is
  inside OneDrive, which syncs to a Microsoft cloud; a folder outside OneDrive is the
  cautious choice, and every tool here accepts any path.
- Copy the dataset only to a machine you control, and delete the copy when the
  work is done. (The GPU session runbook that used to spell this out was removed
  with the learned models.)
