# A low-key photograph is not brightened by the low-light step (2026-11-04)

**The owner's complaint.** The automatic enhancement of a portrait on a black background came out with a dark-grey background; the owner pulled the black back by hand (brightness -65, shadows -18, ...).

## What was measured (the owner's portrait; L = CIE lightness on the renderer's 0-255 scale)

| | black floor (median of the dark part) | subject (95th percentile) | mean difference (of 255) from the owner's hand edit |
|---|---|---|---|
| the upload | 8.7 | 136 | |
| old pipeline | **23.4** | 163 | **22.4-22.8** |
| new pipeline | **7.6-7.7** | 136-137 | **8.7-8.9** |

* In a debug run the first of the two enabled engines (Zero-DCE) had its candidates refused by the validator (`face_geometry_changed`, `identity_changed`, `subject_not_clearer_or_too_bright`) and the second (the classical OpenCV exposure lift) passed; engine names are inferred from that order, the candidate record does not carry them. Either way the black floor goes up by about 15 levels.
* `profile.lift_dark_scenes` is on by default ("the owner: the darkness should be removed"). That rule is right for a dark photograph whose subject is dim; a lit subject on a black background is the exception it did not consider.

## What was tried and dropped

1. **Lift, then put the black floor back** (the same brightness and shadows solution the editor's Auto uses). Every low-light candidate was then refused (`colour_oversaturated`, `face_geometry_changed`, `subject_not_clearer_or_too_bright`): the gates and the correction pull against each other, and the result was no lift at all with a more complicated code path.
2. **Low-key = half the pixels near black, a visible subject (95th percentile above 90).** It also caught a night photograph of a person (dark part 15 showing a brick wall and trees, subject at 97): under-exposed, not low-key, and the old lift is exactly what is wanted. With the skip on it came out unchanged and dim.

## The rule now

The low-light step does not run for a **low-key photograph with a black background**: the subject's 95th percentile is at least 120 and the dark part's median is at most L 12 in the UPLOAD (`auto_edit.is_black_backdrop_low_key`; the owner's portrait is 136 and 8.7, the night photograph 97 and 15). It applies to the Natural and Dynamic styles at the Auto, Low and Medium strengths; **AI Enhance and the High strength still lift dark scenes as asked**, and `operations_skipped` says why ("A lit subject on a black background (low key): left as photographed, because brightening it would turn the black into grey."). `ADAPTIVE_LOWKEY_SKIP_LIFT=False` restores the earlier behaviour. The editor's Auto uses the same two conditions (a dark part that is not black in the upload is left alone; a black one that was lifted is put back).

## On the rest of the owner's library (44 distinct originals, natural / auto, whole engine, same models, old and new side by side)

Two outputs changed (the owner's portrait in two versions, results 65 and 117); the other **42 are identical**. Photographs the earlier, looser criteria also read as low-key kept their lift: results 60 and 61 (dark part L 26 and 18), 87 (bright subject, dark part 26), 38 (the night photograph); 32 and 57 (dark part 0 and 2, bright subjects) did not reach the low-light step either way.

## Not verified

* **One photograph (two versions) is the only positive example**, and the two thresholds (95th percentile 120, dark part 12) are read between it (136, 8.7) and one negative (97, 15). Other portraits on black are untested; the setting undoes it.
* The new output is not brighter: the owner's hand edit also added contrast and colour (95th percentile 152, saturation +14, warmth +9). The editor's Auto produces exactly that on request (`docs/evaluation/2026-11-04-auto-edit.md`); the automatic pipeline does not apply it.
* The editor's Auto reads the nature from the enhanced photograph, so an older result whose floor the old pipeline lifted above L 40 no longer reads as low-key there.
* Synthetic photographs in CI (`enhancement/test_lowkey_floor.py`), the owner's real photographs in the lab above; the editor screen was not run.
