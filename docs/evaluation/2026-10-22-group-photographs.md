# Group photographs and small faces: before and after (2026-10-22)

What changed (feature 2 of the Google-Photos parity pack): a group photograph is accepted when at least one face is big enough to use, however many faces there are; a single small
face is no longer **rejected**; person folders take the 20 largest usable faces of a photograph. Rules in `validation/services.py`, the cap in `identities/services.py`.

## Method and limits (read before quoting)

* **The photographs are composites**: real faces (Labeled Faces in the Wild, a public research dataset, downloaded for this measurement into a temporary folder and not committed;
  only file names and numbers are in the repository) pasted on a blurred natural scene, 1 to 24 faces, large and small. The faces are real, the arrangement and the lighting are not.
  They show how the checks treat *n* faces of a known size, not how often real group photographs look like this.
* **Before** is the code of `main` with the portrait fix of PR #111 (the old group rules: review above five faces or when one face is small; reject a single small face). **After** is this branch.
  Both ran the same photographs through the **real upload endpoint** with YuNet, SFace with consent, person folders on, and (after) the automatic enhancement with the verified model set. Inline,
  one photograph at a time, in a throwaway database, on an Apple M1.
* "What the person sees" is the **effective** verdict (feature 1): a `needs_review` in a personal workspace is accepted with tips; a rejection stays a rejection. A personal workspace is what
  these runs use. In an organization a `needs_review` still goes to a moderator.
* The detector finds more boxes than the composites contain (13 for 12, 27 for 24): extra detections inside hair and clothing. They are counted as found; most are too small to use.
* **The 20-face cap did not bind in this data**: the crowd photograph had 27 detections but only 9 to 10 big enough to use (a face must be 6% of the frame, or 1.2% in a group photograph, or at
  least 160 px on its short side). The cap is covered by unit tests (`identities/test_group_photographs.py`); it is a safety bound, not something this set measures.
* One run per photograph, one machine.

## Result

| Photograph | Faces found | Before: face check → what the person sees | After: face check → what the person sees | Faces placed in folders (before → after) | Enhancement (after) |
|---|---|---|---|---|---|
| g01_one_face_large | 1 | accepted/- → accepted | accepted/- → accepted | 1 → 1 | original_preserved |
| g02_two_faces_large | 2 | accepted/- → accepted | accepted/- → accepted | 2 → 2 | original_preserved |
| g03_three_faces_large | 3 | accepted/- → accepted | accepted/- → accepted | 3 → 3 | original_preserved |
| g04_five_faces_large | 5 | accepted/- → accepted | accepted/- → accepted | 5 → 5 | original_preserved |
| g05_six_faces_one_small | 6 | needs_review/large_group_detected → needs_review | accepted/- → accepted | 5 → 5 | original_preserved |
| g06_eight_faces_three_small | 8 | needs_review/large_group_detected → needs_review | accepted/- → accepted | 5 → 5 | original_preserved |
| g07_twelve_faces_half_small | 13 | needs_review/large_group_detected → needs_review | accepted/- → accepted | 6 → 6 | original_preserved |
| g08_twenty_four_faces_all_large | 27 | needs_review/large_group_detected → needs_review | accepted/- → accepted | 9 → 9 | original_preserved |
| g09_three_faces_all_tiny | 3 | needs_review/group_face_too_small → needs_review | needs_review/group_face_too_small → accepted | 0 → 0 | original_preserved |
| g10_one_tiny_face_in_a_scene | 1 | rejected/face_too_small → rejected | needs_review/face_too_small → accepted | 0 → 0 | original_preserved |
| g11_no_face_scene | 0 | needs_review/no_face_detected → needs_review | needs_review/no_face_detected → accepted | 0 → 0 | original_preserved |

* Before, **7 of 11** photographs would have shown "In review" or "Rejected" to a person who sent them (the six- and more-face photographs, three tiny faces, one tiny face, the face-less
  scene); after, **none**: all are accepted, and every one was handed to the automatic enhancement, which kept them as they were (`original_preserved`; the composites are sharp and
  well lit, so there is nothing to fix).
* The faces placed in person folders **did not change** (same count before and after): grouping already judged each face on its own size and detector score; what changed is that the
  photograph is no longer held back by the faces that were too small.
* In the crowd photograph 17 of 27 detections were skipped as too small and 9 faces were placed, which is the intended behaviour for a crowd.

Raw rows: `evidence/2026-10-22/group-photographs-before.jsonl` and `...-after.jsonl`.
