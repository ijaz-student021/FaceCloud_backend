# SFace person-folder limits: measured, not guessed (2026-10-19)

**What was wrong.** SFace (the identity model) used limits meant for another scale: a face opened a new person folder below a similarity of **0.68** and joined a folder
only from **0.82**. SFace cosine similarity for two photographs of the *same* person sits far lower than that, so nearly every photograph opened a folder of its own and the
rest waited for review as "70 % match". This document records the measurement the new limits come from and how to repeat it.

## Method

`python manage.py evaluate_identity_thresholds <photos>` (`identities/management/commands/evaluate_identity_thresholds.py`, logic in `identities/threshold_evaluation.py`).
Each photograph goes through the application's own detector (YuNet, score floor 0.60 as configured here), alignment and embedding model (SFace 2021dec, the configured
checkpoint), then:

1. **Pair distributions**: the cosine similarity of every same-person pair and of different-person pairs (all of them, or a seeded sample of 200,000).
2. **Album simulation**: albums of 6 people with 6 to 14 photographs each, in random order of arrival, are grouped with `identities.services.decide_group` (the function the
   service itself calls), comparing a face with each folder's average face, for every candidate pair of limits. 150 albums per candidate, the same albums for every candidate.
3. **Folder-pair similarity**: the average-face similarity between two folders of one person and between folders of two people (what the Identity Review merge suggestion compares).

**Data: Labeled Faces in the Wild (LFW)**, a public face-verification benchmark: 13,233 photographs of 5,749 people, downloaded for this measurement
(https://ndownloader.figshare.com/files/5976018), used from a temporary folder and not committed. 3,431 photographs of 500 people were used (people with at least 6 photographs
first, then others for more different-person pairs, at most 14 photographs each, seeded); every one was found and embedded (0 without a usable face).

## Result

| Cosine similarity of a pair | pairs | mean | 1 % | 5 % | 25 % | median | 75 % | 95 % | 99 % | max |
|---|---|---|---|---|---|---|---|---|---|---|
| same person | 16,323 | 0.650 | **0.300** | 0.461 | 0.591 | 0.662 | 0.725 | 0.806 | 0.855 | 0.986 |
| different people | 200,000 | 0.090 | -0.117 | -0.057 | 0.030 | 0.090 | 0.150 | 0.236 | **0.297** | 0.473 |

Share of pairs a threshold lets through (same person = `tar`, different people = `far`):

| threshold | 0.30 | 0.40 | 0.45 | 0.50 | 0.60 | 0.68 | 0.70 | 0.82 |
|---|---|---|---|---|---|---|---|---|
| same-person pairs (`tar`) | 0.990 | 0.977 | 0.956 | 0.912 | 0.722 | 0.428 | 0.347 | 0.032 |
| different-people pairs (`far`) | 0.0092 | 0.0003 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |

**At the old new-folder limit of 0.68, fewer than half (42.8 %) of the photograph pairs of one person are alike enough to be called the same; at the old join limit of 0.82, 3.2 %.** Different people almost never reach 0.45 (none of 200,000 did; the
highest was 0.473).

### Limits chosen (from the pair distributions, checked by the album simulation)

* **`new_group` = 0.30**: the 1st percentile of same-person similarity. A face less alike than that to a folder is, for 99 % of true matches, not that person: it opens a folder.
* **`match` = 0.45**: the threshold at most 0.01 % of different-person pairs reach (0.418), rounded up to the 0.05 grid. A face joins a folder by itself only from here, and at
  least `margin` ahead of the runner-up. Between 0.30 and 0.45 it waits for review, suggested for the closest folder.
* **`margin` = 0.06** (unchanged): varying it from 0 to 0.15 changed nothing measurable on this data (the second-best folder is almost never close), so the existing value stays as
  a guard for look-alikes this benchmark does not contain.
* **`guard` = 0.80** (unchanged): it compares a photograph with its own enhanced version, not two photographs, so these distributions do not apply to it.
* **Merge suggestion minimum = 0.40** (was 0.75): see below.

| 150 albums of 6 people | folders a person ends up in | confirmed photos in the wrong person's folder | photos left for review | pair precision | pair recall |
|---|---|---|---|---|---|
| **old limits 0.68 / 0.82** | **3.96** | 0.0 % | **48 %** | 1.000 | **0.096** |
| **new limits 0.30 / 0.45** | **1.02** | 0.0 % | **1.4 %** | 1.000 | **0.995** |

The old limits never put anyone in the wrong folder; they just almost never put the same person in the same one (pair recall 0.096) and parked half of the photographs for review.

### Merge suggestion (folders already split by the old limits)

Average-face similarity between two folders made by the old limits (the situation in existing accounts): 6,974 pairs of one person (mean 0.554, 5th percentile 0.375, median 0.573)
and 35,523 pairs of different people (99th percentile 0.290, highest 0.401).

| suggestion minimum | 0.35 | **0.40** | 0.45 | 0.50 | 0.75 (old) |
|---|---|---|---|---|---|
| one person's split folders suggested | 96.2 % | **93.3 %** | 87.4 % | 75.1 % | 0.2 % |
| pairs of different people suggested | 38 of 35,523 | **2 of 35,523** | 0 | 0 | 0 |

0.40 is used: a suggestion is only a suggestion (a person confirms the merge), so a missed fold costs more than a spurious one. At 0.75 the old setting suggested 0.2 % of them: essentially nothing.

## What this does not show

* LFW is photographs of public figures, mostly adults, mostly frontal: it is **not your users' photographs**. Phone photographs in poor light, children, large age gaps and
  **relatives who look alike** are not in it; look-alikes are the reason `match` is kept stricter than `new_group` and a margin is kept. Run the command on your own labelled photographs
  (`<person>__<n>.jpg`) before trusting the figures for a deployment; it reads files and writes nothing.
* One benchmark, one model checkpoint, one detector setting. The album simulation uses the largest face of each photograph (the pipeline groups every face that passes the face check).
* The percentages are measured; the choice of the 1st percentile and of 0.01 % is a judgement (they are options of the command: `--new-group-percentile`, `--match-far`).

## Reproducing and repairing

```
python manage.py evaluate_identity_thresholds /path/to/photos --json result.json      # one folder per person, or <person>__<n>.jpg, or --csv labels.csv
python manage.py regroup_identities --owner <id>                                       # dry run: what the new limits would do to one account
python manage.py regroup_identities --owner <id> --apply                               # does it (merges, never deletes; renamed or confirmed folders are left alone)
python manage.py cleanup_rejected_folder_members                                       # dry run: rejected photographs still in folders
python manage.py cleanup_rejected_folder_members --apply
```

On the development database (read-only dry run): owner 1 has 2 folders of 7 and 2 faces plus 2 faces waiting for review; with the new limits they would be one folder of 11 and nothing
waiting. Owner 4 has 3 folders of 1; two of them would merge. Nothing was applied.


## Update 2026-11-04: `new_group` 0.30 -> 0.36

**What was seen.** On the owner's account two different men scored **0.312** against each other. That is above `new_group` (0.30) and below `match` (0.45), so the face was not given a folder: it
waited for review "suggested for" the other man's folder. The same photograph had been uploaded four times, and none of the four got a folder (a face in the review band is compared with
folders only, never with another face waiting for review), so the folder page listed four photographs of one man as "suggested, 31 % match" beside the other man's two.

**Why 0.30 was too low.** It is the 1st percentile of the *same-person* distribution (see above), chosen to lose 1 % of true matches; but on the *different-people* side the same figure is the
99th percentile (0.297): **about 1 % of different-people pairs reach 0.30**, and a review list full of other people is what the person saw. 0.363 is the cosine at which the model's own authors
call two faces the same person (`face_recognition_sface/sface.py` in the OpenCV model zoo: `_threshold_cosine = 0.363`; checked 2026-11-04).

**New value: 0.36.** Different-people pairs at 0.40: 0.03 % (measured, table above); at 0.36 the figure lies between that and the 0.92 % at 0.30 (interpolated, not measured at 0.36). Same-person pairs below
0.36: between 1 % (0.30) and 2.3 % (0.40) of pairs (interpolated). A same person whose photograph falls under it now opens a second folder instead of waiting for review; the merge suggestion
(folder pairs from 0.40, 93 % of split folders in the measurement above) offers the merge. `match` (0.45) and `margin` are unchanged.

**Not verified.** The album simulation was run at 0.30 / 0.45 only; it was not repeated at 0.36. LFW is not phone photographs in poor light. Existing accounts are regrouped with
`python manage.py regroup_identities --owner N` (a dry run first).
