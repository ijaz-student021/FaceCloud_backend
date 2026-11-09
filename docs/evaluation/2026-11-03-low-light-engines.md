# Low-light engines on real dark photographs: Zero-DCE against Retinexformer (2026-11-03)

Question: which engine should brighten a dark photograph on the owner's machine (Apple M1, 8 GB, PyTorch on MPS)? Retinexformer (MIT code) is the engine
the router prefers when `RETINEXFORMER_ENABLED` is on; Zero-DCE (CC BY-NC 4.0, see `docs/model-licenses/commercial-inventory-2026-10.md`) is the fallback and
is what runs today.

## Method

Three real dark photographs of the owner's library (ids 137, 83, 91; originals 3456x5184, 6000x4000, 6000x4000, reduced to a 1400 px long edge), the
whole adaptive engine (`natural` / `auto`, every validator gate), in memory. Brightness is the mean grey level (0-255); noise is the standard deviation of
the grey image minus its blur (a relative measure). The result images were looked at (input, Zero-DCE, Retinexformer side by side). Machine load was 14-17
throughout (Chrome and WindowServer were using the GPU), so every time below is an upper bound.

## What was found

1. **The default tile (1024) does not work on this machine.** At a 1400 px long edge Retinexformer took 224-448 s and the engine returned the original
   (`original_preserved`; the stage was `unavailable`): the engine's 180 s budget was spent before the fallback could run. The same photograph through the
   network directly: 900 px 3.2 s (one tile), 1100 px 21 s (two tiles), 1400 px with 512 px tiles 5.4 s, with 768 px tiles 8.1 s.
2. **Tiles of 512 px show seams.** Each tile estimates the lighting by itself, so neighbouring tiles differ: a faint vertical seam on 137, rectangular
   blocks in the background of 91, blotchy colour patches on 83. The numbers (brightness, noise) looked better than Zero-DCE; the pictures did not.
3. **One pass on a reduced copy, gain applied at the full size, removes the seams** (`enhancement/lowlight.py::transfer_gain`, setting
   `RETINEXFORMER_WORK_EDGE`, default 960; 0 = the old tiled run). The per-channel gain was cleaner than a luminance-only gain (the luminance gain left a
   yellow band beside the subject of 91 and mottled colour on 83). On the CPU the one-pass run took 13.7-13.8 s for both photographs measured, the same
   image as on the GPU.
4. **MPS timing is not stable here.** The same one-pass run took 1.8-6.5 s on MPS for most photographs, 19.9 s once, and the full engine saw single stalls
   of 40 s and over 200 s while Chrome and WindowServer were drawing.

## Through the whole engine (the gates decide)

| Photograph | Zero-DCE (3 s) | Retinexformer, one pass, CPU (25-28 s) |
|---|---|---|
| 137, brightness 28.7 | 111.6, enhanced | 86.3, enhanced (the full-strength candidate was refused) |
| 83, brightness 21.2 | 38.5, `partially_enhanced` | **refused: `face_geometry_changed`, the original was kept** |
| 91, brightness 35.4 | 95.2, enhanced | 93.2, enhanced |

Outside the engine (the raw network, no gates) Retinexformer lifts all three to about 117-124. The face-geometry gate compares the face found before and after
the lift; on a low-key studio portrait the detection moves and the candidate is refused, for Zero-DCE's strongest candidate as well. The gate was not changed.

NAFNet-SIDD (denoise) never ran on these photographs: after the lift their noise was low, so the router did not ask for it. Switching it on changed nothing.

## Decision

* Zero-DCE stays the engine that runs (it finished in 2-3 s every time, never starved the budget, and was as good or better through the gates on these three).
* `RETINEXFORMER_ENABLED` stays off. The checkpoint (`Retinexformer_NTIRE.pth`) and NAFNet-SIDD are installed and match their pinned checksums.
* If it is switched on: `RETINEXFORMER_DEVICE=cpu` on this machine, keep `RETINEXFORMER_WORK_EDGE=960`.

## Not verified

* Three photographs, judged by one person (the pictures) and by the numbers; no larger set, no comparison with the owner's taste beyond these.
* Uncontended MPS timing; the figures above are with the GPU shared with the browser.
* A preferred engine that stalls inside one call can still consume the whole 180 s budget before the fallback runs (the budget is checked between tiles and
  between engines, not inside a call). Not changed here.
