# Every photograph is enhanced: why a dark portrait was refused, and what changed (2026-11-03)

Status: measured with the real models on one Apple M1 (8 GB, MPS) on the owner's portrait "dark-044-040.jpg" (id 137, 3456 x 5184, 2.4 MB, uploaded 12:26). `QUALITY_CHECKS_REJECT` is **off** by default.

## What the owner saw
"Rejected: Too blurry", sharpness 11 percent, brightness 15 percent, **0 faces found**, for a photograph of a clearly visible face that is only dark. The owner's product is image enhancement, so a
photograph that needs enhancing was refused instead of enhanced. "Every image must be enhanced, whether there is a face or not."

## Three separate causes (each measured)
1. **Darkness read as blur.** The faceless path refuses a photograph below a Laplacian sharpness of 20. That measure scales with the light: this photograph (mean brightness 28.7) measured 16.7 ("severe_blur"),
   and 84.5 (sharp, `accepted`) once brightened. It is now measured on the photograph brought to a mean brightness of 110 when it is darker than 90 (a photograph that is not dark is measured as it always was).
2. **The face detector found no face.** YuNet at the photograph's own size: score 0.93 at a long edge of 1024 px, 0.93 at 2048 px, **no face at 4096 px** (the pipeline's standardized copy) or at 5184 px, although the
   face is about 1400 px wide. Any large photograph with a large face could end with "0 faces", no person folder and no identity check. YuNet is now run at 2048 px (or the photograph's own size when smaller)
   and shrunk to 1024 px, boxes and landmarks scaled back, merged by non-maximum suppression; on this photograph it finds the face at full size and at 4096 px (score 0.93), and a 1600 px photograph is unchanged.
3. **A quality check refused it, and a refused photograph is never enhanced.** The engine itself brightens it without trouble: mean brightness **28.7 to 82.5** (natural skin tone, detail kept; 44 s at 18 MP
   without a face, 16 s with the face found). Nothing in the engine was wrong.

## What changed
* `QUALITY_CHECKS_REJECT=False`: `make_decision` never returns `rejected`; the reasons that refused (`severe_blur`, `severe_underexposure`, `severe_overexposure`, `contrast_too_low`, `resolution_too_low`) become review
  reasons and tips; the verdict is `needs_review` (a personal workspace: accepted with tips, enhanced and grouped; a team: its moderator). Only a file that cannot be read is still refused, at upload.
  Old rows stored as `rejected` follow the rule (`effective_decision`, `effective_q`, eligibility, automatic enhancement, display status); `reconsider_rejected_photos` re-runs the face and quality checks for them.
* **A dark scene is lifted whatever the style** (it was left "as photographed" unless AI Enhance or Strong was chosen). The validator's gates still decide. The owner's low-key portrait (id 133), whose lift used to
  be refused, is now lifted: Zero-DCE's two variants were refused ("subject not clearer or too bright"), the classical exposure engine that follows passed every gate (landmark shift 0.0195, face range 162 to 167).

## Tests
Old tests about refusing a photograph now run with `QUALITY_CHECKS_REJECT=True` (the option is kept). New: `quality_control/test_never_reject.py` (the decision never refuses; the old behaviour is one setting away; a dark
photograph is not blurry; old rows behave), `validation/test_yunet_scales.py` (a stand-in detector that misses a large face in a large picture), `quality_control/test_reconsider_rejected.py`. The brightness-aware sharpness
is also in the reference implementation the quality-analysis equivalence tests compare against.

## Limits and what was not verified
* One dark portrait and one low-key portrait. A nearly black photograph (mean brightness below 6) is no longer refused, but nothing can be recovered from a photograph with no signal: the lift will produce
  noise or fail its gates and the original is then kept (`original_preserved`), with the tips. This was not run on a pure black photograph in this session.
* The detector's second size can add a false face in a busy photograph; its score threshold (0.75) is unchanged. Not measured on a set of group photographs.
* In a team, such a photograph waits for its moderator (existing rule); only a personal workspace enhances it at once.
* Timings were taken at a load average of 2 to 4.
