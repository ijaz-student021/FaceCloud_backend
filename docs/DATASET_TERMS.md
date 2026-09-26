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
- Before a GPU session, follow [GPU_SESSION_RUNBOOK.md](GPU_SESSION_RUNBOOK.md): the copy goes
  only to a machine you control, and the tear-down step deletes it.
