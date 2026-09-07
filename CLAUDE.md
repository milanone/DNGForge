# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

GUI Python per editing non distruttivo di file DNG, con salvataggio **sempre e solo dentro il DNG stesso**
— nessun sidecar XMP esterno (requisito assoluto del progetto, non negoziabile).

## Running the Application

```bash
pythonw DNGForge.pyw [file.dng]
```

The optional argument auto-loads a DNG on startup.

## Dependencies

See `requirements.txt`. Currently used: `rawpy`, `PySide6`, `numpy`, `PyExifTool`, `Pillow`,
`opencv-python`. **`lensfunpy` was never added** — lens correction (roadmap item 4) turned out to
need nothing beyond two plain XMP booleans handed to Adobe DNG Converter's own lens database, see
"Lens Corrections" below.

**`opencv-python` (`cv2`) and `Pillow` history**: `cv2` was removed once rendering moved entirely
to Adobe DNG Converter (see "Adobe DNG Converter is the sole renderer" below) — every `cv2.*` call
at the time existed only inside the Python/OpenCV rendering pipeline that had just been deleted.
**Both are now back**, added when local adjustments (radial/graduated/spot) turned out not to be
rendered by Adobe DNG Converter at all (see "Local adjustments are not rendered by Adobe DNG
Converter" below) — `cv2.seamlessClone()` for spot Heal, `Pillow` for the JPEG↔numpy-array
round-trips `_render_local_adjustments_composite()` needs to blend multiple Adobe renders together.
Not a full return to local rendering — everything *except* these three local-adjustment tools still
renders exclusively through Adobe DNG Converter, unchanged.

**Adobe DNG Converter itself is also now a hard runtime dependency**, not an optional enhancement —
see the section below. `_locate_dng_converter()` checks the two standard Windows install paths;
if neither exists, the app shows a critical error and refuses to open any file at all.

**exiftool itself must be a real binary ≥ 12.15** (PyExifTool's minimum). `_locate_exiftool()`
doesn't trust plain PATH lookup — on the dev machine an old 11.91 build sits ahead of a newer
one in PATH order, so it checks `~\scoop\shims\exiftool.exe` first, then falls back to PATH,
version-checking either way. If `exiftool -ver` output ever needs re-validating on a fresh
machine, that's the function to look at first.

## Architecture

Single file (`DNGForge.pyw`), one main class: `DNGForge(QMainWindow)`.

- `rawpy.imread()` decodes the DNG; the `rawpy.RawPy` handle is kept open for the lifetime of the
  loaded file (closed on `load_dng()` of a new file and on window close). It is **not** used for
  rendering — only for reading dimensions/`camera_whitebalance` and for the raw-sensor white
  balance sampling (`sample_raw_neutral()`, see "White balance color picker" below). All actual
  pixel rendering goes through Adobe DNG Converter — see below.
- Slider changes are debounced 60ms via a `QTimer` (`_schedule_update` → `_render_preview`) so a
  fast drag doesn't trigger a render-request per pixel of mouse movement.

### Adobe DNG Converter is the sole renderer

**Every pixel this app ever displays or saves — preview and Save alike — is rendered by Adobe DNG
Converter, never by this app's own code.** `DNGForge.pyw` only ever builds `-XMP-crs:*` edit-state
tags (`_build_xmp_crs_args()`/`_build_all_edit_xmp_args()`) and hands a DNG carrying those tags to
Adobe DNG Converter; it never touches pixel data itself. This is a hard architectural rule, not a
preference, and `_locate_dng_converter()` returning `None` still makes `load_dng()` refuse to open
any file at all (a critical `QMessageBox` explains why, both at startup and on every open attempt
— see `_show_dngconv_missing_error()`) — **with one confirmed, deliberate exception**: local
adjustments (radial/graduated/spot removal) turned out not to be rendered by Adobe DNG Converter's
CLI at all, under any tag shape or ProcessVersion tried (see "Local adjustments are not rendered by
Adobe DNG Converter" below for the full investigation and the fix). For those three tools, and only
those three, DNGForge composites pixels itself — `cv2`/`Pillow` are back as dependencies for
exactly this reason. Every other control in this app still renders exclusively through Adobe DNG
Converter, unchanged; read that section before assuming "no internal rendering" is still
universally true for local adjustments specifically.

**History — this used to be a two-stage system**, and the earlier stage is worth knowing about
even though it's gone, because the reasoning explains several design choices still in the code:
1. **Original approach**: a Python/OpenCV pipeline (`apply_tone()`, `apply_curve()`,
   `apply_texture()`/`apply_clarity()`/`apply_dehaze()`/`apply_vibrance()`/`apply_saturation()`,
   `apply_sharpness()`/`apply_luminance_nr()`/`apply_color_nr()`, `apply_radial_filter(s)`/
   `apply_gradient_filter(s)`/`apply_spot_removal(s)`, `apply_crop_straighten()`, all driven by
   `_render()`) approximated what Lightroom/ACR would do for the same slider values. It was never
   fully successful — the Tone sliders in particular were repeatedly measured against a real
   Lightroom-rendered ground truth (`DSC_6751.dng`) and never closed the gap (see git/session
   history if the specific numbers ever matter again; not reproduced here since the code they
   describe no longer exists). A two-tier cache (`_linear_cache`/`_pre_local_cache`) existed
   purely to keep that expensive pipeline responsive during slider drags.
2. **Hybrid approach**: rather than keep tuning approximations, the *live preview* was routed
   through a background Adobe DNG Converter pipeline (`DngConverterWorkCopyThread`/
   `DngConverterRenderThread`, both still present, see below) while `save_to_dng()` kept using the
   old Python pipeline for its embedded JPEG — an intentionally temporary state, flagged in this
   file at the time as "outstanding, not yet decided."
3. **Current approach (this section)**: at the user's explicit instruction — full parity with
   Lightroom/ACR rendering matters more than keeping a same-machine fallback, and Adobe DNG
   Converter is a free tool anyway — the entire Python/OpenCV rendering pipeline was deleted.
   `save_to_dng()` and `export_preview_jpeg()` were migrated onto the same Adobe DNG Converter
   mechanism the preview already used (full resolution, not the small working copy — see their own
   docstrings). Nothing renders locally anymore, preview or save.

**What this fixed for free, as a direct consequence of switching renderers, not extra work**: every
approximation gap this file used to document at length — the Tone sliders never matching Lightroom,
`apply_exposure_and_gamma()`'s hand-rolled linear-light exposure math, the radial/gradient filters'
local Exposure being gamma-space-only instead of proper linear-light (see those sections' own
former "Scope limits" notes) — is gone, because Adobe's own engine now does all of it for real.
There is no longer a "does this slider value mean the same thing as it does in Lightroom" question
to answer per-control; the answer is always yes, because Lightroom-compatible XMP tags are the
*entire* interface between this app and the pixels it shows.

**What's still worth watching**: the local-adjustment persistence schemas
(`CircularGradientBasedCorrections`/`GradientBasedCorrections`/`RetouchAreas`, see their own
"persistence" sections below) were reverse-engineered from exiftool's `XMP.pm` source, and only the
graduated filter's schema is confirmed against genuine Lightroom-saved output — the radial filter's
`Angle`/`Midpoint`/`Roundness` and, more importantly, **the spot-removal schema's `SourceX`/
`OffsetY` naming are still unverified against a real Lightroom file** (see "Spot Removal" below).
Previously, a schema mistake there would still have shown *something* in DNGForge's own preview
(the local cv2 Heal/Clone implementation ran regardless of what got written to XMP); now, the XMP
tags are the *only* thing that produces a rendered result — if the spot-removal schema is subtly
wrong, placed spots may render incorrectly or not at all in the Adobe-rendered preview and save,
with no local fallback to fall back on. Worth testing against a real Lightroom-opened file before
trusting this feature for real work.

### CONFIRMED BUGS, fixed: Curve and Profile silently had zero rendering effect

Reported by the user right after the Adobe-only migration: "la sezione Curve non ha più effetto
reale." Root-caused by direct experimentation with `exiftool` + `Adobe DNG Converter.exe`, bypassing
this app entirely, then confirmed against RethinkRAW's own Go source and its actual running server
(`C:\Users\milan\RethinkRAW`, launched locally and queried directly over HTTP) — not assumed from
reading code alone. Two independent, unrelated bugs, both real limitations of what a bare
`-XMP-crs:Tag=value` write can make Adobe DNG Converter's renderer actually do:

**1. Curve — `ToneCurvePV2012` needs to be written as a real XMP list, not a flat string.**
Writing `-XMP-crs:ToneCurvePV2012=0, 0; 32, 16; 64, 50; 128, 128; 192, 202; 255, 255` (no special
exiftool option) produces `crs:ToneCurvePV2012="0, 0; 32, 16; ..."` — a single flat XML attribute.
Confirmed this has **zero rendering effect no matter what values are used**, even an extreme
inverted curve (0→255, 255→0) that should be unmistakable: rendered output was byte-identical to
no curve at all, whether via this app's own pipeline or a raw `Adobe DNG Converter.exe -c -p2` run
by hand. The real on-disk structure — confirmed by dumping the raw XMP packet from a file
RethinkRAW itself had just written — is an `rdf:Seq` list, one `<rdf:li>0, 0</rdf:li>` per point,
not a flat attribute. RethinkRAW produces this by passing exiftool the global `-sep "; "` option,
which tells it to split any `"a; b; c"`-valued write for a List-type tag into real list items.
Fixed the same way: `_build_xmp_crs_args()` now starts every call with `-sep`, `"; "` (see
`_format_curve_points()`), and named curve presets (Medium/Strong Contrast) now also send their
actual point list — previously only `ToneCurveName2012=Medium Contrast` was written with no
points at all, and Adobe DNG Converter doesn't know what a named preset's shape is without them
(unlike Lightroom itself, which has that lookup built in). **Verified**: Linear vs. Strong
Contrast now produces a real, measurable difference (pixel std 63.7 → 72.7, mean abs diff ≈8.7) —
matching RethinkRAW's own live server response to within rounding (std 62.96 → 71.92, diff ≈8.72,
queried directly via its `?toneCurve=...` preview endpoint) — through this app's actual render
pipeline, not just an isolated exiftool test.

**2. Profile — the 8 named "Adobe Color/Vivid/…" profiles are not resolved from a bare name at
all.** `CameraProfile=Adobe Vivid` alone produced a byte-identical render to no profile whatsoever
— confirmed the same way as the curve bug. These names are **not** DCP camera-profile files Adobe
DNG Converter looks up (real per-camera DCP profiles live under
`C:\ProgramData\Adobe\CameraRaw\CameraProfiles\`, installed by Photoshop/Lightroom/the Camera Raw
plugin — absent on a machine with only the standalone DNG Converter, as this dev machine confirms).
They're Adobe's built-in, camera-*agnostic* "Look" presets, resolved internally via a fixed
`LookUUID` + `LookTable` hash baked into Adobe's own rendering engine, not by name. Real
Lightroom/ACR writes the full `LookName`/`LookUUID`/`LookParameters*` struct (including each
look's own embedded tone curve — same list-structure requirement as bug 1) rather than relying on
`CameraProfile` for these 8 names at all. RethinkRAW's `profiles.go` has all 8 UUID/LookTable/curve
constants hardcoded (the only source found for these otherwise-undocumented values — ported
verbatim into `PROFILE_LOOK_SETTINGS`, see that constant's own docstring). `CameraProfile` and
every `Look*`-prefixed tag are always cleared first for any named profile (wildcard `Look*=`, same
"always write the off-state" convention this app already uses elsewhere), so switching from one
named profile to another can't leave a stale `LookParameters*` from the previous one stranded.
**Verified**: Adobe Color vs. Adobe Vivid now produces a real difference (std 72.7 → 73.8, mean
abs diff ≈1.8) through this app's own render pipeline — smaller than the curve fix's effect (Vivid
is a subtler look than Strong Contrast), but confirmed non-zero and in the expected direction.

**"Adobe Standard"/"Adobe Standard B&W" have no `LookName`/curve of their own** (matching
RethinkRAW exactly) — they only clear `ConvertToGrayscale`/`CameraProfile`/`Look*`, `Standard B&W`
additionally setting `ConvertToGrayscale=True`. This is Adobe's own genuinely "no look" baseline,
not a gap in the port.

**3. Follow-on bug caught by testing the round-trip, not just the render**: fixing the write side
initially broke *reading a saved profile back* — reopening a file saved with "Adobe Vivid" showed
"Adobe Standard" in the Profile combo instead. Two stacked causes, found by directly inspecting
`read_combined_edit_tags()`'s own return value rather than assuming the fix was complete once
rendering worked:
- `_apply_xmp_crs_state()` used to read the profile name from `CameraProfile` — now always cleared
  for every named profile (see bug 2 above), so it read nothing and fell back to a wrong default.
  Fixed to check `LookName` first, matching RethinkRAW's own read-side resolution: an ambiguous
  read (no `LookName`, no `ConvertToGrayscale`) now resolves to "Adobe Standard" rather than
  guessing "Custom" — the two are visually identical anyway (neither applies a Look).
- `LookName` itself doesn't survive exiftool's `-struct` mode as a flat tag — requesting it by that
  flattened name under `-struct` (needed for the radial/gradient/spot struct reads in the same
  combined call) silently returns nothing; the real data only appears under the struct name
  `Look` (`{'Name': ..., 'UUID': ..., 'Parameters': {...}}`), confirmed by dumping `-XMP-crs:all`
  under `-struct` directly. `read_combined_edit_tags()` now also requests `XMP-crs:Look`
  explicitly, and `read_xmp_crs_state()` falls back to `Look.Name` when the flat `LookName` lookup
  comes back empty — same "flat tag collapses/disappears under -struct" class of gotcha already
  documented for `CircularGradientBasedCorrections`/`GradientBasedCorrections` under "Radial
  filter persistence" below, just the opposite direction (there, struct mode was *required* to see
  more than the last region; here, struct mode hides a tag that's visible without it).

Also fixed: selecting **Custom** now explicitly clears `CameraProfile`/`Look*` too, not just omits
writing anything — otherwise switching *away* from a previously-saved named profile back to Custom
left its `LookName`/`LookParameters*` stranded in the file. **Verified end-to-end** (a real
`save_to_dng()` → close → reopen in a second independent `DNGForge` instance, not a mock): saved
with Profile=Adobe Vivid + Curve=Strong Contrast, reopened, both combos correctly show "Adobe
Vivid"/"Strong Contrast" again; separately, saved with Adobe Vivid then immediately re-saved with
Custom, confirmed `LookName`/`LookUUID`/`LookParametersLookTable`/`CameraProfile` are all absent
from the file afterward, not just overwritten with empty strings.

**Investigation method worth reusing if a future control ever looks similarly inert**: don't trust
"the tag looks right" — render two extreme, obviously-different values through Adobe DNG Converter
directly (bypassing this app) and diff the actual output pixels. A tag that writes and reads back
fine through exiftool can still be silently ignored by Adobe's renderer if its on-disk *shape*
(flat string vs. list, bare name vs. full struct) doesn't match what Adobe's engine actually
expects — exiftool will not warn about this, since the write itself is perfectly valid XMP.

### Local adjustments are not rendered by Adobe DNG Converter — the one real exception to "Adobe DNG Converter is the sole renderer"

**Reported by the user**: "ora i filtri radiale e graduato e la rimozione macchie non fanno niente"
— confirmed real, and root-caused by exactly the investigation method above, pushed further than
usual because the first several rounds of results were all negative. This is the one place the
"Adobe DNG Converter is the sole renderer" architecture (see above) turned out to be wrong, not
just under-verified: **the standalone Adobe DNG Converter CLI does not apply
`CircularGradientBasedCorrections`/`GradientBasedCorrections`/`RetouchAreas` (radial/graduated/
spot) when generating a preview or converting a file, at all, under any configuration found.**

**What was ruled out, exhaustively, before concluding this was a genuine tool limitation and not a
schema mistake:**
1. This app's own generated struct (`build_radial_xmp_value()`'s output) — no effect.
2. The *exact same* struct, byte-for-byte, copied from `DSC_6751.dng`'s own genuine
   Lightroom-written `GradientBasedCorrections` — no effect, even with `LocalExposure2012` pushed
   to an absurd +4.0 (a real Lightroom file's real values *did* originally look like they rendered
   differently when the tag was stripped vs. present — until a proper isolated A/B test, both
   built from the *same* original XMP rewrite rather than one pristine/one-rewritten, showed the
   file with the tag *removed entirely* and the file with `LocalExposure2012=+4.0` are
   byte-identical: the earlier apparent difference was `exiftool` incidentally touching something
   else — plausibly crop/Upright reinterpretation — when rewriting the packet, not the gradient).
3. Lightroom's *exact* XML serialization style (`<rdf:Description crs:What="Correction" .../>`
   attribute form, lowercase `true`) injected as a raw XMP packet via `exiftool "-xmp<=file.xmp"`,
   bypassing exiftool's own struct-literal writer (which instead produces `rdf:parseType='Resource'`
   with child elements) entirely, in case the *shape* — not the content — was the problem, the same
   class of bug as the Curve/Profile fixes above. No effect.
4. `MaskGroupBasedCorrections` — the newer, LR-11-era (2022) unified masking tag that superseded
   `GradientBasedCorrections`/`CircularGradientBasedCorrections` in Lightroom's own UI, in case the
   rendering engine only consults the modern structure now. No effect either.
5. Every `ProcessVersion` from 5.7 (pre-2012, legacy tone tags) through 11.0 (this app's own
   default) — no effect at any of them. (5.7 briefly looked promising in a naive A/B — because
   PV5.7 itself renders the whole frame differently via its own legacy tone-mapping, not because
   the correction did anything; a same-PV, with-vs-without-correction-only comparison at 5.7 also
   came back byte-identical, same as every other PV.)

Adobe DNG Converter does at least *preserve* these tags — reconverting a file that already has one
copies it through into the new file's own XMP unchanged — so it isn't stripping/rejecting them as
invalid, it simply never consults them when rasterizing a preview. Very plausibly a deliberate
scope limit of the standalone converter (a batch/bulk raw→DNG tool) rather than a bug: the full
masking/local-adjustment compositing engine may only ever run inside the interactive Camera
Raw/Lightroom application, not the CLI conversion utility.

**Photoshop was seriously considered as an alternative renderer** (full Camera Raw engine, so it
*would* render these correctly) — the machine has a licensed copy, but it's Photoshop CS2 (2005),
which predates Process Version 2012 (2012), the graduated filter (2010), the radial filter (2013),
and the modern masking system (2022) entirely; its bundled Camera Raw 3.x has no concept of any
tag this app writes. Ruled out for lack of a suitable Photoshop version, not for architectural
reasons — if a current Creative Cloud Photoshop is ever available on this machine, COM automation
(`win32com.client.Dispatch("Photoshop.Application")`) reusing the *exact same* XMP this app already
writes would be worth revisiting as a strictly-more-accurate replacement for the approach below.

**The fix: `LocalAdjustmentRenderThread` / `_render_local_adjustments_composite()`** — composite
two-or-more real Adobe renders per file instead of trying to get Adobe to understand the mask tags
at all:
1. Render the frame once with the current global settings ("base").
2. For each active radial/graduated region with a non-zero Exposure/Contrast/Saturation delta,
   render the *same* frame again with that delta added to the corresponding *global* tag
   (`Exposure2012`/`Contrast2012`/`Saturation`) — mirroring what "local" actually means in
   Lightroom's own model, a delta on top of the base state, not an independent value.
3. Alpha-blend each region's render into the base, pixel by pixel, using that region's own mask
   geometry: `_gradient_alpha_map()` is an *exact* linear ramp between the zero/full points (no
   approximation — confirmed the correct definition against `DSC_6751.dng`, see "Graduated filter
   persistence" above); `_radial_alpha_map()` uses the same inverse-rotation ellipse math as
   `ImageLabel`'s own on-screen overlay, with an approximated linear feather falloff (Feather's
   *exact* Lightroom curve was never confirmed either way, same open caveat as before — now visible
   in rendered pixels instead of only in the overlay). **Every blended pixel is a real Adobe
   render at one of its two endpoints (0% or 100% of the region's delta) — only the shape of the
   transition *between* them is approximated** (a linear blend in output-pixel space, not a
   re-derivation of Adobe's tone-mapping curve) — a fundamentally different, much smaller kind of
   approximation than the pre-Adobe-only local renderer's gamma-space exposure multiply.
4. Spot removal needs no extra Adobe render at all — `_apply_spot_heal()`/`_apply_spot_clone()`
   (real `cv2.seamlessClone()` / a feathered copy, the exact pre-Adobe-only algorithms, brought
   back) run directly on the already-composited pixels, since Heal/Clone are pure pixel-copy
   operations, not photometric ones.

**Crop interaction**: `radial_filters`/`gradient_filters`/`spots` are stored as fractions of the
*full raw frame* (same convention `HasCrop`'s own `Top`/`Left`/`Bottom`/`Right` uses), but Adobe
only ever renders the *effective crop*. `DNGForge._local_filters_for_render()` remaps every
region's fractions into the rendered frame's own fractions before handing them to the compositor
— the same transform `_region_within_effective_crop()` already does for the zoom-detail pipeline's
single rectangle, just applied to points+lengths for three different filter shapes. A **rotated**
effective crop isn't handled (mapping a sub-rectangle through a rotation isn't implemented, same
scope limit `_region_within_effective_crop()` already has) — compositing is skipped entirely in
that case, falling back to the plain single-render `DngConverterRenderThread` path, same graceful
degradation already used elsewhere in this app rather than showing a misplaced region.

**Auto Tone interaction**: while Tone Mode is Auto, `Exposure2012`/`Contrast2012` aren't written at
all (Adobe computes its own values) — there's no known base value to add a region's delta to, so
`base_exposure`/`base_contrast` are treated as `0.0` for the purpose of building each region's
render (the delta alone, on top of whatever Auto decides). A known, accepted edge-case
approximation, not resolved further.

**Cost**: one extra full Adobe DNG Converter conversion per active (non-zero-delta) region, on top
of the base render — for live preview this means a slower settle the more regions are active
(already asynchronous/background, so it doesn't block the UI, just delays the next visible update);
for `save_to_dng()`/`export_preview_jpeg()` (already synchronous, blocking calls the user expects
to wait on) it means a proportionally longer save/export. `LocalAdjustmentRenderThread` is only
ever instantiated when `radial_filters`/`gradient_filters`/`spots` is non-empty — the common
no-local-adjustments case is completely untouched, still the single-render `DngConverterRenderThread`
path exactly as before this fix.

**Known limitation, not addressed**: overlapping regions aren't composited *sequentially* the way
Lightroom would (each region's render reflects its own delta *relative to the base*, not relative
to a previously-composited region) — a visible seam is possible in an overlap zone between two
active regions. Accepted as an edge case; the common case (one graduated filter, the feature this
fix was specifically requested for) is unaffected.

**CONFIRMED BUG, fixed same day: the zoom-detail pipeline wasn't wired to any of this.** Reported
directly by the user right after confirming the fix above worked ("rimozione macchie però non
funziona" → "trovata la causa: non c'è effetto visivo se sono in uno zoom"). Root cause: only
`_start_dngconv_render()` (the main "Fit" preview pipeline) had been updated to route through
`LocalAdjustmentRenderThread` — `_start_zoom_detail_render()` (the separate pipeline that takes
over once zoomed in past `ZOOM_DETAIL_THRESHOLD`, see "Zoom detail rendering" below) still went
straight through the plain `DngConverterRenderThread`, unaware local adjustments existed. So a
placed spot/radial/gradient correctly showed its effect at Fit zoom, then appeared to vanish the
moment the user zoomed in — not a rendering bug in the compositing logic itself, just one call
site out of two that needed the same fix. Fixed the same way: `_start_zoom_detail_render()` now
checks `radial_filters`/`gradient_filters`/`spots` and branches to `LocalAdjustmentRenderThread`
(with the same `side` parameter it already computes for native-resolution detail requests) exactly
like `_start_dngconv_render()` does, reusing the same `_local_filters_for_render(for_save=False)`
crop-remap — no new geometry logic needed, since the alpha-map functions are already resolution-
agnostic (they scale by whatever `w, h` the decoded render actually comes back as). **Verified**:
rendered the same zoomed-in region with a +3.0 EV radial filter active vs. an identical file with
no filter — previously would have been byte-identical (same bug class as the original Adobe DNG
Converter finding, just via a different code path); now shows a real, non-zero difference
(mean|diff|≈1.2, max 44 at this specific pan position — smaller than the Fit-view test's ≈18
because this particular viewport happened to sit nearer the filter's feathered edge than its
core, not because the fix is weaker there).

**Verified end-to-end**, real Adobe DNG Converter renders throughout, not mocked:
- `_gradient_alpha_map()`/`_radial_alpha_map()` unit-checked directly (exact 0/0.5/1 ramp at the
  zero/midpoint/full points for gradient; 1 at center, 0 at corners, correct invert, correct
  hard-edge-vs-feathered transition for radial).
- A graduated filter with a +2.5 EV region rendered through `_render_local_adjustments_composite()`
  directly: mean|diff| against baseline ramps smoothly from ~0.4 at the zero line, to ~26 at the
  midpoint, to ~59 at the full line, staying at a comparable (scene-content-dependent) magnitude
  beyond it — the correct shape of a real linear gradient, not a step function and not a
  whole-frame effect (the exact bug the earlier, wrong "Adobe renders this" assumption had been
  hiding).
- Placed a radial filter (+2.0 EV) through a real `DNGForge` instance, previewed, saved, and
  compared the saved file's own embedded `JpgFromRaw` against an identical file saved with no
  filter: mean|diff|≈18, max 220 — a real, substantial, visible effect in the *actual saved file*,
  where before this fix it was byte-identical regardless of the filter's magnitude.
- Same end-to-end save-and-compare for a graduated filter (mean|diff|≈50) and a Heal spot removal
  (mean|diff|≈2.1, max 255 — small mean as expected since a spot only affects a small circular
  area, but a real, non-zero, correctly-localized effect).
- Crop + radial filter together: committed a crop to the right half of the frame, placed a radial
  filter at full-frame x=0.75 (the horizontal center of *that* crop), saved, and confirmed the
  diff against a crop-only baseline is concentrated at the horizontal center of the *cropped
  output* (mean≈53) rather than its edge (mean≈8) — confirming `_local_filters_for_render()`'s
  coordinate remapping lands the region in the geometrically correct place post-crop, not just
  "somewhere in the frame."
- No-local-adjustments regression check: a plain exposure edit with no filters present, and a
  radial filter present but with all-zero Exposure/Contrast/Saturation (a legitimately placed but
  not-yet-adjusted region), both produced identical output to the pre-this-fix behavior — the
  no-op-region skip (avoiding a wasted extra render) and the simple-path fallback both confirmed
  not to regress the common case.

### Background Adobe DNG Converter render pipeline (`DngConverterWorkCopyThread`, `DngConverterRenderThread`)

Runs the live preview through Adobe DNG Converter asynchronously so slider dragging stays
responsive despite each render being a real external-process conversion (unlike the old in-process
cv2 pipeline, this genuinely cannot be made to run in milliseconds). Mirrors RethinkRAW's own
two-stage design (`edit.go`'s `previewEdit()` — see project memory `dngforge-rethinkraw-research`):
build one small downscaled working copy per file, then re-convert only *that* small file on every
settle, rather than re-running the full-resolution original for every slider move.

- **`DngConverterWorkCopyThread`** — runs once per loaded file (kicked off from
  `_reset_dngconv_pipeline()`, called at the top of `load_dng()`): `-c -lossy -side 2560` produces
  `work.dng` (2560px long edge) in a fresh per-file temp dir (`tempfile.mkdtemp(prefix="dngforge_work_")`).
  Measured ~0.8s on the dev machine — converting the small copy on every subsequent settle is
  correspondingly fast, where converting the full original every time would take seconds.
- **`DngConverterRenderThread`** — runs on every debounced slider settle (`_schedule_dngconv_render()`,
  called from `_render_preview()`, itself driven by the same 60ms debounce timer already used for
  undo snapshots): writes the current edit state onto `work.dng` as `-XMP-crs:*` tags via one
  `exiftool` call (`_build_all_edit_xmp_args()` — `_build_xmp_crs_args()` plus the radial/gradient/
  spot struct tags, factored out so `save_to_dng()`/`export_preview_jpeg()` and this pipeline can
  never drift into writing different tag sets for "the same" edit state), reconverts with `-c -p2`,
  and extracts the result via the shared `_extract_dngconverter_preview()` helper (also reused by
  `save_to_dng()`/`export_preview_jpeg()` for their own full-res renders).
  **Extracts `-JpgFromRaw`, falling back to `-PreviewImage` only if that's absent** — the exact same
  multi-preview-size DNG structure documented under "Save" below (`JpgFromRaw`/SubIFD2 vs
  `PreviewImage`/SubIFD1), confirmed by direct measurement on real Adobe DNG Converter output:
  `PreviewImage` came out only 1024×680 here, while `JpgFromRaw` was the full working-copy
  resolution (2560×1700) — extracting `PreviewImage` first would have silently swapped in a
  lower-resolution preview than intended. Binary extraction goes through a plain
  `subprocess.run([..., "-b", ...])` call rather than `ExifToolHelper`'s persistent stay-open
  process, since that protocol is text/JSON-oriented and isn't reliable for pulling raw binary
  bytes back out. `rendered.dng` is deleted once its preview JPEG has been extracted.
- **Generation counter, single-in-flight-plus-one-pending-flag queueing**: `_dngconv_generation`
  is bumped in `_reset_dngconv_pipeline()` and `_start_dngconv_render()`; a render thread's `ready`/
  `failed` signal carries the generation it was started with, and `_on_dngconv_render_ready()`
  ignores a stale result (`generation != self._dngconv_generation`) — so a slow in-flight render
  whose settings the user has since changed is silently discarded rather than momentarily flashing
  outdated state onto the display. Never more than one Adobe DNG Converter process runs at a time:
  if a slider settles while a render is already in flight, `_dngconv_pending` is set instead of
  starting a second one, and the just-finished render's own completion handler
  (`_on_dngconv_render_ready()`/`_on_dngconv_render_failed()`) immediately kicks off one more render
  with the latest state — always converges to the current settings without ever queuing more than
  one extra run.
- Wired into `DNGForge` via `_reset_dngconv_pipeline()` (`load_dng()`), `_schedule_dngconv_render()`/
  `_start_dngconv_render()`, `_on_dngconv_render_ready()`/`_on_dngconv_render_failed()`. Per-file
  temp dirs aren't deleted immediately on switching files (a background thread may still be using
  one) — they accumulate in `self._old_work_dirs` and are swept with `shutil.rmtree` in
  `closeEvent()`.

**Since there is no internal fallback rendering path, `_render_preview()` doesn't draw anything
synchronously** — it only calls `_schedule_dngconv_render()`. `self._preview_pixmap` starts (and,
for a freshly opened file, stays) `None` until the first Adobe render actually lands
(`_on_dngconv_render_ready()` sets it); `_update_image_label()` and every other reader treat `None`
as "nothing to show yet," with the status bar carrying "Rendering anteprima Adobe DNG Converter…"
in the meantime. A failed background render (`_on_dngconv_render_failed()`) leaves whatever was
already on screen untouched rather than replacing it with nothing.

**Crop guide vs. committed framing is honored by the Adobe pipeline too**: while the crop guide is
being edited, `_build_xmp_crs_args()` writes the *full-frame* crop (via `_effective_crop_for_render()`,
not `self.crop` directly) so the preview shows the whole straightened frame behind the rectangle
overlay, exactly like Lightroom's own Crop tool — see "Crop & Straighten" below.
`_schedule_dngconv_render()`'s dedup key additionally pairs `_capture_edit_state()` with
`self._crop_editing` specifically because of this: toggling the guide checkbox alone changes
nothing `_capture_edit_state()` itself snapshots (it's deliberately not part of Undo/Redo history —
it isn't a real edit), but it *does* change what gets written for the crop tags, so without this
the toggle would silently fail to trigger a fresh render. Verified end-to-end (a real Adobe DNG
Converter run, not a mock): loading a file, waiting for the first render, changing Exposure and
confirming a new render lands, and toggling the crop guide and confirming that also triggers a
render, all pass against `RAW_NIKON_D90.dng`.

**Console-window flash fix**: every `subprocess.run()` call in this file passes
`creationflags=_NO_WINDOW_FLAGS` (`_NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)`,
module-level) — the user noticed a transient command-window flash on every Adobe DNG Converter
invocation, contrasted unfavorably with RethinkRAW's own clean behavior.

**Spinner overlay while rendering** — user-requested after noticing RethinkRAW.exe's own
rendering indicator ("un cerchio a pallini al centro dell'immagine... è visivamente molto
efficace"): `ImageLabel` gained a small self-contained animated spinner (`start_spinner()`/
`stop_spinner()`/`_advance_spinner()`, drawn by `_paint_spinner()` in the same `paintEvent()`
that already draws the radial/gradient/crop/spot overlays) — a ring of `SPINNER_DOT_COUNT` (12)
dots on a `SPINNER_RADIUS`-px circle centered on the label, one bright "lead" dot with the rest
fading out behind it, advanced one dot every 80ms by a `QTimer`, over a soft translucent dark
backdrop circle so the dots stay visible regardless of what's under them (confirmed against a
busy, brightly-colored test photo, not just a plain background). Purely visual — same
"something is happening" signal the status bar's "Rendering anteprima Adobe DNG Converter…" text
already gave, just more RethinkRAW-like.

Wired into the single choke-point the main preview pipeline already funnels every render
through: `start_spinner()` in `_start_dngconv_render()`, `stop_spinner()` in
`_on_dngconv_render_ready()`/`_on_dngconv_render_failed()` — but only on the branch that does
*not* immediately re-arm via `_start_dngconv_render()` for a pending re-render (single-in-flight-
plus-one-pending queueing, see above), so a settle-into-next-render never flickers the spinner
off and back on. `start_spinner()` is itself idempotent (a no-op if already running) for the same
reason. This piggybacks entirely on the existing generation-counter/pending state machine — no
new bookkeeping needed, same principle already used elsewhere in this pipeline (e.g. the crop
guide dedup key above). Covers the very first render of a freshly opened file for free, too
(`_preview_pixmap` starts `None` until the first render lands — previously a blank area with
only a status bar message, now the spinner as well), since that render also goes through
`_start_dngconv_render()`.

**Now covers the zoom-detail pipeline too** (`_start_zoom_detail_render()`/
`_on_zoom_detail_render_ready()`/`_on_zoom_detail_render_failed()`, see "Zoom detail rendering"
below) — added right after the main-pipeline version shipped, at the user's explicit follow-up
request ("vorrei lo spinner anche quando viene renderizzato uno zoom"). That pipeline already has
its own separate status-bar text ("Rendering dettaglio zoom…"), kept as-is; the spinner is purely
additive on top of it.

**Two independent pipelines sharing one spinner needed a real coordination mechanism, not just a
second pair of start/stop calls.** The obvious approach — call `start_spinner()`/`stop_spinner()`
from each pipeline's own begin/end points, the same way the main pipeline originally did on its
own — breaks the moment both pipelines can be in flight at once (e.g. dragging Exposure while
zoomed in past `ZOOM_DETAIL_THRESHOLD` kicks off both a main re-render and a zoom-detail
re-render together): pipeline A finishing and calling `stop_spinner()` would incorrectly kill the
animation while pipeline B is still rendering. A naive call-count refcount doesn't fix this
either — both pipelines' own "settle straight into the next queued render without an intervening
stop" behavior (`_dngconv_pending`/`_zoom_detail_pending`, the same single-in-flight-plus-one-
pending queueing documented above) means `start_spinner()` can fire again without a matching
`stop_spinner()`, so paired counting drifts out of sync over a few renders.

Fixed with `_sync_spinner()`: a single method, called after *every* point where either
`self._dngconv_thread` or `self._zoom_detail_thread` changes, that recomputes spinner state from
scratch as a plain OR of both pipelines' current thread state (`is not None`) rather than
incrementing/decrementing anything. Always correct regardless of interleaving, since it never
trusts accumulated call history — just the two threads' actual state at the moment it's called.
`ImageLabel.start_spinner()`/`stop_spinner()` themselves stayed exactly as originally built
(simple idempotent booleans); only the *callers* changed, from direct start/stop calls to routing
everything through this one coordinator.

**CONFIRMED BUG, fixed: the spinner sat off in a corner, or entirely off-frame, whenever zoomed
in and scrolled away from the middle of the image.** Originally documented above as a deliberate
scope limit ("anchored to `ImageLabel`'s own center, not the scroll area's visible viewport") —
the user hit it in real use and reported it directly ("lo spinner a volte si sposta verso un
bordo o fuori frame. dovrebbe apparire invece sempre al centro del frame"), so it needed fixing
after all, not just documenting. Root cause: once zoomed in past 100%, `ImageLabel` itself is
resized to the *full* scaled pixmap (see `_update_image_label()`), which is generally much larger
than the `QScrollArea`'s viewport — `self.rect()` (what `_paint_spinner()` originally centered
on) is the whole scrollable canvas, not what's actually on screen, so its center drifts far from
the visible area the moment the user pans away from the image's own middle. Fixed by centering on
`self.visibleRegion().boundingRect()` instead — the sub-rect of this widget actually unclipped by
the scroll area, confirmed (via a scripted check panning to several positions, offscreen platform
included) to track the real viewport position and size correctly, matching a hand-computed
scrollbar-value + viewport-size expectation to within 1px rounding at every position tested.
Recomputed fresh on every paint (and the spinner's own 80ms timer already forces a repaint that
often), so it also tracks live panning during an active render, not just its starting position.
Kept the same loose coupling used everywhere else between `ImageLabel` and its containing
`QScrollArea` (signals only — `panRequested`/`zoomRequested`, never a stored parent reference):
`visibleRegion()` is a plain `QWidget` capability, no reference to the scroll area needed at all.
Falls back to the plain widget rect only in the degenerate empty-region case (not yet shown).

Verified with real rendered screenshots (`ImageLabel.grab()`/`image_scroll.grab()`, same
convention used throughout this file for overlay verification) for both pipelines: the
main-pipeline spinner (dotted ring, fading trail, dark backdrop) renders correctly on top of a
real Adobe-rendered photo; a second screenshot grabbed mid-flight during an actual zoom-detail
render (not a forced/faked state — polled `image_label._spinner_active` until true, then grabbed)
shows the same spinner correctly overlaid on the zoomed-in preview; a third, taken *after* the
centering fix, with the view zoomed in and panned all the way to the far bottom-right corner of a
label several times larger than the viewport, shows the spinner correctly centered in the visible
viewport (a completely different part of the photo than the label's own geometric center) rather
than off-frame. Also verified programmatically: the spinner is off after the main render settles,
turns on when a zoom-detail render is kicked off, and turns back off once that render lands —
confirming `_sync_spinner()`'s OR-logic converges correctly across a real start→finish cycle of
both pipelines, not just each in isolation.

### Performance: consolidated exiftool reads on load (`read_combined_edit_tags()`)

`load_dng()` used to open **7 separate** `exiftool.ExifToolHelper()` processes per file —
`load_dng_camera_profile()`, `read_xmp_crs_state()`, `read_radial_filters()`,
`read_gradient_filters()`, `read_spot_removals()`, `read_crop()`, `read_metadata()` — each one a
fresh process spawn. Profiled on `DSC_6751.dng`: each spawn cost ~440-470ms almost entirely as
process-startup overhead (Perl interpreter init + tag table loading), regardless of how few tags
it actually requested — ~3.15s total just for metadata reads on every file open, which is what
made DNGForge's load time feel much slower than RethinkRAW despite both using the same underlying
Adobe DNG Converter rendering engine.

**Fix**: 6 of the 7 functions — every one except `read_metadata()` — already shared the same
`common_args=["-G","-n"]` (machine-parsable) convention, and `-struct` (needed by the 3
local-adjustment-filter reads) is a global exiftool option that doesn't alter non-struct tag
output. So all 6 can safely share **one** combined call: `read_combined_edit_tags()` fetches the
union of every tag those 6 functions need, with `-struct` always on, in a single
`ExifToolHelper` invocation. Each of the 6 functions gained an optional `tags: dict | None`
parameter — when `load_dng()` passes in the pre-fetched combined dict, the function parses it
directly instead of spawning its own exiftool process; passing nothing (`tags=None`) preserves
the original standalone behavior, so every function is still independently callable and testable
exactly as before. `read_metadata()` is the one function that must stay separate — it wants
human-readable print-converted values (`common_args=["-G"]`, no `-n`) for direct UI display,
a genuinely different exiftool invocation mode from the other 6.

**Verified**: profiled the isolated metadata-read portion on a disposable copy of `DSC_6751.dng`
— 540ms for the combined call vs. 2802ms for the same 6 functions called the old way (each
opening its own process) — and confirmed byte-for-byte identical results between the two paths
(camera profile, XMP-crs state, radial/gradient filters, spots, crop) via a scripted comparison,
not just "it ran without erroring." Also ran the real `load_dng()` code path end-to-end (not just
the isolated helper functions) on the same file, confirming it completes without error and
correctly restores the file's actual saved edit state (1 real gradient filter, a real crop,
`camera_profile` colorimetry) exactly as before the change.

**Any new feature that reads exiftool state on file load should be added to the tag list inside
`read_combined_edit_tags()` and given a `tags=None` parameter to accept the shared dict** — not
given its own fresh `ExifToolHelper()` instantiation called from `load_dng()`, or the
7-spawns-per-load problem will silently creep back in.

### Control panel

Section names/order (Profile → White Balance → Tone → Presence → Curve → Detail → Lens
Corrections → Effects) deliberately mirror RethinkRAW's panel layout (the reference
implementation studied for this project — see project memory `dngforge-rethinkraw-research`) —
**every** reference section is now reproduced, including Lens Corrections (see that section
below), added once reading RethinkRAW's own source showed it needed far less than originally
assumed.

- **Profile**: `profile_combo` — the 8 named profiles (Adobe Color/Vivid/Landscape/Neutral/
  Portrait/Monochrome/Standard/Standard B&W) write Adobe's built-in "Look" struct
  (`PROFILE_LOOK_SETTINGS`, see "CONFIRMED BUGS, fixed: Curve and Profile" above for why a bare
  `-XMP-crs:CameraProfile=Name` tag alone doesn't work), "Custom" writes nothing. (This used to be
  `apply_profile()`, a Python stand-in built by composing this app's own tone/saturation
  functions; gone along with the rest of the local render pipeline, see "Adobe DNG Converter is
  the sole renderer" above.)
- **White Balance**: `wb_mode` combobox — **As Shot** / **Camera Matching…** write
  `-XMP-crs:WhiteBalance=As Shot`/`Custom` respectively with no Temperature/Tint tags, so Adobe DNG
  Converter resolves the actual white balance itself from the file's own `AsShotNeutral` (the two
  modes aren't actually distinguished yet at the tag level — true camera-matching needs the EXIF
  `WhiteBalance` tag name via exiftool, not wired in until phase 5); **Auto** likewise writes
  `WhiteBalance=Auto` with no Temperature/Tint, so Adobe computes its own Auto WB — the disabled
  Temperature/Tint sliders show the same As-Shot reading As Shot/Camera Matching… use (see
  `_multipliers_to_temp_tint()`) rather than any local estimate of what Auto itself will do, never
  fed into rendering either way. This wasn't always the case: an earlier gray-world estimate
  (`_compute_auto_wb()`/`compute_gray_world_neutral()`, since deleted) tried to guess Auto's actual
  number and was confirmed, by direct measurement, to be badly wrong on some files — see "White
  balance: named presets and Auto render differently than displayed" below for the investigation,
  the numbers, and why As-Shot's real reading is the better (if still inexact) stand-in; the six
  named presets
  (Daylight/Cloudy/Shade/Tungsten/Fluorescent/Flash, standard photographic Kelvin/tint values in
  `WB_PRESETS`) and **Custom** drive the Temperature/Tint sliders directly and **always write
  `-XMP-crs:WhiteBalance=Custom`, never the literal preset name** (see that same section for why —
  a real, confirmed bug, not a stylistic choice), with the actual numbers written straight into
  `-XMP-crs:ColorTemperature`/`Tint` — Adobe DNG Converter does the actual colorimetric conversion
  from there using the file's own `ColorMatrix1/2` (see "Colorimetric white balance" below for how
  this app's own `DNGCameraProfile` mirrors that same math, used for slider *display* and the WB
  eyedropper, not for rendering). Manually nudging Temperature/Tint while a preset is selected
  flips the mode to Custom (`_on_wb_slider_changed()`), matching Lightroom-style UX. For every
  non-manual mode (As Shot, Camera Matching…, Auto) the Temperature/Tint sliders still **display**
  a plausible reading (via `_multipliers_to_temp_tint()`) even though they stay disabled and don't
  drive rendering — never a stale/default reading. **Pick White Balance** button arms
  click-to-set-WB mode (`_toggle_wb_picker()`); the next click on the image (`ImageLabel.clicked`,
  emits click position as a fraction of the displayed pixmap) calls `_on_image_clicked()` →
  `sample_raw_neutral()` → `DNGCameraProfile.neutral_to_temperature_tint()`, switches the mode to
  Custom, and sets Temperature/Tint from whatever was clicked — same "treat this point as neutral
  gray" logic as Lightroom's eyedropper. Clicking a non-neutral point (tested: a painted purple
  background) produces a strongly color-cast result — that's correct behavior, not a bug; the tool
  does exactly what was asked, garbage-in-garbage-out like any real WB eyedropper.

### White balance color picker (`sample_raw_neutral()`)

Reads **raw, pre-demosaic sensor data** around the click (not the rendered/demosaiced preview) —
a small block per Bayer channel, black-level-subtracted, green-normalized to the same
`[R/G, 1, B/G]` convention as `AsShotNeutral`, straight into `DNGCameraProfile`. Mirrors
RethinkRAW's own 4x4-sample color picker (`computeWhiteBalance()` in `meta.go`).

**Always opens its own short-lived `rawpy.imread()` handle — never reuses `self.raw`.** Found
this the hard way: on a DNG saved with Adobe DNG Converter's `-lossy` flag (demosaics *before*
JPEG-compressing, since a Bayer mosaic doesn't compress well — the project's own
`RAW_NIKON_D90.dng` test file is one of these), there's no mosaic to read and `raw_image_visible`
raises `RuntimeError: RAW image is not flat`. That's handled (falls back to
`_sample_linear_neutral()`, which samples a WB-disabled demosaiced render instead) — but a failed
`raw_image_visible` access was also observed to corrupt LibRaw's internal call-order state enough
that a *later, unrelated* `postprocess()` on the *same* handle then throws
`LibRawOutOfOrderCallError: Out of order call of libraw function`. Since `self.raw` is the
long-lived handle the live preview depends on for the rest of the session, the picker must never
risk it — both `sample_raw_neutral()` and `_sample_linear_neutral()` take a **path** and open
their own handle, closed immediately after. (Separately, and non-deterministically, that same
`LibRawOutOfOrderCallError` was also seen appear as a background/ignored traceback with no
functional effect after several rapid open/close cycles on the Google Drive-hosted test file —
plausibly the same virtual-filesystem flakiness already documented for RethinkRAW in project
memory `dngforge-rethinkraw-research`, not something to chase further here.)

**CONFIRMED BUG, fixed:** `_sample_linear_neutral()` (the linear-DNG fallback) originally called
`raw.postprocess(user_wb=[1,1,1,1], ...)` without setting `output_color`, so it defaulted to
**sRGB** — meaning it sampled color-matrix-transformed sRGB pixels but fed them to
`DNGCameraProfile.neutral_to_temperature_tint()` as if they were **camera-native** RGB (the space
`AsShotNeutral`/`neutral` are actually defined in — a completely different space, related by the
camera's ColorMatrix, not interchangeable). Caught by the user clicking a genuinely neutral
gray/white area and getting a nonsense result (8368K, tint pinned at the +150 slider ceiling).
Fix: pass `output_color=rawpy.ColorSpace.raw`, which disables LibRaw's camera→sRGB matrix step
entirely so the sampled RGB is actually camera-native. Verified after the fix: sampling
near-neutral regions of the Nikon test file now lands close to its own As-Shot reading (~4800-
4900K, vs. As Shot's 5283K) instead of pinning at slider extremes. **If any other code path ever
needs "camera-native" pixel values from `postprocess()`, remember `output_color` defaults to
sRGB regardless of white-balance settings — disabling WB alone does not give you raw camera space.**

**CONFIRMED BUG, fixed at the time (third occurrence of the same mistake class this session) —
`_compute_auto_wb()`/`compute_gray_world_neutral()` were later deleted entirely, see "White
balance: named presets and Auto render differently than displayed" below; kept here as the
historical record of what this fix actually addressed, same convention this file already uses
elsewhere for removed code.** `_compute_auto_wb()` originally gray-world-averaged the **already
camera-WB-corrected, gamma-encoded
8-bit** preview and used that ratio *as if* it were a fresh camera-native multiplier — two stacked
errors: wrong color space (gamma vs. camera-native linear, same mistake as `WB_GAMMA` and the
color-picker bug above) *and* wrong baseline (a small correction computed **on top of** an
already-corrected image, silently discarding the camera's real multiplier instead of replacing
it). Reported by the user: a normal photo landing on 2563K, tint -107 — the temperature/tint
solver was straining to reach an extreme, barely-plausible point trying to satisfy a target
neutral the camera's real `ColorMatrix` can't produce at any sane temperature. Fixed by
`compute_gray_world_neutral()`: averages genuinely uncorrected camera-native data (reusing
`sample_raw_neutral()`'s exact mosaic/linear-DNG dual path, just averaged over the whole image
instead of a small block) and returns a proper `[R/G, 1, B/G]` neutral, same convention as
`AsShotNeutral`. Verified after the fix: Auto now lands at 4608K/-17 (Nikon) and 5142K/7 (Leica)
— both close to their own As-Shot readings, not extreme outliers. **Pattern to watch for going
forward:** any time a statistic gets computed from `postprocess()` output and then fed back into
`DNGCameraProfile`, check *both* that the space matches (camera-native linear, not gamma/sRGB)
*and* that it isn't being computed from data that already had a different correction applied.

### Slider label vs. actual value — a widget-level bug, not WB-specific (`Slider.set_value()`)

**CONFIRMED BUG, fixed.** Reported by the user opening `RAW_LEICA_M8.DNG` specifically: "il
rendering è corretto però mi dà come temperatura 13200K (ovvero il massimo)... poi quando inizio
a cambiare il valore vengono cose strane." Root cause had nothing to do with white balance
computation itself — that file simply has `WhiteBalance=Custom`/`ColorTemperature=13200` saved in
its XMP from an earlier session (predating, or otherwise outside, the Temperature slider's
2000-12000 range), and `Slider.set_value()` had a real bug: `QSlider.setValue()` clamps
internally to `[min, max]`, but the value **label** was being set from the raw, possibly
out-of-range value passed in, not from what the slider actually clamped to. So the slider handle
correctly sat pinned at its max (2000-12000 → landed at 12000), while the text next to it kept
showing the unclamped "13200" — a real, visible mismatch between what the control showed and what
it actually held. Rendering itself was never affected (confirmed: `_build_xmp_crs_args()` always
reads via `.value()`, which returns `self.slider.value()/self.scale` — the already-clamped
number, so the file was always being written/rendered with `ColorTemperature=12000`, correctly) —
only this one label was wrong, but wrong in a way that's confusing enough to look like a real bug
and to make subsequent dragging feel unpredictable (the user drags from where the handle visually
sits, but the number they were tracking was never actually the handle's number to begin with).

Fixed by re-reading `self.slider.value()` right after `setValue()` inside `set_value()`, and
formatting the label from *that* (the value the slider actually landed on) rather than the raw
input — a one-line change, but a general `Slider` widget fix, not confined to white balance:
**any caller anywhere in this app that ever calls `set_value()` with a number outside that
slider's own declared range** was subject to the identical label/handle mismatch, not just
Temperature/Tint. Verified directly: `set_value(13200)` on a 2000-12000 slider now shows "12000"
matching `.value()`, `set_value(500)` on the same slider shows "2000", and a normal in-range call
is unaffected — then reproduced and confirmed fixed on the exact real file (`RAW_LEICA_M8.DNG`,
`WhiteBalance=Custom`/`ColorTemperature=13200` genuinely saved on disk): loads now showing
"12000" instead of "13200", with the write-side XMP arg unaffected either way (`ColorTemperature=12000`
before and after this fix, since `.value()` was already correct).

### White balance: named presets and Auto render differently than displayed

**Reported by the user on `RAW_LEICA_M8_prova.DNG`**: opened with `WhiteBalance=As Shot` showing
5880K; switching to **Auto** dropped the displayed temperature to 5142K, yet the rendered image
looked *warmer*, not cooler, than 5142K should look; and repeatedly switching between the named
presets sometimes rendered inconsistently "at parity of temperature" — the same displayed Kelvin
value producing visibly different renders depending on which preset button had been clicked to get
there. Investigated the same way every other "does this tag actually do what it says" question in
this project has been (see "CONFIRMED BUGS, fixed: Curve and Profile" above) — real Adobe DNG
Converter renders, bypassing the app, diffing actual pixels — and found **two distinct, confirmed
issues**, one a genuine architectural limitation and one a real, fixed bug in this app's own code.

**1. Auto's displayed temperature was never going to match its render — expected, not a bug, but
worth quantifying.** `WhiteBalance=Auto` (no Temperature/Tint tags at all) makes Adobe DNG Converter
compute its own Auto white balance internally; the Temperature/Tint sliders shown in Auto mode come
from this app's own `_compute_auto_wb()`/`compute_gray_world_neutral()` gray-world estimate, a
*completely different, independent* algorithm — already documented above as "display-only, never
fed into rendering." What wasn't previously known was *how large* that gap could get: rendering
`WhiteBalance=Auto` versus `WhiteBalance=Custom` at this app's own displayed Auto reading
(5142K/8, on the Leica test file) produced mean|diff|≈21.8, max 254 — a huge, unmistakable
divergence, not a rounding-level mismatch. (For contrast, the equivalent check for **As Shot** —
rendering `WhiteBalance=As Shot` versus `Custom` at this app's own displayed As-Shot reading,
5881K/9 — came back mean|diff|≈0.29, max 16, i.e. genuinely accurate; `DNGCameraProfile`'s
`AsShotNeutral`-based conversion is a real, precise inverse of what Adobe does with that same tag,
consistent with the exact-multiplier round-trip already verified under "Colorimetric white
balance" below. Auto's gray-world guess has no such privileged relationship to Adobe's own,
unpublished Auto WB algorithm — there is no way to close this gap short of reverse-engineering
Adobe's proprietary logic, which is out of scope. **Accepted as a known, now-quantified limitation
of Auto mode specifically** — the display is a plausible independent estimate, not a preview of
what Auto will actually do, exactly as already documented, just now with real numbers behind it
rather than a theoretical caveat.

**2. CONFIRMED BUG, fixed: the six named presets (Daylight/Cloudy/Shade/Tungsten/Fluorescent/
Flash) silently had their explicit `ColorTemperature`/`Tint` values overridden by Adobe DNG
Converter, because this app was also writing the literal preset name into `WhiteBalance`.** This
directly explains the "same displayed temperature, different render" report. Proven with three
escalating experiments, all against `RAW_LEICA_M8_prova.DNG`, all bypassing the app:
- Rendered `WhiteBalance=Daylight` + explicit `ColorTemperature=5500`/`Tint=0` against
  `WhiteBalance=Custom` with the *identical* explicit tags: **not** byte-identical
  (mean|diff|≈2.0, max 68) — if Adobe were purely reading the numeric tags, these would have to
  match exactly, the same way two `Custom` renders at the same numbers always do (confirmed
  separately: `Custom`@5500/0 built from a copy of the Daylight-preset file and from a copy of the
  Flash-preset file rendered byte-identical, mean/max both exactly 0.0 — `Custom` mode is fully
  deterministic from its numbers alone, as expected).
- `WhiteBalance=Daylight` and `WhiteBalance=Flash` — both defined in `WB_PRESETS` with the
  *exact same* nominal pair, (5500, 0) — rendered **differently from each other** (mean|diff|≈2.0,
  max 68) despite writing byte-identical `ColorTemperature`/`Tint` tags in both cases. Only the
  `WhiteBalance` string itself differed. This is the literal bug the user described.
- Decisive test: `WhiteBalance=Daylight` with a deliberately *conflicting* explicit
  `ColorTemperature=3000` (deep tungsten range) rendered nearly identically to `Custom@5500K`
  (mean|diff|≈2.0, matching the first experiment) and nothing like `Custom@3000K` (mean|diff|≈31.7)
  — proving Adobe DNG Converter, on seeing a *named, non-Custom* `WhiteBalance` string, resolves
  its own per-camera preset value and **discards the literal ColorTemperature/Tint this app also
  sent**, the same behavior already known and documented for `As Shot`/`Auto`, just not previously
  known to extend to the six named presets too. Testing all six systematically found Shade,
  Tungsten, and Flash happened to render byte-identical to `Custom` at this app's own `WB_PRESETS`
  numbers for this specific camera (their Adobe-internal defaults apparently coincide with this
  app's generic textbook values here) while Daylight/Cloudy/Fluorescent did not (mean|diff| 2.0/1.9/
  0.57 respectively) — which is exactly why the bug looked intermittent/inconsistent to the user
  rather than affecting every preset uniformly: three presets masked it by coincidence, three didn't.

**Fix**: `_build_xmp_crs_args()`'s `WhiteBalance` tag now writes `Custom` for every named preset
too, not just for `Camera Matching…` (which already had no distinct crs concept of its own) — the
Temperature/Tint sliders are this app's actual source of truth for what the user sees on screen, so
whatever numbers they hold should be exactly what gets rendered, regardless of which preset button
was clicked to set them. `Custom` mode was already proven fully deterministic from its numeric tags
alone (see above), so this makes every preset's render match its own displayed numbers exactly, and
makes two presets that happen to share a nominal (temp, tint) pair (Daylight/Flash) render
identically too — both previously false. **Verified end-to-end**: `_build_xmp_crs_args()` through a
real `DNGForge` instance now emits `WhiteBalance=Custom` for both Daylight and Flash (previously
`WhiteBalance=Daylight`/`WhiteBalance=Flash`); a real Adobe render of both post-fix tag sets came
back byte-identical (mean/max diff both exactly 0.0, matching the already-deterministic `Custom`
baseline); `As Shot` and `Camera Matching…` are untouched (confirmed still writing `As Shot`/
`Custom` respectively with no Temperature/Tint tags). Also checked the save/reload round-trip: saved
with the `Cloudy` preset selected, reopened in a second independent `DNGForge` instance — `wb_mode`
correctly shows `Custom` (not `Cloudy`, since the file itself never records "Cloudy" as intent
anymore, only `Custom`/`6500`/`0`) with Temperature=6500K/Tint=0 preserved exactly, an intentional
and accepted trade-off — the same one `Camera Matching…` already had, and arguably a fix in its own
right: reloading a `WhiteBalance=Daylight`-tagged file before this change would have handed the
same literal-preset-name numeric-override problem straight back to the read path too.

**Follow-up, same conversation: Auto's *displayed* temperature was also confirmed misleading, and
fixed by comparing against RethinkRAW's own reference behavior.** After the fix above, the user
asked specifically about Auto: switching to it dropped the display to 5142K while the render
looked *warmer* than that — and noted that RethinkRAW, the reference implementation this project
studies, doesn't show a computed temperature for Auto at all, yet its Auto render looks similar to
its As-Shot render. Checked RethinkRAW's actual source (`reference/RethinkRAW/assets/raw-editor.js`,
`whiteBalanceChange()`): for any `WhiteBalance` value other than a name in its own preset table or
`"Custom"` — i.e. `"As Shot"`, `"Auto"`, `"Camera Matching…"` alike — it only disables the
Temperature/Tint inputs and **never computes or sets a value for them at all**, leaving whatever
was already there (typically 0, since `editXMP()` explicitly clears `ColorTemperature`/`Tint` for
every non-Custom `WhiteBalance` value on save, mirroring the same "Adobe ignores these numbers for
named/Auto/As-Shot modes" finding from the fix above). RethinkRAW never runs any local color-science
estimate for these three modes — DNGForge's own `_compute_auto_wb()`/`compute_gray_world_neutral()`
gray-world guess (see "White balance color picker" above for its own history) had no RethinkRAW
counterpart at all.

Quantified how bad that guess actually was with a real Adobe render, bypassing the app: rendering
`WhiteBalance=Auto` against `WhiteBalance=Custom` at this app's own gray-world-computed display
value (5142K/8 for `RAW_LEICA_M8_prova.DNG`) gave mean|diff|≈21.8, max 254 out of 255 — a wholesale
mismatch, not a rounding gap. Root cause: a whole-image gray-world average is a weak estimator that
breaks badly the moment a photo lacks genuinely neutral content across most of the frame, exactly
the failure mode this particular file hit. For comparison, the equivalent check for **As Shot**
(`WhiteBalance=As Shot` vs. `Custom` at its own displayed reading, 5881K/9) came back
mean|diff|≈0.29, max 16 — genuinely accurate, since `DNGCameraProfile`'s `AsShotNeutral`-based
conversion is a real, precise inverse of what Adobe does with that same tag (see "Colorimetric
white balance" below). So unlike RethinkRAW, this app *can* show an accurate number for As Shot —
just not, it turns out, for Auto, where there's no equivalent real anchor to invert.

Also rendered `WhiteBalance=Auto` directly against `WhiteBalance=As Shot` (both through real Adobe
DNG Converter, bypassing the app) to check the user's specific observation: mean|diff|≈16.8, max
253 — a real, non-trivial difference, but well under half the gray-world guess's own ~21.8 error,
and small enough to read as "similar" at a glance, consistent with what the user described seeing
in RethinkRAW (same renderer, same tags, so the same relationship necessarily holds regardless of
which app's UI triggered the conversion).

**Fix**: `_on_wb_mode_changed()`'s `Auto` branch no longer calls `_compute_auto_wb()` — it now
shares the exact same branch as `As Shot`/`Camera Matching…`, showing the camera's own As-Shot
reading (`self.camera_wb` through `_multipliers_to_temp_tint()`) while in Auto mode too.
`_compute_auto_wb()` and `compute_gray_world_neutral()` were deleted entirely (confirmed unused
anywhere else — `self._auto_wb` was write-only, read by nothing, and is gone too) rather than left
as dead code. This isn't a claim that Auto now shows what Adobe's Auto algorithm actually computes
— there is no way to know that without reverse-engineering Adobe's undisclosed logic, out of scope
here, same as concluded above for the named-preset investigation — it's a deliberate move from "a
fabricated, independently-wrong number" to "a real, accurate, non-fabricated number that happens to
be a reasonably close proxy," matching RethinkRAW's own philosophy of not inventing precision this
app doesn't actually have. This part of the fix only changed what the disabled slider *displays* —
**Verified**: through a real `DNGForge` instance, switching to As Shot then Auto now shows the
identical 5880K/8 reading in both modes (previously 5142K/8 for Auto).

**This display fix turned out not to be the whole story — see the next section for the real
rendering bug it was masking**, caught only because the user pushed back on an unverified claim
made in this same investigation (below) that `WhiteBalance=Auto` rendering was "completely
unaffected either way" by anything display-related. That claim was wrong, and finding out why is
its own worthwhile lesson in not trusting an assumption just because it sounds architecturally
tidy.

### White balance: Auto/As Shot/Camera Matching never cleared a stale ColorTemperature/Tint —
the actual rendering bug behind "Auto produces a very warm image"

**The display fix above was real and worth keeping, but it did not fix the user's original,
concrete complaint.** After it shipped, the user restated the problem plainly: "in rethinkraw auto
differisce poco da as shot, mentre auto in dngforge produce una immagine caldissima. e non va
bene." — i.e. this was never just a misleading number on a disabled slider, it was the *rendered
pixels* actually looking wrong, and the previous fix (which only changed what the Temperature
slider displays) could not have addressed that on its own. Worth flagging honestly: the
investigation above asserted "Rendering itself is completely unaffected either way" for the
display-only fix without re-verifying that DNGForge's actual `WhiteBalance=Auto` write path was
still clean — it wasn't, and this section is that missed check, done properly.

**Root cause, confirmed by direct experiment (not inferred from reading code alone):**
`_build_xmp_crs_args()`'s `WhiteBalance` block only ever wrote `-XMP-crs:ColorTemperature`/`Tint`
when the mode was a named preset or `Custom` — for `As Shot`/`Auto`/`Camera Matching…` it wrote
nothing for those two tags at all, neither a value nor a clear. So whatever `ColorTemperature`/
`Tint` happened to already be saved in the file from an *earlier* session — e.g. from back when
the mode was last `Custom` or a preset — stayed there untouched, forever, no matter how many times
the mode was later switched to Auto/As Shot/Camera Matching. `RAW_LEICA_M8_prova.DNG` had exactly
this: a stale `ColorTemperature=13200`/`Tint=9` left over from the `Slider.set_value()` bug
investigation earlier in this file's history (13200 being the Temperature slider's own maximum).

Confirmed the leak is real with a clean, isolated experiment: rendered `WhiteBalance=Auto` through
real Adobe DNG Converter twice on the same source file — once with the file's actual stale
`ColorTemperature=13200`/`Tint=9` left in place (DNGForge's real behavior before this fix), once
with those two tags explicitly cleared (`-XMP-crs:ColorTemperature=`, `-XMP-crs:Tint=`) — and got
mean|diff|≈18.2, max 254 out of 255. So Adobe DNG Converter's `WhiteBalance=Auto` resolution is
**not** fully independent of a simultaneously-present `ColorTemperature`/`Tint` tag, unlike what
the named-preset investigation above found for `WhiteBalance=Daylight` (there, a conflicting
`ColorTemperature` was confirmed fully ignored) — Auto behaves differently from the named presets
in this specific respect, an important asymmetry this investigation hadn't previously tested for.
13200K is deep into the Temperature slider's own "compensate for a very cool/blue light source"
end, which in Lightroom's convention pushes the rendered image *warmer* — precisely the "Auto
produces an image that's evidently too warm" symptom reported from the very first message in this
whole investigation, now root-caused rather than just observed.

RethinkRAW never has this problem because its `editXMP()` (`xmp.go`) always unconditionally writes
`-XMP-crs:ColorTemperature=`, `-XMP-crs:Tint=` (i.e. clears them) for *every* `WhiteBalance` value
other than `Custom` — confirmed directly from source, not assumed. This is the exact same "always
write the off-state too" convention already applied elsewhere in `_build_xmp_crs_args()` (`HasCrop`,
`ConvertToGrayscale`, the radial/gradient filters' empty-list case) — it had simply never been
applied to `ColorTemperature`/`Tint` for the three non-numeric white-balance modes specifically.

**Fix**: `_build_xmp_crs_args()` now has an explicit `else` branch that clears
`-XMP-crs:ColorTemperature`/`Tint` whenever the mode is not a named preset or `Custom` (i.e. for
`As Shot`/`Auto`/`Camera Matching…`), matching RethinkRAW's own always-clear convention exactly.
**Verified**: through a real `DNGForge` instance on a disposable copy of
`RAW_LEICA_M8_prova.DNG` (which still carries the stale 13200K/9 on disk), `_build_xmp_crs_args()`
now emits `-XMP-crs:ColorTemperature=`/`-XMP-crs:Tint=` (the clearing form) for all three of As
Shot/Auto/Camera Matching…, while named presets are confirmed unaffected (still writing their real
numeric value, e.g. `ColorTemperature=5500` for Daylight); a real end-to-end `save_to_dng()` with
Auto selected, run against that same stale-tag file, confirmed the saved file's `ColorTemperature`
tag is now genuinely gone from disk (previously it would have silently survived every save).
**User-confirmed in real use, not just scripted**: re-tested `RAW_LEICA_M8_prova.DNG` directly —
"tutto funziona alla perfezione, lightroom compliant." Closing note from the user worth keeping
verbatim as the intended framing of this whole investigation: no need to reverse-engineer what
Adobe's own real "Auto" algorithm does internally — Adobe DNG Converter (a free tool from Adobe)
already does that work correctly by itself; this app's only job is to stop feeding it stale,
conflicting tags that make it look wrong.

**Process note, worth remembering:** this bug was found only because a real, compiled reference
instance (`RethinkRAW.exe`, run locally with its HTTP server queried directly — same
gold-standard verification method as the `PROFILE_LOOK_SETTINGS` investigation, see project memory
`dngforge-rethinkraw-research`) produced a measurably different result (Auto vs. As Shot,
mean|diff|≈2.1) than a naive bypass-the-app render using DNGForge's own tag set on the same file
(mean|diff|≈16.8) — a gap that theorizing about `-lossy` flags and JS UI behavior alone did not
explain (both were tested and ruled out directly), and that only got explained once the file's own
stale saved state was checked. The lesson isn't "always launch the live reference app" — it's that
a rendered-pixel discrepancy between two things that are supposed to produce identical output is
worth chasing to an actual root cause via direct A/B experiment, not resolved by the more
architecturally elegant-sounding explanation.

### Colorimetric white balance (`DNGCameraProfile`)

Temperature/Tint are **not** a generic Kelvin→RGB heuristic — they use the specific DNG's own
embedded `ColorMatrix1/2` + `CalibrationIlluminant1/2` (+ `CameraCalibration1/2` +
`AnalogBalance`) tags, read via exiftool in `load_dng_camera_profile()` and interpolated exactly
as Adobe's own tools do, so a given Temperature/Tint value means the same thing here as it does
in Lightroom/ACR for that file. This matters: the user explicitly rejected a generic
approximation ("io mi aspetto che se uso 6000K l'aspetto sia identico a quello che vedrei in
Lightroom applicando 6000K, altrimenti serve a poco") — a camera-agnostic formula fundamentally
can't deliver that, only the file's own calibration data can.

`DNGCameraProfile` (and its helpers `_xy_to_temperature_tint`/`_temperature_tint_to_xy`/
`_xyz_to_xy`, the `_TEMP_TABLE` blackbody-locus constant, `ILLUMINANT_TEMPERATURES`) is a direct
port of Adobe's DNG SDK `dng_temperature`/`dng_color_spec`, via RethinkRAW's Go port
(`pkg/dng/temp.go`, `profile.go`, `lightsrc.go` — see project memory
`dngforge-rethinkraw-research`). Validated by round-tripping: converting the test DNG's real
`AsShotNeutral` to (Temperature, Tint) and back reproduces `rawpy`'s own `camera_whitebalance`
multipliers exactly (2.105/1.0/1.255, matching to 3+ decimals) — see
`dng_colorspec_test.py` in scratch history if this needs re-verifying after a change.

**`kelvin_to_multipliers()` (Tanner Helland approximation) is a display-only fallback** — used by
`multipliers_to_kelvin_tint()`'s reverse search (see "White Balance" above) only when
`self.camera_profile` is `None`, i.e. exiftool isn't available or the file lacks `ColorMatrix1`
(non-DNG raw, or an unusual profile). It has no bearing on rendering at all — Adobe DNG Converter
does its own colorimetric WB regardless of whether this app's own display estimate is exact.
`load_dng()` sets `self.camera_profile` once per file via `load_dng_camera_profile(self.exiftool_path,
path)`; the status bar reports which path is active ("colorimetria DNG attiva" vs "colorimetria
approssimata") so the *display* is never silently wrong, even though it no longer affects what
gets rendered.
- **Tone**: `tone_mode` combobox — **Auto** writes `-XMP-crs:AutoTone=True` and disables the six
  sliders, letting Adobe DNG Converter compute its own auto-tone (this app no longer estimates
  anything itself for Auto — see below); **Default** resets them to 0; **Custom** is entered
  automatically when a slider is nudged away from Default (`_on_tone_slider_changed()`). Every
  slider just writes its own `-XMP-crs:*` tag (`Exposure2012`/`Contrast2012`/`Highlights2012`/
  `Shadows2012`/`Whites2012`/`Blacks2012`) — Adobe DNG Converter does the actual rendering.

**History — this used to be the least successful part of the app**, worth knowing about even
though the code is gone: an earlier Python/LAB-channel approximation of Contrast/Highlights/
Shadows/Whites/Blacks was built, measured against a real Lightroom-rendered ground truth
(`DSC_6751.dng`), fixed twice (a backwards Whites/Blacks sign, then a channel-independent-RGB
desaturation bug), and even after both fixes still measured meaningfully flatter/less contrasty
than Lightroom's own render — a gap the user judged visually unacceptable and that several further
tuning attempts (mask shape changes, blur-based local averaging, vibrance compensation) couldn't
close. That whole line of investigation is now moot: since Adobe DNG Converter renders these tags
directly, there is no longer an approximation to calibrate — the exact numbers and dead ends tried
are not reproduced here since they described code that no longer exists (see git/session history
if ever relevant again). Exposure's own former linear-light fix (`apply_exposure_and_gamma()`,
also gone) is moot the same way — a physical stop adjustment (`2**EV`) needs to happen on
scene-linear data, and Adobe's own engine already does that correctly.
- **Curve**: `curve_combo` — Linear/Medium Contrast/Strong Contrast write
  `-XMP-crs:ToneCurveName2012` directly (Adobe DNG Converter knows what those preset shapes mean),
  plus their own point list (`CURVE_PRESETS`) as `-XMP-crs:ToneCurvePV2012` — the name alone isn't
  enough, see "CONFIRMED BUGS, fixed: Curve and Profile" above; **Custom** shows a real interactive
  editor (`CurveEditor`, see below) whose points are written as `-XMP-crs:ToneCurvePV2012` the
  same way — the user flagged the original placeholder ("clicco su Custom e non posso fare di
  fatto nulla") and it's now a genuine feature, not a documented gap.
- **Presence**: Texture/Clarity/Dehaze/Vibrance/Saturation each just write their own
  `-XMP-crs:*` tag (`Texture`/`Clarity2012`/`Dehaze`/`Vibrance`/`Saturation`) — Adobe DNG Converter
  does the actual rendering, so these are real Adobe algorithms now, not Python approximations.
- **Parametric Curve** (roadmap item 19): the Tone Curve panel's other tab (alongside the
  point-based `Curve` above) — Highlights/Lights/Darks/Shadows region sliders (real Lightroom's
  own bright-to-dark order, not the roadmap's original Shadows-first listing) plus 3 split-point
  sliders, defaulting to 25/50/75 (not 0 — see "Virgin-file defaults match Lightroom, not zero").
  Plain independent sliders, no ordering constraint between the 3 splits — same simplification
  already used for other multi-handle controls in this app.
- **HSL / Color** (roadmap item 20): 24 sliders, `HueAdjustment*`/`SaturationAdjustment*`/
  `LuminanceAdjustment*` × 8 colors (`HSL_COLORS`), grouped into 3 nested group boxes (Hue/
  Saturation/Luminance) standing in for Lightroom's own 3-tab panel. Hides automatically while
  **Black & White** is checked (`self.hsl_group`, toggled in `_on_bw_toggled()`) — matching real
  Lightroom, which shows one panel or the other, never both.
- **Black & White** (roadmap item 16): a standalone checkbox writing `-XMP-crs:ConvertToGrayscale`
  *independent* of Profile — real Lightroom lets you hit its own B&W treatment button regardless
  of which color profile is active, the same tag the "Adobe Standard B&W"/"Adobe Monochrome"
  profile entries already set (`PROFILE_LOOK_SETTINGS`). Written in `_build_xmp_crs_args()`
  *after* the Profile block so a checked box can override whatever that block decided; unchecked
  leaves the profile's own decision standing. 8-color Gray Mixer (`GrayMixer*`) sliders sit below
  the checkbox, always written regardless of whether B&W is actually on (harmless no-op when off).
- **Split Toning** (roadmap item 17): 5 sliders — Shadow/Highlight Hue (0-360) + Saturation
  (0-100) + Balance (-100 to 100) → `-XMP-crs:SplitToningShadowHue/Saturation`,
  `HighlightHue/Saturation`, `Balance`. Lightroom's classic panel; the same tags back modern
  Lightroom's wheel-based "Color Grading" UI too (confirmed via XMP.pm's own note on these
  fields), so this app's simpler slider form is still real, current Adobe schema, not a legacy
  fallback.
- **Sharpening** + **Noise Reduction** + **Defringe** (Lightroom's real single "Detail" panel,
  split into 3 group boxes here): restructured from an earlier flattened 3-slider "Detail" group
  (Sharpness/Luminance NR/Color NR only) into Lightroom's real field set, at the user's explicit
  request ("dovrebbe essere sharpening (amount - radius - detail - masking) e noise reduction
  (luminance - detail - contrast - color - detail - smoothness)"). **Sharpening**: Amount (0-150,
  `Sharpness`) / Radius (0.5-3.0, `SharpenRadius`) / Detail (`SharpenDetail`) / Masking
  (`SharpenEdgeMasking`). **Noise Reduction**: Luminance/Luminance Detail/Luminance Contrast
  (`LuminanceSmoothing`/`LuminanceNoiseReductionDetail`/`LuminanceNoiseReductionContrast`) +
  Color/Color Detail/Color Smoothness (`ColorNoiseReduction`/`ColorNoiseReductionDetail`/
  `ColorNoiseReductionSmoothness`). **Defringe**: Purple/Green Amount (0-20) + HueLo/HueHi
  (0-100) → `DefringePurpleAmount/HueLo/HueHi`, `DefringeGreenAmount/HueLo/HueHi` — positioned
  right after Noise Reduction, matching Defringe's real position at the bottom of Lightroom's
  Detail panel, not a separate top-level panel as the original roadmap note assumed. Every tag
  name across all three groups confirmed against exiftool's own `XMP.pm` crs table, not guessed.
- **Lens Corrections** (roadmap item 4 — done): two plain checkboxes, **Enable Profile
  Corrections** (`lens_profile_checkbox` → `-XMP-crs:LensProfileEnable`) and **Remove Chromatic
  Aberration** (`lateral_ca_checkbox` → `-XMP-crs:AutoLateralCA`), positioned right after Detail
  and before Effects — Lightroom's own real panel order, matching RethinkRAW's reference layout
  exactly. The XMP side mirrors RethinkRAW's own implementation field-for-field — no lens-ID
  mapping, just these two booleans, always written (both `1`/`0` states, same "always write the
  off-state too" convention as every other toggle in `_build_xmp_crs_args()`), both defaulting to
  **checked** (confirmed against `DSC_6751.dng`'s own untouched values, same "virgin-file
  defaults match Lightroom, not zero" rule as Sharpening/Noise Reduction). `AutoLateralCA` was
  confirmed to render correctly through Adobe DNG Converter's own CLI from the start.
  `LensProfileEnable`'s own geometric/vignetting correction, however, turned out to be a
  confirmed no-op in that CLI (isolated-tested, cross-checked against RethinkRAW's own live
  server — full investigation in "Lens Corrections — `LensProfileEnable` is a confirmed no-op in
  Adobe DNG Converter's CLI" below) — so **this app now applies that correction itself, locally,
  via `lensfunpy`** after Adobe's own render, exactly when the checkbox is checked and a database
  match is found (see "Lens Corrections: local `lensfunpy`-based rendering" below for that
  implementation). Save/reload and Undo/Redo round-trip both verified working for both
  checkboxes.
- **Grain** (roadmap item 14) and **Post-Crop Vignette** (roadmap item 15): two more group boxes
  in the same flat-tag pattern as Presence/Detail, not part of RethinkRAW's reference panel —
  user-requested additions once shown what other `crs:*` fields exist. Grain: Amount/Size/
  Frequency → `-XMP-crs:GrainAmount`/`GrainSize`/`GrainFrequency`. Vignette: Amount/Midpoint/
  Feather/Roundness sliders plus a Style combo (Highlight Priority/Color Priority/Paint Overlay)
  → `-XMP-crs:PostCropVignetteAmount`/`Midpoint`/`Feather`/`Roundness`/`Style` — `Style` is the
  one field in either group that isn't a plain number, since Adobe's schema encodes it as an
  integer enum (1/2/3), not a string; `VIGNETTE_STYLE_VALUES`/`VIGNETTE_STYLE_NAMES` handle that
  int↔label mapping both ways. Verified with a real Adobe DNG Converter render, bypassing the app
  (same method as "CONFIRMED BUGS, fixed: Curve and Profile" below): grain and vignette both
  produce a real, measurable pixel difference against a no-tags baseline, not just a tag that
  writes and reads back but does nothing.
- **Radial Filter** (not part of RethinkRAW's reference panel — this project's own roadmap phase
  3): **Place Radial Filter** button arms click-to-place (same pattern as the WB picker); clicking
  the image creates one ellipse (`self.radial_filter` dict) at that point, default 25% size,
  50% feather. Size X/Y, Rotation, Feather sliders plus Exposure/Contrast/Saturation (applied
  *inside* the ellipse, or *outside* if **Invert** is checked) become visible once placed; a
  dashed yellow ellipse overlay (`ImageLabel.overlay_ellipse`, painted in `ImageLabel.paintEvent`,
  purely visual — never part of the actual image data) shows where it is. See below for details
  and scope limits.

### Lens Corrections — `LensProfileEnable` is a confirmed no-op in Adobe DNG Converter's CLI

**The Lens Corrections checkbox shipped with a wrong verification.** The first pass toggled
`LensProfileEnable` and `AutoLateralCA` *together* (both on vs. both off) and measured a real
pixel difference on `RAW_NIKON_D90.dng` (mean|diff|≈1.7, max 117), reported as "lens correction
confirmed working." **Reported by the user right after trying it on `DSC_6751_prova.dng`**: no
visible distortion difference toggling the checkbox at all — and a sharp, correct objection to the
first explanation offered ("same limitation as local adjustments"): lens-profile correction is a
*global* whole-frame operation, not a masked/composited local one, so that analogy didn't actually
hold architecturally. The user also flagged a real inconsistency: in RethinkRAW they *do* see the
image visibly undistort, but only once, on first load — never again on later toggles. Both
objections were right, and chasing them down replaced a wrong verification with a real, isolated,
cross-confirmed one.

**Root cause of the wrong first verification**: never isolated which of the two flags produced the
measured difference. Re-tested each flag alone (the other held at `0` in both renders, both real
Adobe DNG Converter runs, bypassing the app):
- `LensProfileEnable` alone: **mean|diff| = 0.0, max = 0.0 — byte-identical** — on both
  `DSC_6751_prova.dng` *and* `RAW_NIKON_D90.dng`. Zero effect, not a subtle one.
- `AutoLateralCA` alone: mean|diff|≈1.6–1.8, max 102–121 on the same two files — the entire
  difference the first verification measured. This flag genuinely works; it was never the one in
  question.

**Ruled out, each with a direct experiment, before concluding this is a real CLI limitation and
not a missing-flag or missing-data problem:**
1. **A missing/ambiguous lens match** — ruled out decisively: `DSC_6751_prova.dng` already carries
   a *complete, real, Lightroom-resolved* lens profile reference (confirmed via
   `exiftool -struct -XMP-crs:LensProfile*`): `LensProfileName="Adobe (SIGMA 17-50mm F2.8 EX DC OS
   HSM, NIKON CORPORATION)"`, `LensProfileFilename="NIKON CORPORATION (SIGMA 17-50mm F2.8 EX DC OS
   HSM) - RAW.lcp"`, a matching `LensProfileDigest`, and all three Scale fields at `100`. Real
   Lightroom (`Software: Adobe Photoshop Lightroom 6.8`, per the file's own embedded-preview tags)
   had already solved the "generic Nikon-tagged third-party lens ID" problem the original roadmap
   note worried about — this was never a lens-ID-matching gap. Toggling `LensProfileEnable` with
   this exact, complete, digest-matched struct already present still produced 0.0/0.0.
2. **The wrong output mode** — `Adobe DNG Converter.exe -?`'s own official docs
   (`reference/RethinkRAW/pkg/dngconv/doc.pdf`, read in full — confirmed exactly 4 pages via the
   PDF's own `/Count` entry and a second, independent `pdftotext` extraction, nothing missed)
   list three mutually exclusive output modes: `-c` (compressed DNG, mosaic preserved — this
   app's own default, always used), `-u` (uncompressed, mosaic preserved), and `-l` ("linear DNG",
   i.e. already demosaiced). A plausible theory: geometric lens-distortion correction needs full
   per-pixel RGB to warp, which a still-mosaiced Bayer pattern can't cleanly provide — so maybe
   `-l` was the missing ingredient. Tested directly: `-l -p2 -side 1800` with `LensProfileEnable=1`
   vs. `=0` still came back 0.0/0.0, byte-identical. Not the missing ingredient either.
3. **A missing `-lossy` flag** — RethinkRAW's own re-render step (`dng.go`'s `runDNGConverter()`,
   `side > 0` branch) always includes `-lossy`, unlike this app's own re-render step. Queried
   RethinkRAW's actual **live, compiled server** directly (`RethinkRAW.exe`, launched against this
   exact file, its HTTP `?preview` endpoint hit with `curl` — same gold-standard live-reference
   method already used for the `PROFILE_LOOK_SETTINGS` and Auto-WB investigations, see project
   memory `dngforge-rethinkraw-research`) with `lensProfile=true` vs. `lensProfile=false`: **also
   0.0/0.0, byte-identical** — RethinkRAW's own real rendering pipeline, `-lossy` included, shows
   the identical no-op.
4. **A misread of what `-p2` does** — worth correcting explicitly, since earlier write-ups in this
   file (and the original verification's own reasoning) implicitly treated `-p2` as "apply the
   PV2012 develop recipe." Adobe's own docs say otherwise: `-p2` only sets the embedded **JPEG
   preview size** to full ("Set JPEG preview size to full size"), one of `-p0`/`-p1`/`-p2`. There
   is **no CLI flag anywhere in Adobe's documented option list for "apply the XMP develop
   settings"** — confirmed by reading the complete, 4-page official flag reference, not just the
   flags this project happened to already use. The develop-settings application this project has
   relied on all along (exposure, WB, tone curve, profile Looks, grain, vignette, Defringe,
   `AutoLateralCA`, all independently confirmed elsewhere in this file) is *implicit*: Adobe DNG
   Converter reads and applies whatever `-XMP-crs:*` tags are already present in the *source* file
   it's handed, regardless of which output-mode/preview-size flags accompany the call. This
   implicit mechanism evidently just doesn't extend to `LensProfileEnable`'s own geometric/
   vignetting/CA-scale correction, specifically and only for that one field.

**This fully resolves the user's "non mi torna" objection about RethinkRAW's first-load
deformation — and the real explanation has nothing to do with the checkbox toggle mechanism at
all.** RethinkRAW has a separate, faster preview path (`jpeg.go`'s `previewJPEG()`, logged as
`"dcraw (get thumb)..."`) that extracts whatever preview JPEG is **already embedded in the source
file** via `dcraw` — entirely bypassing Adobe DNG Converter — used by its gallery/thumbnail
endpoint (`http_thumb.go`) for an instant first look before the slower real conversion finishes.
`DSC_6751_prova.dng`'s own embedded preview was placed there by genuine, full desktop Lightroom
(`PreviewApplicationName: Adobe Photoshop Lightroom`, `Software: Adobe Photoshop Lightroom 6.8
(Windows)`, confirmed via exiftool) — where lens correction runs for real, in Camera Raw's full
interactive engine. So the very first image ever shown is Lightroom's own **real, correctly
undistorted** photo, extracted directly, no Adobe DNG Converter involved. Once Adobe DNG
Converter's own CLI render lands moments later — confirmed above to never apply
`LensProfileEnable` under any tested flag combination — it **replaces** that correct preview with
an uncorrected one. What looked like "the image deforming" was the opposite of what it appeared to
be: not correction switching on, but a correct (Lightroom-original) image being swapped out for an
uncorrected (Adobe-DNG-Converter-CLI) one, exactly once, at load. Every toggle afterward goes
through the same CLI path that never applies it, hence "flag/deflag successivi non fanno niente" —
fully consistent, no contradiction once the actual mechanism is understood instead of assumed.

**Where this left the feature at the time**: `AutoLateralCA` was confirmed real and shipped
correctly through Adobe DNG Converter. `LensProfileEnable`'s own geometric/vignetting correction
was a confirmed, real limitation of the standalone CLI — not a bug in this app's tag-writing, not
a lens-ID-matching gap, not a missing flag among any of the ones Adobe documents. See the next
section for the fix that was actually built afterward, once `lensfunpy` turned out to be
practical after all — the tag itself is still written unconditionally either way, real, valid XMP
a genuine Lightroom/ACR would also honor.

### Lens Corrections: local `lensfunpy`-based rendering (`compute_lensfun_correction()`, `_apply_lensfun_correction()`)

**`lensfunpy` was reintroduced after all** — not because the earlier "just flip Adobe's flag"
premise became viable, but because the two blockers that made a from-scratch geometric-correction
pipeline look expensive both turned out not to apply: (1) lensfun's own public database, checked
directly against the real Sigma 17-50mm f/2.8 EX DC OS HSM file `DSC_6751_prova.dng` is built
around, has a complete calibration entry for it — distortion (`ptlens` model, 5 focal lengths
spanning the full 17–50mm range) *and* vignetting (`pa` model, 5 focal lengths × 5 apertures × 2
distances) *and* TCA, not just the sparse distortion-only coverage lensfun's own docs warn is
typical; and (2) the "generic Nikon-tagged third-party lens ID" problem the original roadmap item
4 worried about (and that blocked a naive `EXIF:LensModel`-based lensfunpy lookup — confirmed
directly, zero results for `"17.0-50.0 mm f/2.8"` even with an explicit `lens_maker="Sigma"`
guess) turns out to already be solved, for free, by **`Composite:LensID`** — exiftool's own
lens-ID resolution database, already used and documented in `read_metadata()`'s own "Lens" field
(confirmed there against this exact file, this exact lens, back when the Metadata panel was
built) — correctly resolving to `"Sigma 17-50mm F2.8 EX DC OS HSM"` from the same generic EXIF
tag lensfunpy's own search couldn't use. No new lens-ID mapping logic was needed; this app already
had the answer sitting in a different feature.

**Architecture**: mirrors `LocalAdjustmentRenderThread`'s own "Adobe renders it, this app
post-processes the pixels" pattern — Adobe DNG Converter still renders every pixel first
(unchanged), then `_apply_lensfun_correction()` decodes the resulting JPEG, applies
`lensfunpy.Modifier`'s distortion + vignetting correction (`ModifyFlags.DISTORTION |
ModifyFlags.GEOMETRY | ModifyFlags.VIGNETTING` — **deliberately not `TCA`**, since
`AutoLateralCA` already renders correctly through Adobe's own CLI and requesting both would
double-correct fringing), and re-saves over the same JPEG path. Applied as the very last step
after any local-adjustment compositing (`_render_local_adjustments_composite()`'s own final
`Image.fromarray(...).save()`) rather than per-region, since geometric distortion is a
whole-frame warp — correcting each region separately before blending would desync the alpha
masks (defined in the *pre-warp* rendered frame's own pixel coordinates) from the corrected
geometry.

**Lens matching** (`compute_lensfun_correction()`) reuses `self.metadata_values` —
`_update_metadata_panel()`'s own already-fetched dict, populated at `load_dng()` time for the
Metadata panel — rather than a fresh exiftool call, keeping this file's own "one combined read
per file load" discipline intact. Needs `metadata_values["Make"]`/`["Model"]` (camera lookup) and
`metadata_values["Lens"]` (the `Composite:LensID`-resolved string, used as the lens search hint —
**not** `EXIF:LensModel` directly, confirmed via `lensfunpy.Database.find_lenses()` returning zero
results for the raw generic string) plus `["Focal Length"]`/`["Aperture"]` (parsed from their
print-converted `"17.0 mm"`/`"f/8.0"` display strings — the exact strings `read_metadata()`
already produces). **Note this reuse requires non-numeric mode**: `read_combined_edit_tags()`'s
own shared exiftool call always runs with `-n` (numeric), under which `Composite:LensID` returns
the *raw, unresolved* MakerNotes byte sequence instead of the resolved lens name (confirmed
directly: `"8F 48 2B 50 24 24 4B 0E"` vs. the resolved `"Sigma 17-50mm F2.8 EX DC OS HSM"`) — so
this genuinely needed `read_metadata()`'s own separate, already-existing, non-numeric call; it
could not be folded into the shared `-n` one the way most other per-load reads are.

**Database**: `lensfunpy`'s PyPI wheel bundles the lensfun XML database directly (confirmed: a
plain `pip install lensfunpy` on this dev machine, with no separate lensfun install, correctly
resolved both the Sigma 17-50mm on a Nikon D7200 and, in a regression check, the Nikon D90's own
kit 50mm f/1.4D lens) — no extra download or setup step needed beyond the one new dependency.
`_get_lensfun_db()` loads and caches the `lensfunpy.Database()` once per process (parsing the XML
is the only non-trivial cost, tens of ms) rather than once per file.

**Threading**: `DngConverterRenderThread`/`LocalAdjustmentRenderThread` both gained a
`lens_correction` constructor parameter — plain data (`self._lens_correction`, or `None` when the
checkbox is unchecked) resolved on the **main thread** before the thread starts, same "Qt widget
reads must happen before handoff" rule `xmp_args` already followed — the background thread itself
only ever touches the already-resolved `Camera`/`Lens` objects and plain floats, never a live Qt
widget. Wired into all four render call sites that produce a final JPEG: the main live-preview
pipeline (`_start_dngconv_render()`), the zoom-detail pipeline (`_start_zoom_detail_render()`),
`save_to_dng()`, and `export_preview_jpeg()` — each computes
`lens_correction = self._lens_correction if self.lens_profile_checkbox.isChecked() else None` and
passes it through; a `None` correction is a pure no-op in `_apply_lensfun_correction()`, so the
common case (checkbox unchecked, or no database match) costs nothing extra.

**Verified end-to-end**, all real Adobe DNG Converter + lensfunpy runs, not mocked, against
`DSC_6751_prova.dng`: a standalone proof-of-concept (`Modifier` applied directly to a real Adobe
render) showed a real, correctly-shaped geometric change — mean|diff|≈17.9 overall, but
noticeably stronger at the frame edges (≈23-25) than the center (≈5.5-9.5), exactly the expected
signature of a lens-distortion+vignetting correction, not a uniform tone/color shift (visually
confirmed too: the wide-17mm test photo's curved vertical elements — door frames, curtain edges —
visibly straighten). Then the full, real, integrated app pipeline: (1) `load_dng()` correctly
finds the Sigma+D7200 match; (2) the **async live-preview thread** (`DngConverterRenderThread`,
waited on via a real Qt event loop, not skipped) produces a visibly different rendered pixmap when
the checkbox is toggled off vs. on (mean|diff|≈17.5); (3) `save_to_dng()` end-to-end — real file,
real save, real reload of the embedded preview — shows the same real difference with the same
edge-vs-center signature (edge≈24.9, center≈5.6); (4) `export_preview_jpeg()` completes and writes
a real, non-trivial JPEG with correction applied. A regression check on `RAW_NIKON_D90.dng` (a
different camera/lens pair, its own kit 50mm f/1.4D) confirmed the database lookup succeeds there
too and `save_to_dng()` still completes without error; and with `HAVE_LENSFUNPY` forced `False`,
`compute_lensfun_correction()` cleanly returns `None` rather than raising, confirming the
graceful-degradation path for a machine without `lensfunpy` installed.

**Match-status label, added later**: the "Enable Profile Corrections" checkbox originally gave no
indication at all of whether the currently loaded file's camera/lens combination was actually
found in lensfunpy's database — `compute_lensfun_correction()` returning `None` on a lookup miss
looked, from the UI, identical to a successful-but-zero-effect correction. Raised by the user
directly ("non c'è modo di sapere se quella particolare combinazione camera/lente viene
riconosciuta e corretta?"). Fixed by splitting the lookup into `lensfun_lookup_with_status()`
(returns `(correction, status_text)`; `compute_lensfun_correction()` is now a thin wrapper
returning just the first element, kept for callers that don't need the text) and a small
`lens_match_label` under the two checkboxes, updated in `load_dng()` right after the lookup runs —
green text naming the matched camera+lens on success ("riconosciuto: Nikon D90 + Nikon AF Nikkor
50mm f/1.4D"), gray text with the specific reason on a miss (camera not in database, lens not in
database, lensfunpy not installed, required EXIF field missing/unparsable). **Verified** end-to-end
against two real files: `RAW_NIKON_D90.dng` shows the green "riconosciuto" text with the correct
camera/lens names; `RAW_LEICA_M8.DNG` (whose `Aperture`/`Focal Length` metadata don't parse
cleanly, see "Primary test file" below) shows the correct specific miss reason instead of a blank
or generic label.

### Radial filter (placement and UI — see "Radial filter persistence" below for the XMP schema)

Placement/size are stored as **fractions** of image width/height — same resolution-independent
convention as the WB picker's click coordinates — and written to
`-XMP-crs:CircularGradientBasedCorrections` (see "Radial filter persistence" below) for real
Lightroom to read. **Adobe DNG Converter itself ignores this tag when rendering** (see "Local
adjustments are not rendered by Adobe DNG Converter" below) — DNGForge's own preview/save pixels
come from `LocalAdjustmentRenderThread` compositing two Adobe renders per region, not from Adobe
applying the mask directly. (This app used to build the elliptical mask and blend it itself,
`build_radial_mask()`/`apply_radial_filter()`, before the Adobe-only migration; briefly assumed —
wrongly — that Adobe DNG Converter's engine had picked up that job for free once rendering moved
to it. It hadn't; see the section below for the real story and the current fix.)

**Drag handles** (added after initial feedback: "manca un handle al centro per spostarlo ed i
controlli sull'ellisse per ridimensionarla e ruotarla"): `ImageLabel` draws 4 handles directly on
the *selected* ellipse overlay — center (move), right edge (resize `rx`), bottom edge (resize
`ry`), and one offset above the top edge, connected by a line (rotate) — and handles their drag
interaction itself (`_hit_test_handles()`, `mousePressEvent`/`mouseMoveEvent`/`mouseReleaseEvent`),
emitting `ellipseChanged` on every move. `DNGForge._on_ellipse_dragged()` keeps
`radial_filters[radial_selected]` and the Size X/Y/Rotation sliders in sync with whichever one the
user just touched — dragging a handle updates the sliders, and moving a slider updates the handle
positions on next repaint, same bidirectional pattern used nowhere else in the app but worth
reusing if a future feature needs on-image dragging again (e.g. a spot-removal tool, roadmap
item 7). Resize/rotate handle math works in the ellipse's **local** (unrotated) coordinate frame —
dragging the `rx` handle projects the mouse's screen position onto the ellipse's rotated x-axis
via inverse rotation (`local_x = dx*cos(θ) + dy*sin(θ)`) before converting to a radius, so resizing
behaves correctly even when the ellipse is rotated, not just at rotation=0. Verified with a
scripted press/move/release simulation for all three drag modes (move, resize, rotate to exactly
90°) before it ever got tested by hand, all matching the expected geometry.

**Multi-region support** (added after the user tried it and said "se torno in radial filter mi
aspetto di poter vedere le varie ellissi piazzate in precedenza" — right after the single-region
version shipped): `self.radial_filters` is a **list** of filter dicts, `self.radial_selected` an
index into it. **Place Radial Filter** always *appends* a new region rather than replacing —
placing selects the new one. `ImageLabel.overlay_ellipses` (list) + `.selected_index` mirror this:
every placed ellipse is drawn (unselected ones as a thin dim outline, no handles), and clicking
directly on any ellipse's *body* (not just handles — `_hit_test_ellipse_body()`, checked in
`mousePressEvent` after the handle-hit-test and before falling through to plain click-to-place)
emits `ellipseSelected(index)` → `DNGForge._on_ellipse_selected()` → `_select_radial_filter()`,
which loads that region's own values into the sliders (blocked signals) and gives it the handles.
**Remove** deletes only the selected region — others are untouched. `build_radial_xmp_value()`
serializes the whole list, in placement order, into one `-XMP-crs:CircularGradientBasedCorrections`
tag (see "Radial filter persistence" below) — real Lightroom reads every region from that one tag
(**Adobe DNG Converter itself does not, see "Local adjustments are not rendered by Adobe DNG
Converter" below** — DNGForge's own preview/save pixels for this tag come from
`LocalAdjustmentRenderThread`'s own compositing, not from Adobe applying the tag directly).
Verified: placing two regions, editing one (exposure=1.0)
leaves the other's exposure at 0.0 untouched; selecting the first again correctly shows *its own*
0.0 in the slider, not the second region's value; removing the selected one leaves the other
intact; the panel-wide Reset clears the whole list.

**Scope limits, deliberate:**
- Only Exposure/Contrast/Saturation are adjustable inside each region, not the full slider set —
  a deliberately focused subset rather than reproducing every global control locally.
- **Rendering — see "Local adjustments are not rendered by Adobe DNG Converter" below.** This
  section previously claimed the move to Adobe-only rendering resolved the old local-renderer's
  gamma-space-exposure approximation "for free" — that claim was **wrong**, caught only much
  later when the user reported the radial/graduated/spot tools "don't do anything" and a rendered
  pixel-diff investigation found Adobe DNG Converter silently ignores this tag entirely, at any
  ProcessVersion, regardless of the correction's magnitude. The current fix
  (`LocalAdjustmentRenderThread`) is a different, new approximation — not a solved problem, see
  that section for exactly what is and isn't exact about it.
- **Now written to XMP on save** — originally not at all (confirmed to the user directly when
  asked: "queste ellissi verrebbero riconosciute in lightroom?" / "questo programma dove salva le
  ellissi?"), a gap the user later asked to close explicitly, partly because the same persistence
  problem would recur for every future local-adjustment-style tool (crop, straighten, spot
  removal). See "Radial filter persistence" below for the real Adobe schema this now writes/reads,
  as opposed to a simplified stand-in.

### Graduated filter (placement and UI — see "Graduated filter persistence" below for the XMP schema)

Built right after the radial filter's own XMP persistence, at the user's explicit request ("ora
passiamo al filtro graduato, stessa logica del filtro radiale") — deliberately mirrors that
feature's architecture field-for-field rather than inventing a different pattern: **Place
Graduated Filter** / **Remove** buttons, a **Show handles / edit** checkbox, multi-region list
(`self.gradient_filters` + `self.gradient_selected`, same shape as `radial_filters`/
`radial_selected`), Exposure/Contrast/Saturation sliders only (same deliberate scope limit as the
radial filter — see its own "Scope limits" list, identical reasoning applies here), and drag
handles drawn directly on the image (`ImageLabel.overlay_lines`, alongside — not replacing —
`overlay_ellipses`; both kinds of region coexist and render simultaneously, confirmed by a
rendered screenshot showing a selected gradient line and an unselected radial ellipse at once).

**Geometry is simpler than the radial filter's, on purpose, because the real thing is simpler**:
a linear gradient only needs two points — `zero_x/zero_y` (0% effect) and `full_x/full_y` (100%
effect) — with direction and feather both implicit in the vector between them. No Size X/Y,
Rotation, or Feather sliders exist for this filter; the only way to adjust its geometry is
dragging the two endpoint handles (or dragging the connecting spine to move both at once,
`ImageLabel._move_line()`'s `"move"` drag mode) directly on the image, the same way real
Lightroom's own graduated filter works. This two-point model isn't a simplification invented for
this project — it's confirmed to be Adobe's actual real representation (see "Graduated filter
persistence" below).

**`Invert`** flips which side of the gradient is affected — same UI convenience as the radial
filter's own Invert checkbox — but note this is purely a DNGForge-side UI convenience, not
something written to the file as a separate flag; `build_gradient_xmp_value()` reconciles it by
swapping the Zero/Full points on write instead, see "Graduated filter persistence" below for why
and how.

Verified with a scripted press/move/release-equivalent test (setting `overlay_lines` state
directly and calling the same drag-handler methods `mouseMoveEvent` would) for both endpoint drags
and the whole-line move; and with an actual rendered screenshot (`ImageLabel.grab()`, saved to PNG
and visually inspected) confirming the cyan dashed spine + perpendicular boundary ticks + handle
dots render correctly and don't visually conflict with the radial filter's yellow ellipse overlay
on the same widget.

### Custom tone curve editor (`CurveEditor`)

A `QWidget` subclass, hand-painted (`QPainter`) rather than any charting library: a diagonal
identity reference line, the actual curve as straight segments between 5 fixed x control points
(0/64/128/192/255), and draggable red handles at each (only y moves, x stays fixed — same
simplicity tradeoff as `CURVE_PRESETS`, kept deliberately simple rather than adding a spline
dependency, since the actual curve rendering is Adobe DNG Converter's job, not this widget's).
Only shown when Curve mode is Custom (`_on_curve_mode_changed()` toggles `curve_editor`/
`curve_reset_btn` visibility); dragging emits `curveChanged` → the normal debounced
`_schedule_update()`, same as every slider. Points persist across mode switches — flipping to a
preset and back to Custom doesn't lose an edit in progress; only **Reset Curve** or the panel-wide
**Reset** clears it back to identity. `_build_xmp_crs_args()` reads `curve_editor.get_points()`
directly when Custom is active. Saving with Custom active writes both `ToneCurveName2012=Custom`
and the real point list as `ToneCurvePV2012=x, y; x, y; …` (same "x, y; …" format RethinkRAW
itself writes), not just the name.

### Known simplification in the fallback path (tracked, not a bug)

`kelvin_to_multipliers()` (Tanner Helland blackbody approximation), used when `DNGCameraProfile`
isn't available, no longer touches rendering at all — Adobe DNG Converter is the sole renderer and
does its own colorimetric conversion from whatever `-XMP-crs:ColorTemperature`/`Tint` numbers this
app sends it, regardless of whether `self.camera_profile` was available locally. But it still
matters indirectly, in two places: **display** (the disabled Temperature/Tint sliders, via
`multipliers_to_kelvin_tint()`'s reverse search built on this function, see "White Balance" above)
and, more consequentially, the **WB eyedropper** (`_on_image_clicked()` →
`DNGCameraProfile.neutral_to_temperature_tint()` when a real profile exists, or effectively this
approximation's territory when it doesn't) — since whatever Temperature/Tint the eyedropper lands
on gets written straight into the slider and then into XMP, an inaccurate fallback here can still
produce a visibly wrong rendered white balance for cameras that fall back to this path, just
indirectly (through the value the slider ends up holding) rather than through this app's own pixel
math. Its RGB is a *display-gamma* appearance value; the ratios are raised to `WB_GAMMA` (2.2) so
the internal numeric search this function supports stays self-consistent. If retuning this
fallback further, verify against the eyedropper's actual resulting Temperature/Tint reading on a
known-neutral patch, not just by reasoning about the formula.

## Roadmap (incremental, per original project brief)

1. **Apertura DNG + bilanciamento del bianco + curva chiaroscuro (MVP)** — done.
2. **Saturazione, nitidezza** — done.
3. **Filtro radiale — done.** See "Radial filter" below. Multi-region, Exposure/Contrast/
   Saturation only (deliberate scope limit — see that section's scope-limits list), now written
   to and read from real Adobe XMP (see "Radial filter persistence").
4. **Correzione profilo obiettivo — done, via a small local `lensfunpy` rendering step.** See
   "Lens Corrections" and "Lens Corrections: local `lensfunpy`-based rendering" below for the
   full story. UI mirrors RethinkRAW's own implementation (two plain booleans,
   `LensProfileEnable`/`AutoLateralCA`, always written to XMP for real Lightroom/ACR
   compatibility). `AutoLateralCA` was confirmed to render correctly through Adobe DNG
   Converter's own CLI from the start. `LensProfileEnable`'s own geometric/vignetting
   correction was confirmed to be a genuine no-op in that CLI, isolated-tested and
   cross-checked against RethinkRAW's own live server — so this app now applies that
   correction itself, locally, via `lensfunpy` (its bundled public database, not `.lcp`
   files) after Adobe's own render, the same "Adobe renders, this app composites on top"
   pattern already used for local adjustments. The original Nikon/Sigma third-party-lens-ID
   concern turned out to be a non-issue either way — solved for free by exiftool's own
   `Composite:LensID` resolution, already used elsewhere in this app.
5. **Salvataggio: JPEG risultante + parametri di editing (namespace XMP `crs`) incorporati nel
   DNG originale, via ExifTool (`pyexiftool`), nessun sidecar — done** (skipped ahead of 3/4 at
   user request, since this is the project's absolute non-negotiable requirement). See "Save" below.
6. **Filtro graduato — done.** See "Graduated filter" below. Same architecture and scope limits
   as the radial filter (multi-region, Exposure/Contrast/Saturation only, real Adobe XMP
   persistence) — user explicitly asked for "stessa logica del filtro radiale".
7. Rimozione macchie (spot/blemish removal — healing or clone-stamp style tool for dust spots
   etc.; a materially different kind of feature from anything built so far, needs its own design
   pass, likely involving picking a source region and a destination region and some form of
   content-aware or simple patch copy/blend).
8. **Ritaglia (crop) — done**, paired with item 9, see "Crop & Straighten" below.
9. **Raddrizza (straighten) — done.** Built together with Crop as a single tool, as the roadmap
   note here anticipated: a Rotation control that auto-adjusts the crop rectangle to stay within
   the rotated image bounds. See "Crop & Straighten" below.
10. **Sezione metadati — done.** See "Metadata panel" below. Campi richiesti esplicitamente
    dall'utente, in quest'ordine: File Name, Dimensions, If it is cropped, Date taken, Exposure
    time (shutter speed), Focal length, Focal length at 35mm (full-frame equivalent), Brightness
    Value, Exposure Bias (exposure compensation), ISO speed rating, Flash, Exposure Program,
    Metering mode, Make, Model, Lens, Software (firmware version). Uses exiftool (already a
    dependency) to read most of these — rawpy/libraw doesn't expose this range of EXIF fields.
11. **Performance: two-tier render caching + capped preview resolution — done, then superseded.**
    Not part of the original brief, added when the app started feeling sluggish as features
    accumulated; the caching approach itself was later deleted along with the entire local
    render pipeline it existed to speed up (see "Adobe DNG Converter is the sole renderer" above)
    — its own performance concern is now handled by the background Adobe render pipeline's
    generation-counter/single-in-flight queueing instead.
12. **Undo/Redo + Revert to Last Save — done.** See "Undo/Redo" below. User explicitly scoped
    this down from a full history to "a few steps back or a revert to last save" — landed on a
    bounded 20-state stack plus a separate one-shot revert, not an unbounded undo log.
13. **Rimozione macchie — done (funzionalmente), persistenza XMP non ancora fatta.** See "Spot
    Removal" below. Heal (real Poisson blending via `cv2.seamlessClone`) and Clone (feathered
    direct copy), multi-region, same Place/Remove/Show-handles pattern as radial/gradient. Real
    Adobe schema for future persistence (`crs:RetouchAreas`) already identified from exiftool's
    own `XMP.pm` source but unverified against a real Lightroom-saved retouch region — see that
    section's own persistence note for the specific open question (`SourceX`/`OffsetY` naming).
14. **Grana pellicola — done.** `GrainAmount`/`GrainSize`/`GrainFrequency`, 3 sliders in a new
    "Grain" group box (Amount 0-100 default 0, Size 0-100 default 25, Frequency 0-100 default
    50 — matching Lightroom's own defaults for these three, not this app's usual "everything
    defaults to 0" convention, since Size/Frequency are meaningless until Amount is raised above
    0 anyway). Flat tags, always written (including at Amount=0, so a previous
    save's grain is properly cleared on Reset rather than left stranded — same "always write the
    off-state" convention as every other slider group). No new persistence mechanism needed —
    folded straight into `XMP_CRS_TAGS`/`_build_xmp_crs_args()`/`_apply_xmp_crs_state()`/
    `_capture_edit_state()`/`_apply_edit_state()`, the same flat round-trip Presence/Detail
    already use.
15. **Vignettatura post-ritaglio — done.** `PostCropVignetteAmount`/`Midpoint`/`Feather`/
    `Roundness` (4 sliders) + `PostCropVignetteStyle` (combo: Highlight Priority/Color
    Priority/Paint Overlay), new "Post-Crop Vignette" group box, same flat-tag round-trip as
    Grain above. **`PostCropVignetteStyle` is the one field here that isn't a plain string or
    number** — it's an integer XMP enum (1/2/3), so it needed its own explicit
    `VIGNETTE_STYLE_VALUES`/`VIGNETTE_STYLE_NAMES` int↔label map (write sends the raw int, read
    comes back as a raw int too since every read in this app goes through the usual `-n`
    convention — see `read_xmp_crs_state()`) rather than writing/reading the combo text directly
    the way `ToneCurveName2012`/`WhiteBalance` do.

    Both 14 and 15 were verified the way CLAUDE.md's own "Investigation method worth reusing"
    note (see "CONFIRMED BUGS, fixed: Curve and Profile" above) prescribes: not just "the tag
    round-trips through this app," but a real Adobe DNG Converter render, bypassing the app,
    diffing actual output pixels against a baseline with no grain/vignette tags at all — since a
    structurally-valid flat write can still be silently ignored by Adobe's renderer, exactly the
    class of bug that section describes. Grain (Amount=90/Size=80/Frequency=70) produced
    mean|diff|≈12.6 against baseline (real per-pixel noise, mean brightness essentially
    unchanged at 148.79→148.73, as expected for a noise-only effect); Vignette
    (Amount=-100/Midpoint=30) produced mean|diff|≈71.7 with mean brightness dropping from
    148.79→77.10 (a strong, unmistakable darkening toward the frame edges) — both real,
    non-zero, and in the expected direction. Also verified the full in-app round-trip (not just
    a raw exiftool render): set distinctive values on every new slider/combo in a real
    `DNGForge` instance, `save_to_dng()`, reopened in a second independent instance, all 8
    values matched exactly, including `PostCropVignetteStyle`'s int↔"Color Priority" mapping
    both directions.
16. **Bianco e nero — done.** `ConvertToGrayscale` + 8-channel Gray Mixer
    (`GrayMixerRed/Orange/Yellow/Green/Aqua/Blue/Purple/Magenta`). A standalone "Black & White"
    checkbox, independent of Profile — real Lightroom lets you hit its own B&W treatment button
    regardless of which color profile is active, the same `ConvertToGrayscale` tag the
    "Adobe Standard B&W"/"Adobe Monochrome" profile entries already set (see
    `PROFILE_LOOK_SETTINGS`). See "Control panel" above for how the two paths are reconciled on
    write, and "HSL / Color panel" below for why the HSL group hides while this is checked.
    **CONFIRMED BUG, fixed before shipping**: the first version only wrote
    `ConvertToGrayscale=True` when the checkbox was checked, never writing `False` — with
    Profile=Custom (whose own branch never touches this tag either), unchecking Black & White
    after it had been on left the stale `True` in the file, not actually turning color back on.
    Fixed by writing the tag unconditionally as a plain OR of both paths
    (`self.bw_checkbox.isChecked() or profile in ("Adobe Standard B&W", "Adobe Monochrome")`),
    same "always write the off-state too" lesson already documented for `HasCrop`/`WhiteBalance`/
    radial-gradient filters. Verified with 3 real save/reload cases through the actual app: Profile
    = Adobe Standard B&W with the checkbox left off still saves grayscale (profile path alone is
    sufficient, checkbox doesn't need to mirror it); Profile = Custom + checkbox on saves
    grayscale; the same file with the checkbox then turned back off correctly saves color again —
    the exact case the bug broke.
17. **Split Toning — done.** `SplitToningShadowHue/Saturation`, `SplitToningHighlightHue/
    Saturation`, `SplitToningBalance` — 5 sliders, Lightroom's classic Split Toning panel (still
    the real tags even in modern Lightroom's wheel-based "Color Grading" UI, which reuses the
    same fields — confirmed via exiftool's XMP.pm, which literally notes "also used for newer
    ColorGrade settings" on these tags).
18. **Defringe — done.** `DefringePurpleAmount/HueLo/HueHi`, `DefringeGreenAmount/HueLo/HueHi` —
    6 sliders, bottom of Lightroom's real "Detail" panel (not a separate top-level panel — placed
    right after Noise Reduction in this app's own panel, matching that). Amount's real range is
    0-20 (not the usual 0-100), confirmed via XMP.pm; **no edge-aware masking was built** — unlike
    the original roadmap note's expectation, this is a flat XMP tag write like every other control
    here, Adobe DNG Converter does the actual edge-aware fringe removal itself, nothing local to
    build.
19. **Curva parametrica — done.** `ParametricHighlights/Lights/Darks/Shadows` +
    `ParametricShadowSplit/MidtoneSplit/HighlightSplit` — 4 region sliders + 3 split points, no
    new UI widget (plain sliders, as originally scoped — no ordering constraint enforced between
    the 3 split points, same simplification tradeoff as the ellipse/crop handle math elsewhere in
    this app). **Slider order corrected to match Lightroom's real top-to-bottom panel order**
    (Highlights, Lights, Darks, Shadows — bright-to-dark, matching the Basic panel's own
    Highlights→Shadows convention) instead of the roadmap's original Shadows/Darks/Lights/
    Highlights ordering, caught during the Lightroom-structure audit this item and 16/17/18/20
    were all built under (see "Virgin-file defaults match Lightroom, not zero" below for the
    audit's other main finding).
20. **Pannello HSL completo — done.** `HueAdjustment*`/`SaturationAdjustment*`/
    `LuminanceAdjustment*`, 8 colors × 3 = 24 tags. Three nested group boxes (Hue/Saturation/
    Luminance) inside one outer "HSL / Color" group, standing in for Lightroom's own 3-tab
    layout — this app's panel is flat, not tabbed, so three stacked sections show the same 24
    sliders at once instead. Hides automatically while Black & White (item 16) is checked, same
    as real Lightroom showing HSL/Color *or* B&W, never both (Hue/Saturation are no-ops on a
    fully desaturated image).

    **All 5 of the above were built together in one pass**, triggered by the user flagging that
    the *previous* version of the Detail panel (see the "Sharpening + Noise Reduction" rewrite
    above, in the same pass) "non coincide bene con la struttura di lightroom e dei tag xmp," then
    extending that same scrutiny to the rest of the still-open roadmap ("anche il resto della
    roadmap deve riflettere la struttura reale di lightroom"). Every tag name across all 5 items
    was pulled directly from exiftool's own `XMP.pm` crs table (`grep`'d from the installed
    module, not recalled from memory) and cross-checked against a genuine Lightroom-saved file
    (`DSC_6751.dng`) wherever that file had a relevant value — see "Sharpening + Noise Reduction"
    above for the parallel investigation on that group, and "Virgin-file defaults match Lightroom,
    not zero" below for what that cross-check changed about default *values* specifically.
    **Verified** with a real Adobe DNG Converter render for each group, bypassing the app (same
    method as "CONFIRMED BUGS, fixed: Curve and Profile"): Parametric Curve mean|diff|≈6.5,
    HSL≈3.6, B&W+Gray Mixer≈17.9, Split Toning≈14.9, Defringe≈0.8 (small but real and non-zero —
    Defringe is inherently an edge-localized effect, so a small *mean* diff with a much larger
    *max* diff, 109, is the expected signature of "affects only fringed edges," not a sign it did
    nothing). Also verified the full in-app round-trip for all 20 new controls at once (Parametric
    Curve, one color from each HSL tab, Black & White + one Gray Mixer color, Split Toning, both
    Defringe amounts): set distinctive values in a real `DNGForge` instance, `save_to_dng()`,
    reopened in a second independent instance, every value matched exactly, including the HSL
    group correctly re-hiding itself on reload since the reloaded state has Black & White checked.
21. **Rimozione macchie: persistenza XMP — done.** See "Spot Removal" above (persistence
    subsection). `crs:RetouchAreas`, same unverified-against-real-Lightroom caveat as the
    feature's own geometry.
22. **Zoom — done, later extended to true 1:1 detail past 100%.** See "Zoom" and "Zoom detail
    rendering" below. Fit-to-window plus discrete percentage presets, Ctrl+wheel and
    toolbar/menu controls; up to 100% this re-scales the small work-copy preview, but past that
    a second background pipeline re-renders the actual visible region from a full-resolution
    source (up to ~2000px, capped by native resolution) — a later, explicit user request
    ("un po' come fa Lightroom"), not part of the original zoom work.
23. **Preservare il timestamp del file allo scatto originale, sia per il DNG salvato sia per
    l'anteprima JPEG esportata — done.** See "Preserving file timestamps" below. User-requested
    ("mi accorgo che uso spesso queste due funzioni"): every `save_to_dng()` and
    `export_preview_jpeg()` now sets the output file's OS modification/creation timestamps to
    the photo's own `DateTimeOriginal`, not "now" — so edited/exported files keep sorting
    correctly by shooting date in a file browser instead of clustering on today's date.
24. **Esportazione anteprima JPEG a piena risoluzione — done.** See "Preserving file
    timestamps" below (built together with item 23, same user request). File → Export Preview
    as JPEG… (Ctrl+E): renders the *current* editing state (independent of whether Save has
    been clicked) at full resolution via Adobe DNG Converter, the sole renderer in this app (see
    "Adobe DNG Converter is the sole renderer" above) — the exported bytes are exactly what Adobe
    itself embedded, since there's no local re-encoding step left to pick a JPEG quality for.
25. **Verificare filtro radiale e rimozione macchie contro Lightroom vero — not started, blocked
    on access to a real Lightroom install.** Raised by the user directly after asking how the
    local-adjustment compositing fix (see "Local adjustments are not rendered by Adobe DNG
    Converter" above) would actually be recognized in real Lightroom — the compositing only
    changes what DNGForge's *own* preview/save-embedded-JPEG shows; the `-XMP-crs:*` tags
    written to the saved file are completely unaffected and unchanged by it, and Lightroom
    always re-renders from those tags with its own full engine, never from this app's embedded
    preview. Of the three local-adjustment tools, two have an open, unresolved schema question
    specifically because no Lightroom install has ever been available in this environment to
    check against (see each section's own caveat, not repeated here):
    - **Radial filter** (`build_radial_xmp_value()`, "Radial filter persistence" below): `Angle`/
      `Midpoint`/`Roundness` are written using Adobe's own apparent defaults (50/0), since this
      app has no matching UI controls for them — whether real Lightroom expects `Angle` in
      degrees vs. radians, and the precise visual meaning of `Midpoint`/`Roundness`, is unverified.
    - **Spot removal** (`build_spot_xmp_value()`, "Spot Removal persistence" below): the
      `SourceX`/`OffsetY` field-naming asymmetry (one reads as an absolute coordinate, the other
      as a delta) is inferred from field names and Adobe-schema conventions in exiftool's
      `XMP.pm`, not confirmed fact — the one place a schema mistake would now be invisible in
      DNGForge's own preview too (unlike before the Adobe-only migration, when the local cv2
      Heal/Clone ran regardless of what got written to XMP).
    - **Graduated filter** is the one already confirmed exact, byte-for-byte, against a genuine
      Lightroom-saved region (`DSC_6751.dng`) — no open question there, not part of this item.

    **What "done" would look like**: place all three tools in DNGForge, save, open the file in a
    real Lightroom/ACR install, and confirm each region appears in the correct position with the
    correct effect. If radial or spot removal render wrong/misplaced there, that pinpoints
    exactly which field(s) in `build_radial_xmp_value()`/`build_spot_xmp_value()` need
    correcting — the failure mode would be geometric (wrong position/shape), not "no effect at
    all", so it should be immediately obvious which tool and which field is at fault.
26. **Galleria filmstrip — done.** See "Gallery filmstrip" below. **File → Apri cartella…**
    (Ctrl+Shift+O) scans a folder for `.dng` files and populates a disposable, per-session
    thumbnail strip docked at the bottom of the window — explicitly not a persistent catalog,
    per the user's own stated workflow (process a folder, then move the finished files
    elsewhere). Coexists with the pre-existing **File → Open DNG…** (single-file open),
    unchanged — the gallery is an additional, independent way to load a file, not a
    replacement for it.
27. **Copia/Incolla impostazioni — done.** See "Copy/Paste Settings" below. **Edit → Copia
    impostazioni**/**Incolla impostazioni** (Ctrl+Alt+C/V, real Lightroom's own shortcuts) —
    copies the *entire* current edit state and applies it to a different photo, for batches of
    similar shots. Scoped down at the user's own choice from two possible directions: applies
    to whichever single photo is currently open (not a multi-select-and-sync-to-many gallery
    operation), and copies everything in one shot (no Lightroom-style per-category checklist).
    Both were confirmed explicitly before building, not assumed.
28. **Conferma di chiusura — done.** See "Close confirmation" below. Closing the window now
    always asks first — a plain "Uscire da DNGForge?" Yes/No when there's nothing unsaved
    (including when no file is even open), or Save/Don't Save/Cancel when there is — where
    closing previously discarded any in-progress edit silently, with no confirmation of any
    kind, reported directly by the user as feeling unpolished.

### Undo/Redo (`_capture_edit_state()`, `_apply_edit_state()`, `undo()`/`redo()`, `revert_to_saved()`)

Added at the user's request ("possiamo implementare gli undo? o sarebbe troppo complesso in
generale? magari giusto tre o 4 step indietro o un ripristino alla versione dell'ultimo
salvataggio") — they explicitly offered two options, so both got built, since the second is nearly
free once the first exists.

**Snapshots are taken on the *settled* state, not on every widget signal.** A naive
"hook every slider's `valueChanged`" approach would fill the bounded stack with near-duplicate
in-drag states (a `QSlider` fires `valueChanged` continuously while being dragged, not just on
release). Instead, `_maybe_push_undo_state()` runs from inside `_render_preview()` — which the
60ms debounce timer (`_schedule_update()`) already only calls once dragging *pauses* — comparing a
freshly captured snapshot against `self._current_edit_state` and only pushing when something
actually changed. This needed **zero new signal wiring** anywhere else in the app; it piggybacks
entirely on infrastructure that already existed for a different reason (keeping renders cheap
during a drag).

**`_capture_edit_state()`/`_apply_edit_state()` are the in-memory counterparts to
`_build_xmp_crs_args()`/`_apply_xmp_crs_state()`** — same idea (snapshot every control, restore
every control), but staying entirely in memory (no `exiftool` round-trip, so cheap enough to run on
every settled change) and covering strictly more ground: radial/gradient filters and crop are part
of the undo snapshot too, even though those persist to XMP through a separate mechanism on save.
`radial_filters`/`gradient_filters`/`crop` are `copy.deepcopy()`'d going both into and out of a
snapshot — they're mutable (lists of dicts / a dict), so without the copy, later in-place edits to
`self.radial_filters` would silently corrupt already-stored undo history through aliasing.

**Bounded to `UNDO_STACK_MAX = 20`** ("tre o 4 step" was the user's stated minimum, not a ceiling —
20 gives real headroom without an unbounded memory footprint, since each snapshot includes deep
copies of the filter lists). A `self._restoring_state` flag suppresses `_maybe_push_undo_state()`
while `_apply_edit_state()` is running, so calling Undo doesn't itself get recorded as a new
change; making a genuinely new edit after an Undo correctly clears the redo stack (standard
undo/redo semantics, verified).

**`revert_to_saved()`** is the second, independent option: no snapshot bookkeeping at all, just
`load_dng(self.raw_path)` again — the file's own last-saved XMP state is already the "revert
target," so there's nothing to cache. Confirms via `QMessageBox.question` before discarding
unsaved work. `load_dng()` resets the undo/redo stacks and `_current_edit_state` unconditionally at
the top (a new/reloaded `self.raw` handle makes any prior history meaningless).

Verified end-to-end (`QT_QPA_PLATFORM=offscreen`, simulating the debounce timer's settle by calling
`_render_preview()` directly): a sequence of distinct changes (Exposure, then Shadows, then adding
a radial filter) produces exactly one stack entry per change, no duplicates from a repeated
no-op settle; two Undos correctly peel back to the pre-radial-filter, then pre-Shadows-change
state while leaving earlier changes (Exposure) intact; Redo correctly restores what was undone; a
fresh edit after an Undo clears the redo stack; the stack never exceeds 20 entries across 30 rapid
changes. `revert_to_saved()` verified against a file with a known saved baseline (Exposure=0.4)
and further unsaved changes on top (Exposure=1.9, Shadows=-40): confirming reverts correctly to
the *saved* value, not to zero/default, and resets both stacks.

**CONFIRMED BUG, fixed: `_apply_edit_state()` silently dropped roughly a third of the fields
`_capture_edit_state()` captures — Undo/Redo left them untouched.** Found incidentally while doing
an unrelated control-panel layout pass, not reported by the user first. `_capture_edit_state()`
builds its snapshot dict from every editable control, including Parametric Curve, HSL, Gray Mixer,
Black & White, Split Toning, and Defringe — but `_apply_edit_state()`, the restore counterpart
called by both `undo()` and `redo()`, never read those keys back out of the dict, only
profile/WB/tone/presence/curve/Sharpening/Noise Reduction/Grain/Vignette/radial-gradient-spot/crop.
So the snapshot always held the correct values (these fields were never missing from Undo history
itself, only from *applying* it), but calling Undo/Redo left Parametric Curve, HSL, Gray Mixer,
Black & White, Split Toning, and Defringe sliders sitting at whatever they already showed,
completely unaffected by the stack operation — a real, silent gap, not a cosmetic one, since
nothing in the UI signals that a subset of controls didn't actually revert.

Fixed by adding the missing restore lines, mirroring the existing per-field pattern exactly
(`self.xxx_slider.set_value(state["xxx"])`, looping `hsl_hue_sliders`/`hsl_saturation_sliders`/
`hsl_luminance_sliders`/`gray_mixer_sliders` the same way `_capture_edit_state()` builds them from
those dicts). `bw_checkbox` needed one extra step beyond a plain `setChecked()`: since the HSL/
Color panel now merges HSL and B&W into one panel with `_update_hsl_bw_content_visibility()`
deciding which sub-section is shown (see that method), restoring `bw_enabled` without also calling
`_update_hsl_bw_content_visibility()` afterward would leave the *wrong* slider group visible post-
undo even though the checkbox state itself was correct — wrapped in `blockSignals()` (matching
`wb_mode`/`tone_mode`/`curve_combo`'s own convention in this function) so the checkbox's own
`toggled` → `_on_bw_toggled()` → `_schedule_update()` doesn't fire mid-restore, with the visibility
call made explicitly instead.

**Verified** (`QT_QPA_PLATFORM=offscreen`, disposable copy of `RAW_NIKON_D90.dng`, same
`_render_preview()`-as-settle convention as the rest of this section): captured a baseline, changed
Split Toning shadow hue, an HSL hue slider, Black & White, a Defringe amount, and a Parametric
Curve slider together, settled (confirmed exactly one new stack entry), called `undo()`, and
confirmed all five reverted to their baseline values — before this fix, all five would have stayed
at the changed values after `undo()`.

### Zoom (`_update_image_label()`, `_zoom_step()`)

Added at the user's request. `self._zoom_level` is `None` (Fit — the app's original, only-ever
behavior) or a fraction (0.10 to 4.00) from `ZOOM_PRESETS`. The two modes drive the surrounding
`QScrollArea` differently: Fit keeps `setWidgetResizable(True)` (the label is forced to match the
viewport, image scaled to fit inside it — the pre-existing behavior); an explicit zoom level
switches to `setWidgetResizable(False)` and resizes `image_label` to the *actual* scaled pixmap
size, letting the scroll area's own native scrollbars handle panning — no custom pan/scroll code
needed, Qt already does this once the widget is genuinely larger than the viewport.

**Up to `ZOOM_DETAIL_THRESHOLD` (100%), this re-scales the work-copy preview, same as before** —
the live preview pixmap comes from Adobe DNG Converter's small working copy (`WORK_COPY_SIDE`,
1920px long edge — a plain FullHD screen's own long edge, not a number chosen for raw-pixel
accuracy; lowered from an original 2560px once the detail pipeline below existed to cover
anything sharper, per the user's own observation: "2560 px per la copia di lavoro forse è
troppo... un normale schermo full hd ha 1920px"). Re-scaling this small pixmap rather than
re-rendering it at every zoom level is what keeps zooming itself instant; see "Zoom detail
rendering" below for what happens past 100%.

Three ways in: the toolbar's +/− buttons and combo box (above the image), View menu actions with
shortcuts (Ctrl+= / Ctrl+- / Ctrl+0 Fit / Ctrl+1 100%), and Ctrl+scroll-wheel directly on the
image (`ImageLabel.wheelEvent()` — a plain wheel scroll is left untouched for the scroll area's
own default handling; only Ctrl+wheel is intercepted). `_zoom_step()`'s first step away from Fit
always lands on exactly 100% in either direction (the natural "actual size" anchor) rather than
jumping straight to the next preset past it, matching common image-viewer convention. Zoom resets
to Fit whenever a new file is opened (`load_dng()`).

Verified (`QT_QPA_PLATFORM=offscreen`): default state after load is Fit with the scroll area
resizable; the first zoom step from Fit lands on exactly 100% with the label resized to the
preview pixmap's own full dimensions and the scroll area switched to non-resizable; stepping
further moves correctly through the preset list in both directions; selecting a preset directly
from the combo box works; simulating a Ctrl+wheel event (`zoomRequested.emit()`) zooms correctly;
returning to Fit restores `setWidgetResizable(True)`; opening a new file resets an explicitly-set
zoom level back to Fit.

### Zoom detail rendering (past 100%, `_schedule_zoom_detail_render()`, `_compose_zoom_detail()`)

User-requested, explicitly modeled on Lightroom's own 1:1 zoom: "vorrei che lo zoom possa davvero
renderizzare la porzione che mi interessa ad una risoluzione maggiore... risoluzione dovrebbe
essere sul lato lungo di circa 2000 pixel prendendoli dal file originale, sempre se non ho
zoomato troppo e quei 2000 pixel non ci sono." Past `ZOOM_DETAIL_THRESHOLD` (zoom level 1.0 —
i.e. once the work copy's own 1920px is already being upscaled to display), a second, independent
background pipeline re-renders just the currently visible region from a full-resolution source,
up to `ZOOM_DETAIL_MAX_SIDE` (2000px, the user's own number), capped by the region's own *native*
pixel size so this never invents detail beyond what the sensor actually recorded.

**Interaction model — deliberately unchanged from before**: native scrollbars keep working exactly
as they did (a design choice confirmed with the user rather than assumed — the alternative was
Lightroom-style click-and-drag panning, replacing the scrollbars entirely, which would have been a
bigger interaction change for a debatable benefit). Panning/zooming always shows *something*
correct immediately (the plain, already-working work-copy preview, upscaled); the higher-detail
patch composites on top once it lands, shortly after settling — never blocks, never shows a gap.

**Two-stage pipeline, mirroring the main preview pipeline's own architecture** (same
generation-counter/single-in-flight-plus-pending queueing, just a second independent instance of
that pattern):
- **Zoom source** (`_ensure_zoom_source()`): the first time zoom crosses the threshold for the
  current file, `DngConverterWorkCopyThread` (generalized to accept `out_name`/`side` — `side=None`
  omits Adobe DNG Converter's own `-side` flag entirely, producing a copy at *full native
  resolution* instead of a capped one) builds `zoom_source.dng`, alongside `work.dng` in the same
  per-file temp dir. Built lazily — files the user never zooms in this far never pay the cost.
- **Detail render** (`_start_zoom_detail_render()`/`_on_zoom_detail_render_ready()`, reusing
  `DngConverterRenderThread` with its own now-optional `side` parameter): renders the *whole
  effective frame* (the user's own committed crop/rotation, if any — never just the tiny viewport
  region on its own, see the CONFIRMED BUG below for why) from `zoom_source.dng`, at whatever
  full-frame resolution makes the visible region-within-it come out close to its own correct size,
  then crops just that region back out with a plain `QPixmap.copy()` + `.scaled()` in
  `_on_zoom_detail_render_ready()` — a display-only geometric operation on pixels Adobe already
  rendered, the same category as every other `.scaled()` call already used for Fit/zoom display
  throughout this app, not a new rendering decision.

**CONFIRMED BUG, root-caused, changed the whole approach**: the first implementation wrote the
viewport region directly as `-XMP-crs:HasCrop`/`Crop*` (the obvious, cheaper approach — request
exactly the resolution needed, nothing more) and asked Adobe DNG Converter to crop-and-scale in
one step. This **silently lost real detail**: Adobe DNG Converter stops embedding a full-size
`JpgFromRaw` preview once a *cropped* conversion's requested `-side` drops below roughly
1000-1500px — confirmed by direct experimentation (varying `-side` from 700 to 2200 on both
cropped and uncropped conversions, bypassing this app) — silently falling back to the much
smaller, fixed-ratio `PreviewImage` tier instead (consistently ≈1/6 of the requested resolution
across every value tried, e.g. a 715px request came back as a 119×115 preview). Exactly the
"a flat write can be structurally valid and still be silently ignored by Adobe's renderer" lesson
already flagged in "CONFIRMED BUGS, fixed: Curve and Profile" above, but for resolution instead of
XMP shape — and the common case for a zoomed-in region (viewport-sized, typically well under
1500px) hit this every time, not just as an edge case. Rendering the *whole frame* — always
comfortably above that threshold, since the full frame is being sized to make the *region within
it* reach the target, not the region directly — sidesteps it entirely, confirmed by requesting the
render resolution and the resulting cropped detail patch's own size and finding them match exactly
(715px requested → 715px delivered, 357px requested → 357px delivered, both through the real app
pipeline, not an isolated exiftool test).

`ZOOM_DETAIL_MIN_REQUEST_SIDE` (1600) floors the full-frame request regardless, as a safety margin
above that empirical threshold; when the region needs less than that (i.e. still zoomed in enough
that a smaller full-frame render would suffice), the resulting crop comes out *larger* than the
correct target size, and `_on_zoom_detail_render_ready()`'s own final `.scaled()` trims it back
down — so the displayed result is always exactly the correct, native-capped size regardless of how
large a margin the full-frame request used internally.

**CONFIRMED BUG, fixed: editing a slider while zoomed in past 100% made the displayed view jump
or "zoom" to a different framing at random.** Reported directly by the user ("se modifico degli
slider quando sto visualizzando uno zoom, la zona visualizzata si sposta e/o si zooma a caso").
Root-caused with a scripted repro (zoom in, pan to a non-centered position, apply a sequence of
slider edits while polling `self._preview_pixmap.size()` and the scrollbar values after each one)
rather than guessing from the symptom: the *scroll position never actually moved* — the bug was
that `self._preview_pixmap` (the **main** pipeline's own small work-copy render) intermittently
ended up holding the **zoom-detail** pipeline's output instead, a completely different resolution
and framing, which then got displayed scaled by the current zoom level exactly as if the view had
jumped. Confirmed by the repro directly: `_preview_pixmap` size held steady at 1920×1275 (the
work-copy resolution) for two edits, then jumped to ~2849×1892 (a zoom-detail-sized render) on the
third — alongside literal `"File not found - rendered.dng"` errors surfacing from the background
thread. Real cause: `DngConverterRenderThread.run()` (used by *both* the main preview pipeline, on
`work.dng`, and the zoom-detail pipeline, on `zoom_source.dng` — see "Zoom detail rendering"
above) hardcoded the exact same two output filenames, `"rendered.dng"`/`"rendered_preview.jpg"`,
inside the *same* per-file temp directory both files already live in. Editing a slider while
zoomed in past `ZOOM_DETAIL_THRESHOLD` starts both pipelines' background threads at roughly the
same time (`_render_preview()` calls `_schedule_dngconv_render()` and
`_schedule_zoom_detail_render()` back to back), so their two concurrent Adobe DNG Converter
processes — and the `os.unlink()` cleanup in each thread's own `finally` block — raced to
write/read/delete the *same* two files, occasionally letting one pipeline's `_on_dngconv_render_ready()`
read back the *other* pipeline's JPEG. Not a scroll-position or zoom-math bug at all — the
scrollbar value was correct the whole time, only the *pixels behind it* were briefly the wrong
ones. Fixed by minting a `uuid.uuid4().hex[:8]` token once per `DngConverterRenderThread` instance
(`self._token`, set in `__init__`) and folding it into both filenames
(`rendered_{token}.dng`/`rendered_preview_{token}.jpg`, including the `-o` argument passed to
Adobe DNG Converter itself, which must match) — no two concurrently-running render threads, from
either pipeline, can ever collide on a shared filename again. **Verified**: re-ran the exact same
scripted repro (zoom in, pan, five sequential slider edits) after the fix — `_preview_pixmap` now
stays at a stable 1920×1275 across all five edits, no `"File not found"` errors, and a real
`ImageLabel.grab()` screenshot taken before vs. after three edits (Exposure/Saturation/Clarity)
shows the identical framing with only the rendered tone/color changed, exactly as expected.

**Region ↔ crop composition** (`_compute_visible_region()`, `_region_within_effective_crop()`):
the visible viewport is computed as fractions of the *full raw frame*, composing the scroll
position + zoom level with the user's own committed crop (`_effective_crop_for_render()`, the same
"guide vs committed" helper the crop tool itself uses) — and the inverse composition maps back to
fractions of the *effective crop's own* frame for both the Qt-crop-out step and for
`_compose_zoom_detail()`'s on-screen paint position. **Rotated crops are a known, deliberate scope
limit**: mapping a sub-rectangle back through a rotation isn't handled, so both directions bail out
(returning `None`) when `crop.angle != 0`, falling back to the plain work-copy preview — same
graceful degradation this app already uses elsewhere rather than showing a misaligned patch.

**Verified** (`QT_QPA_PLATFORM=offscreen`, real Adobe DNG Converter runs, not mocked): zooming past
100% builds the zoom source and lands a detail patch whose long edge exactly matches its own
computed native cap; the composited image differs from the plain upscaled preview (proving the
patch is actually painted in, not silently skipped); zooming further in (400%, an even smaller
native region) still lands exactly on the newly (smaller) computed cap — confirmed *not* upscaled
beyond it; returning to Fit clears the patch; with a real committed crop active, the detail
region's fractions stay correctly bounded within the crop's own edges, and panning to the
crop's far corner (scrollbar values driven directly) correctly triggers a new, different region.

**Status bar feedback** (user-requested after trying the feature: "sarebbe bello avere nella
status bar una scritta che mi avvisi che sta generando l'immagine zoomata e magari i pixel
rasterizzati così so se sto zoomando troppo e l'immagine è solo zoomata digitalmente"):
`_start_zoom_detail_render()` shows "Rendering dettaglio zoom…" while the background render is in
flight (same pattern as the main pipeline's own "Rendering anteprima Adobe DNG Converter…"), and
`_on_zoom_detail_render_ready()` reports the *actual rasterized resolution* once it lands — e.g.
"Dettaglio zoom: 715×689px" — appending "— a questo livello lo zoom è solo digitale (nessun
dettaglio nativo in più)" whenever `target_cap` was set by the region's own native pixel size
rather than `ZOOM_DETAIL_MAX_SIDE` (tracked via `_pending_zoom_detail_native_capped`), so the user
can tell "genuinely sharper" from "just enlarged" apart while zooming, exactly as asked.
`_on_zoom_detail_render_failed()` shows "Rendering dettaglio zoom non riuscito" on failure (leaving
whatever was already displayed untouched, same convention as the main pipeline's own failure
handler). **Wording note**: the first version phrased this as a forward-looking warning ("zoom
ulteriore è solo digitale," implying *future* zooming would stop helping) — the user corrected it
to describe the *current* level directly instead ("non 'ulteriore' ma 'lo zoom a questo livello è
solo digitale'"), since the point is knowing whether *what's on screen right now* is real detail
or just enlarged pixels, not a prediction about zooming further. Verified empirically (not just
theorized) that the flag itself was already computing correctly at every zoom preset tested
(150%-400%, real Adobe DNG Converter renders) — a modest-resolution test file (`RAW_NIKON_D90.dng`,
12MP) combined with the app's own default window size exhausts native detail almost immediately
past 100% (the first available preset above 1.0 is already 150%, no finer step in between), so it
reads "digital-only" at every preset tried on that file — expected to vary more on a
higher-resolution sensor (confirmed by hand-computing the same math for a 24MP sensor at the same
window size and zoom level, landing above `ZOOM_DETAIL_MAX_SIDE` instead), not a bug in the flag.

### Click-and-drag panning (`ImageLabel.panRequested`, `DNGForge._on_pan_requested()`)

**CONFIRMED BUG, fixed: zoomed-in views were effectively unusable.** Reported directly by the
user after trying the zoom detail feature above: "quando la vista è oltre il fit della finestra la
vista è bloccata... viene zoomato l'angolo superiore sinistro senza possibilità di muovermi lungo
l'immagine." Verified this wasn't actually a `QScrollArea` geometry bug — scripted checks confirmed
the scroll range and `setValue()` both worked correctly at the Qt level — the real gap was
*interaction*: this app only ever supported the plain native scrollbars for panning, with no
click-and-drag support on the image itself, which is what any Lightroom-style (or, really, any
modern image viewer's) zoomed view is expected to support. `ImageLabel` already intercepts every
mouse press for its own tools (WB picker, radial/gradient/crop/spot handles), so a plain
click-drag on the image did nothing at all once zoomed in — not locked, just never implemented.

**Fix**: a plain press that no other tool/handle/overlay claims (`mousePressEvent`'s existing
final fallback) now also records a *potential* pan-drag start; if `mouseMoveEvent` sees the mouse
move past `PAN_DRAG_THRESHOLD` (3px) before release, it's treated as a pan — emitting
`ImageLabel.panRequested(dx, dy)`, handled by `DNGForge._on_pan_requested()` by moving the
`QScrollArea`'s own scrollbar values (dragging right/down moves the *content* right/down, so the
scroll position moves the opposite way, the standard click-and-drag-pan convention). Below that
threshold, it's still a plain click, same as before. Cursor switches to `Qt.ClosedHandCursor`
once a drag is confirmed, back to `Qt.ArrowCursor` on release — the only visible affordance change
needed, since panning is purely a fallback when nothing else claims the mouse.

**Second bug found while fixing the first, same investigation**: `clicked` (the signal feeding the
WB picker and radial/gradient/spot placement) used to fire **unconditionally on every press**, not
only on a genuine click — meaning a click-and-drag pan would *also* fire whichever picker happened
to be armed, at the press-down point, which is not what dragging to pan means. Fixed by deferring
`clicked` to `mouseReleaseEvent`, and only emitting it when the press never crossed
`PAN_DRAG_THRESHOLD` (i.e. it really was a click, not a drag) — a few-millisecond timing shift from
press to release that's imperceptible for a genuine click, but now correctly distinguishes the two
gestures. This bug predates the pan feature itself; it just had no way to manifest before, since
there was previously no drag gesture on a plain (un-armed) press to begin with.

**Verified** (`QT_QPA_PLATFORM=offscreen`, synthetic `QMouseEvent` press/move/release sequences —
same scripted-interaction convention already used throughout this file for radial/gradient/crop/
spot dragging): a plain click (no movement) still fires `clicked` and leaves the scrollbars
untouched; a real drag pans the scrollbars in the correct direction and does *not* fire `clicked`;
pan state is fully cleared after release, so a subsequent plain click still works normally: and,
critically, dragging an *existing* handle (tested: a placed radial filter's center handle) still
takes priority over panning and moves the filter, not the scroll position — pan is strictly a
fallback for "nothing else claimed this drag," never able to hijack an in-progress tool.

### Preserving file timestamps (`save_to_dng()`, `export_preview_jpeg()`)

Added at the user's request — they use both often enough to flag as its own feature: every
`save_to_dng()` and the new `export_preview_jpeg()` (File → Export Preview as JPEG…, Ctrl+E) set
the output file's OS modification *and* creation timestamps to the photo's own `DateTimeOriginal`
(when it was actually taken), not "now" — so a file edited or exported today still sorts correctly
by shooting date in a file browser, instead of every save/export bumping it to today's date and
scattering a shoot across a file listing sorted by date modified.

Uses exiftool's own `FileModifyDate`/`FileCreateDate` pseudo-tags (verified: `FileCreateDate` is a
real, working directive on Windows/NTFS, not just `FileModifyDate` — confirmed by touching a file
to "now" first, then checking both actually revert to the source `DateTimeOriginal` afterward).

**CONFIRMED BUG, fixed before this ever shipped to the user**: on `save_to_dng()`, the first
version silently did nothing — `FileModifyDate` stayed at "now" instead of the shot date. Root
cause: the two new directives were appended *after* an earlier `-tagsfromfile tmp_jpeg.name`
(used for `-ifd0:imagewidth<imagewidth` etc.) in the same exiftool argument list — in exiftool,
`-tagsfromfile` sets the source for *every subsequent* tag-copy directive until reset, so
`-FileModifyDate<DateTimeOriginal` was silently trying to read `DateTimeOriginal` from the
freshly rendered temp JPEG (which has no EXIF data at all) instead of from the DNG itself,
and found nothing to copy. Fixed by inserting `-tagsfromfile @` (exiftool's syntax for "the target
file itself") immediately before the two timestamp directives, resetting the source. Caught by an
automated test that deliberately bumped the file's mtime to "now" *before* saving and asserted it
came back to the 2009 shooting date afterward — not by assuming the CLI syntax was self-evidently
correct once it ran without error.

`export_preview_jpeg()` needs the cross-file form instead (`-tagsfromfile <dng_path>` naming the
*source* DNG explicitly, since the target being written is a brand new JPEG, not the DNG) —
verified working the same way, both directions tested independently before being combined.
Renders the *current* editing state at full resolution via Adobe DNG Converter — independent of
whether Save has been clicked, unlike extracting whatever preview happens to already be embedded
in the DNG, which could be stale relative to in-progress unsaved edits.

Unlike before rendering moved entirely to Adobe DNG Converter, **exiftool is now a hard
requirement for export, not just for the optional timestamp step** — it's needed to write the XMP
edit-state tags onto the temp copy Adobe renders from, and to extract the resulting preview bytes;
without it there is nothing to export at all. There is also no longer a JPEG-quality distinction
between this export (previously quality 92) and Save's own embedded preview (previously 69) — the
exported bytes are exactly what Adobe DNG Converter itself embedded, since there's no local
re-encoding step left to pick a quality for.

Verified (the file-timestamp mechanics specifically, against the earlier cv2-based pipeline):
bumped a test file's mtime to "now", saved, confirmed both the file's modification time and
(independently) its EXIF `DateTimeOriginal` matched the original 2009 shooting date afterward.
Verified again (the Adobe-based rendering path specifically, this session): `export_preview_jpeg()`
run end-to-end against `RAW_NIKON_D90.dng` with a mocked save dialog completes in ~2.9s and writes
a real, non-trivial JPEG (1.7MB) with the timestamp fix still applied.

### CONFIRMED BUG, fixed: the exported JPEG carried no metadata at all

Reported by the user right after the white-balance fixes above: "ora quando esporto il jpg finale
non viene scritto nessun tag." Confirmed by dumping a real exported file's tags directly: Adobe DNG
Converter's embedded `JpgFromRaw`/`PreviewImage` stream — what `_extract_dngconverter_preview()`
pulls out and what `export_preview_jpeg()`'s output *was*, verbatim, before this fix — carries no
EXIF block whatsoever, just raw JPEG image data plus a minimal APP14 color-transform marker. No
`Make`/`Model`/`LensModel`/`DateTimeOriginal`/`ExposureTime`/`ISO`/`FocalLength`, nothing. This
makes sense once stated plainly: Adobe DNG Converter's *preview* stream was never designed to be
extracted and used standalone — normally it stays embedded inside the DNG container, which already
carries all of that metadata in its own IFD0/EXIF structure (exactly why `save_to_dng()` never
needed to add any of this: the DNG it's embedded back into already has it). A bare extracted JPEG
has no such container to inherit from.

**Fix**: `export_preview_jpeg()`'s existing timestamp-only exiftool call was extended into one
combined call that also copies real metadata from the source DNG, mirroring `save_to_dng()`'s own
established `-tagsfromfile`-source-switching conventions (each `-tagsfromfile` changes the *source*
for every subsequent directive until the next one, the same idiom the timestamp fix itself already
relies on):
1. `-tagsfromfile <raw_path> -all:all --xmp-crs:all` — copies every real EXIF/IPTC/XMP tag from the
   source DNG (Make/Model/LensModel/exposure settings/GPS/whatever else it carries), the standard
   exiftool idiom for "convert raw to JPEG, keep all metadata" (documented in exiftool's own FAQ).
   `--xmp-crs:all` explicitly excludes the Camera Raw develop-recipe tags (`Exposure2012`,
   `WhiteBalance`, etc.) — those describe *how to render*, meaningless clutter on a JPEG that's
   already the rendered result, unlike everything else which is real photo metadata worth keeping.
2. `-EXIF:Software=`/`-XMP-xmp:CreatorTool=` stamped to `APP_SOFTWARE_NAME` *after* step 1, so this
   app's own identity wins over whatever got wildcard-copied in (usually "Adobe DNG Converter") —
   same convention `save_to_dng()` already uses, confirmed empirically that a later explicit
   directive to the same tag overrides an earlier wildcard copy before relying on it here.
3. `-tagsfromfile @` (self) `-ExifIFD:ExifImageWidth<File:ImageWidth`/`ExifImageHeight<File:ImageHeight`
   — re-syncs the EXIF dimension tags from the JPEG's own *actual* decoded pixel size. Needed
   because step 1's wildcard copy would otherwise carry over the source DNG's raw sensor
   dimensions verbatim, silently disagreeing with the real exported JPEG whenever a crop is
   active. **Verified this matters, not just theorized**: without this step, exporting a
   deliberately different-sized (2000×1500, simulating a crop) JPEG still claimed
   `ExifImageWidth`/`ExifImageHeight` = 4288×2848 (the DNG's own raw size); with it, both read the
   correct 2000×1500.
4. The pre-existing `DateTimeOriginal`-sourced OS-timestamp fix, unchanged, folded into the same
   call instead of a separate one (one exiftool spawn instead of two).

**Verified end-to-end** through a real `DNGForge` instance (`QT_QPA_PLATFORM=offscreen`, mocked
save dialog, disposable copy of `RAW_NIKON_D90.dng`): the exported JPEG correctly shows
`Make=NIKON CORPORATION`, `Model=NIKON D90`, `LensModel=50.0 mm f/1.4`,
`DateTimeOriginal=2009:02:10 19:47:07`, `ExposureTime=1/60`, `FNumber=3.5`, `ISO=100`,
`FocalLength=50.0 mm`, `Software`/`CreatorTool=DNGForge (Beta)` (not "Adobe DNG Converter"), and
`ImageWidth`/`ExifImageWidth` correctly matching (4288, the file's real uncropped size) — with no
`XMP-crs:*` tags present at all, confirming the exclusion worked.

### Save (`save_to_dng()`)

File → Save / Ctrl+S / the **Save** button. Always embeds directly into the DNG — deliberately
does **not** replicate RethinkRAW's default-Save behavior of falling back to an external `.xmp`
sidecar on a pristine DNG (confirmed by testing RethinkRAW directly, see project memory
`dngforge-rethinkraw-research`); every save, including the very first one, goes straight into the
file, modeled on RethinkRAW's *Export → DNG* path instead.

**Sequence (rendering via Adobe DNG Converter, the sole renderer — see that section above), from
a disposable full-resolution copy, never the original directly:**
1. `tempfile.mkdtemp()` a scratch dir; `shutil.copyfile()` the original into it (`full.dng`).
2. Write the current edit state onto that **copy** as `-XMP-crs:*` tags
   (`_build_all_edit_xmp_args(for_save=True)` — `for_save=True` gets the real committed crop, not
   the guide-mode full frame, see `_effective_crop_for_render()`).
3. Run Adobe DNG Converter (`-c -p2`) on the copy, producing `rendered.dng` in the same scratch dir.
4. Extract its embedded preview via `_extract_dngconverter_preview()` (the same
   `JpgFromRaw`-then-`PreviewImage` helper the background preview pipeline uses, see above).
5. **Close `self.raw` before touching the *original* file** (the exact Windows file-lock bug found
   and root-caused in RethinkRAW — a lingering read handle blocks `-overwrite_original`'s
   rename-in-place, see project memory).
6. One combined `exiftool` call against the **original**: embeds the extracted preview
   (`-PreviewImage<=...`/`-JpgFromRaw<=...` + `-tagsfromfile` to fix up `ifd0:ImageWidth/Height/
   RowsPerStrip` from it) and writes the same edit-state tags built in step 2 (reused, not
   rebuilt, so the tags recorded on the original are guaranteed to match what was actually
   rendered) — `-overwrite_original`.
7. Reopen `self.raw` and delete the scratch dir in a `finally` block, save success or failure, so
   the app is never left without a usable handle.

The original file is **only ever touched by the single combined call in step 6** — every risky
step (the Adobe conversion, the extraction) runs against the disposable copy first, so a failure
partway through never leaves the original half-modified. This is a stronger atomicity guarantee
than the pre-Adobe-only version of this function had, gained essentially for free as a consequence
of needing a scratch copy for Adobe DNG Converter to work from anyway. Errors surface via a
`QMessageBox`, not a silent failure or a crash.

Verified end-to-end (this session, against `RAW_NIKON_D90.dng`): `save_to_dng()` run through a real
`DNGForge` window (not mocked) completes in ~3.3s and leaves `self.raw` usable afterward. Also
verified on the older cv2-based pipeline before this migration (disposable copies, not the tracked
sample): saved file still opens fine in `rawpy` afterward; `exiftool -XMP-crs:all` reads back
exactly the values that were set; the embedded preview, extracted and viewed, visibly shows the
edit (not just present bytes) — the embedding mechanics (steps 5-7 above) are unchanged by the
Adobe migration, only how the preview JPEG itself gets produced (steps 1-4) changed. A pre-existing
"[minor] Suspicious MakerNotes offset" exiftool warning on the Leica M8 test sample is unrelated to
saving — confirmed present on the untouched original too.

**CONFIRMED BUG, fixed: a saved edit didn't show up in FastStone** (and presumably any other
LibRaw-based viewer), even though `exiftool -b -PreviewImage` extracted straight from the saved
file — bypassing any viewer entirely — visibly showed the edit. First hypothesis (viewer thumbnail
cache) was reasonable but wrong, disproved by the user's own A/B test: the *same* viewer showed a
Lightroom-saved edit to the same photo correctly, so it wasn't blanket-stale-caching anything.
Root cause found by hashing every embedded-image tag before/after a real `save_to_dng()` call:
this DNG's structure (from Adobe DNG Converter's `-lossy` mode) embeds **two separate** full-
resolution JPEGs under **different tag names** — `PreviewImage` (spanning `IFD0`/`SubIFD1`/
`PreviewIFD`, which `-PreviewImage<=` *does* update, confirmed byte-identical to the source JPEG)
and a wholly distinct `JpgFromRaw` (`SubIFD2`), which `-PreviewImage<=` **never touches** — hashed
byte-for-byte identical to the pre-edit original no matter how many times a save ran. A real
Lightroom-saved comparison file (`DSC_6751.dng`, user-provided, developed and saved years earlier)
confirmed Lightroom itself writes multiple distinct preview resolutions across `IFD0` (a genuine
small ~256px thumbnail), `SubIFD1` (medium), and `SubIFD2`/`JpgFromRaw` (full res, "Full Size"
preview) — all three, not just one — matching this file's own structure. LibRaw-based tools are
known to read `JpgFromRaw` specifically as *the* embedded preview, which explains both directions
of the bug: Lightroom updates it correctly, this app didn't.

Fix: `save_to_dng()` now writes `-JpgFromRaw<=` alongside `-PreviewImage<=` with the same rendered
JPEG (see the code comment at that line for the short version of this story). Harmless to always
include even on a DNG structure with no `JpgFromRaw` slot — confirmed via `RAW_LEICA_M8.DNG` (which
also happens to have one) and via general `exiftool <TAG><=` semantics: writing a group/tag pairing
that doesn't exist in a given file's IFD structure is a no-op, not an error, the same tolerance
`-ifd0:imagewidth<imagewidth` already relies on a few lines above it. Verified end-to-end through
the real `save_to_dng()` code path (not just an isolated exiftool call): hashed `JpgFromRaw` before
and after a save with a large, unmistakable edit (+3EV exposure) — byte-identical before the fix,
different after, and the extracted bytes decode to the correct full resolution with a dramatically
different mean pixel value (153.8 → 222.0), confirming it's a real visual change, not just
incidental re-encoding noise.

**Ruled out along the way, for the record:** `PreviewSettingsDigest`/`PreviewDateTime`/
`PreviewApplicationName` staying stale after a save (still reading "Adobe DNG Converter", the
original conversion tool, not this app) looked like a plausible related culprit — Adobe's spec
defines that digest specifically to validate a preview matches current settings — but exiftool
refuses to write any of them ("unsafe for writing", by design, to avoid producing structurally
invalid DNGs), so this wasn't pursued further as a fix target. Also considered adding a dedicated
small `ThumbnailImage` (the classic EXIF IFD1 slot) since many simple viewers check it first, but
this DNG's structure has no IFD1 chain and exiftool's write silently no-ops rather than creating
one — not viable on this file structure, abandoned once `JpgFromRaw` turned out to be the real
answer anyway.

### Reloading edit state on open (`read_xmp_crs_state()`, `_apply_xmp_crs_state()`)

Until this pass, `load_dng()` always called `reset_controls()` unconditionally — every control
came up at Default on reopen even for a DNG this app had itself saved with a full set of edits,
even though the exact values were sitting right there in the file's own `-XMP-crs:*` tags (Save
writes real Lightroom-compatible tag names, see above). Write-only edit metadata, never read
back — flagged by the user as "mi sembrava una funzione ovvia" (an obvious feature to be missing).

`read_xmp_crs_state(exiftool_path, path)` reads back exactly the tag set `_build_xmp_crs_args()`
writes (`XMP_CRS_TAGS`, same bare names on both sides so the two stay in sync by construction) via
one `et.get_tags()` call, keyed by bare tag name after stripping `ExifToolHelper`'s default `-G`
group prefix (`"XMP:WhiteBalance"` → `"WhiteBalance"` — confirmed empirically: even though the
write side uses `-XMP-crs:Tag` and the *read* side requests `-XMP-crs:Tag` too, `ExifToolHelper`'s
default `common_args=["-G", "-n"]` is **group-0**, so results always come back keyed `"XMP:Tag"`,
never `"XMP-crs:Tag"` — same convention `load_dng_camera_profile()` already relies on for
`"EXIF:ColorMatrix1"`). Returns `None` (meaning: leave `reset_controls()`'s Default state alone)
when exiftool is unavailable *or* the file has no crs data at all — detected via the always-written
`WhiteBalance` tag being absent, since that's the one field `_build_xmp_crs_args()` never omits.

`_apply_xmp_crs_state()` runs immediately after `reset_controls()` in `load_dng()` and mirrors the
write side's logic field-by-field. Profile is the one field that doesn't mirror `CameraProfile`
directly — named profiles round-trip via `LookName` instead (see "CONFIRMED BUGS, fixed: Curve and
Profile" above for why, and for the `-struct`-mode gotcha that made this read need its own fix
too); an ambiguous read (no `LookName`, no `ConvertToGrayscale`) resolves to "Adobe Standard," not
"Custom" — the two render identically, so this is a display-only distinction, not a functional one.
`AutoTone=True` restores Tone mode to Auto (values themselves are recomputed live, not
read from tags, matching how Auto already behaves). `ToneCurvePV2012`'s `"x, y; x, y; …"` string is
parsed positionally into `CurveEditor`'s 5 fixed control points (`CurveEditor.set_points()`, new —
ignores mismatched-length input rather than raising, in case a foreign tool ever writes a
differently-shaped curve there). Radial filters restore separately — see "Radial filter
persistence" below, which covers a genuinely different (structured, nested) tag shape from
everything else in this section.

Verified with a scripted round-trip (`QT_QPA_PLATFORM=offscreen`, disposable copies of the tracked
test DNG, not run against it directly): saved a distinctive value on every control, opened a
**second, independent** `DNGForge` instance on the same file (not just re-rendering the same
window — a real "close and reopen"), confirmed every slider/combo/curve-point matches exactly.
Also checked the edge cases the field-mirroring logic depends on: a pristine file never saved by
this app loads with all-Default state and doesn't crash (`WhiteBalance` tag absent → `None` →
skip); Auto WB + Auto Tone round-trip with their sliders correctly left disabled; Profile "Custom"
round-trips correctly (see "CONFIRMED BUGS, fixed: Curve and Profile" above for the full writeup
and its own verification, since fixing this correctly needed more than the naive
absence-implies-Custom inference this section originally relied on).

### Virgin-file defaults match Lightroom, not zero (`reset_controls()`)

User-directed principle, added during the same Lightroom-structure audit that built roadmap items
16-20: "per i file dng vergini mai modificati prima, per i vari settaggi si devono usare i valori
di default che usa lightroom, quando questi sono diversi da zero o dal valore di riferimento
'neutro'" — for a DNG this app has never saved (`read_xmp_crs_state()` returns `None`, so
`reset_controls()`'s own values are what the user actually sees), any control whose *real*
Lightroom default isn't 0/neutral should show that real default, not a blanket zero.

**`reset_controls()` is the single right place for this** — it's already called both from
`load_dng()` (before `_apply_xmp_crs_state()` conditionally overrides it with real saved data) and
is conceptually what a "Reset" action means, and real Lightroom's own Develop-panel Reset button
doesn't zero everything either — it resets to Lightroom's *default* recipe, e.g. Sharpening Amount
back to 40, not 0. So one set of default values correctly serves both "freshly opened, never
edited" and "user hit Reset," matching Lightroom's own behavior in both cases without needing two
different code paths.

**Changed from this app's previous all-zero convention**: `sharpness_slider` (Amount) 0→**40**,
`color_nr_slider` (Color) 0→**25**, `color_nr_detail_slider`/`luminance_nr_detail_slider` (Color
Detail / Luminance Detail) 0→**50**. Everything else already defaulted correctly (either
genuinely 0 in real Lightroom too — every Basic-panel slider, HSL, Gray Mixer, Split Toning — or
was already given a non-zero default when first built: Grain Size/Frequency 25/50 (item 14),
Vignette Midpoint/Feather 50/50 (item 15), `SharpenRadius` 1.0, Parametric split points 25/50/75,
Defringe `HueLo`/`HueHi` 30/70 (Purple) and 40/60 (Green)).

**Confidence level stated explicitly, per this project's own culture of not letting "looks
right" pass as "confirmed"**: `SharpenRadius`=1.0, the 3 Parametric split points, and all 4
Defringe `HueLo`/`HueHi` values are **hard-confirmed** — read directly off `DSC_6751.dng`'s own
still-untouched fields (a genuine Lightroom-saved file, not this app's own output). Sharpening
Amount=40, Color NR Amount=25, and Color/Luminance NR Detail=50 are **well-established Lightroom
documentation values**, not independently confirmed against a local reference file the way the
others are — `DSC_6751.dng` itself doesn't help here, since its own Sharpening/NR fields show
clear signs of having been actively edited by the photographer (e.g. `SharpenDetail=50`, not the
widely-documented default of 25), so it can't be trusted as "the untouched default" for *those*
specific fields the way it can for Radius/Parametric-splits/Defringe-hues, where the file's own
Amount fields being 0 makes an untouched reading far more plausible.

**Verified**: stripped every `-XMP-crs:*` tag from a disposable copy of the tracked test file
(simulating a genuinely virgin DNG, since the tracked file itself has been saved-to by this app
across many earlier sessions and so is no longer virgin — confirmed directly: it already carries
`WhiteBalance=Auto` and `Sharpness=0`/`ColorNoiseReduction=0`, old values from before this change)
and loaded it in a real `DNGForge` instance: `sharpness_slider`=40, `color_nr_slider`=25,
`color_nr_detail_slider`=50, `luminance_nr_detail_slider`=50, `sharpen_radius_slider`=1.0, all 3
Parametric splits, and all 4 Defringe hue values all landed exactly on their new defaults.

### Radial filter persistence (`build_radial_xmp_value()`, `read_radial_filters()`)

Writes/reads Adobe's *real* local-adjustment schema, `crs:CircularGradientBasedCorrections` — a
list of `crs:Correction` structs, each holding a `crs:CorrectionMasks` list with one
`Mask/CircularGradient` entry — not a simplified stand-in. Field names, nesting, and types were
**not guessed**: pulled directly from exiftool's own `XMP.pm` source (`%sCorrection` /
`%sCorrectionMask` in the `crs` namespace, e.g. `LocalExposure2012`/`LocalContrast2012`/
`LocalSaturation`, and the mask's `Top`/`Left`/`Bottom`/`Right`/`Angle`/`Midpoint`/`Roundness`/
`Feather`/`Flipped`/`Version`/`SizeX`/`SizeY`/`X`/`Y`) — the same file this project already
depends on at runtime, so "what exiftool will accept" and "what the schema actually is" can't
drift apart.

**Writing** uses exiftool's JSON-like struct-literal CLI syntax (confirmed the only reliable way
to write this shape — flattened-tag-with-index syntax like `CircGradBasedCorr[1]What=...` was
tried first and rejected outright as invalid tag names): one
`-XMP-crs:CircularGradientBasedCorrections=[{...},{...}]` argument per save, built by
`build_radial_xmp_value()`. Struct field names must be the real member names
(`LocalSaturation`, `MaskValue`), not their `FlatName` aliases (`Saturation`, `Value`) — tried the
FlatName first, got a silent "not a field of Correction" warning and dropped value for
`Saturation` specifically (it collides with the top-level global Saturation tag's own FlatName),
while `Amount`/`Active`/`Exposure2012` FlatNames happened to work with no conflict — inconsistent
enough that using real struct member names throughout is the only version that's actually
reliable, not just usually-working. An empty `radial_filters` list writes `=` (no value), which
**deletes** the tag rather than leaving stale regions from a previous save stranded in the file —
confirmed empirically (write struct → clear with empty value → re-read finds nothing).

**Reading requires exiftool's `-struct` mode**, unlike every other tag this app reads. The
*flattened* (non-struct) tag names for this specific structure (e.g. `CircGradBasedCorrExposure2012`)
collapse a multi-region list down to just the **last** region's value — confirmed by writing two
regions and reading both ways: `-struct` correctly returns a 2-element list, the flattened form
silently returns only the second region's numbers with no error. XMP.pm's own source explains why:
`List => 0` is deliberately set on these flattened tags, "because these are nested in lists and
the flattened tags can't do justice to this complex structure." So `read_radial_filters()` passes
`params=["-struct"]` to `ExifToolHelper.get_tags()` — the one place in this codebase that does,
everywhere else relies on the plain flattened default. Malformed/foreign mask entries (a real
Lightroom gradient or brush mask, say) are skipped per-region rather than aborting the whole read;
numeric fields are clamped to the same ranges the UI's own drag-handle math and sliders enforce.

**What's verified versus best-effort, stated plainly because this project's culture is to say so
rather than let an assumption pass as fact:** structural round-trip through this app itself (write
→ read → identical `radial_filters` list, multi-region included) and through exiftool's own
`-struct` reader are both directly tested and confirmed. What is **not** verified — no Lightroom
install exists in this environment to check against — is visual fidelity in real Lightroom/ACR:
whether `Angle` should be degrees or radians, and the precise visual meaning of `Midpoint`/
`Roundness` (always written as Adobe's own apparent defaults, 50 and 0, since this app has no
matching UI controls for them). If a region opened in real Lightroom looks positioned/rotated
differently than in DNGForge, start there — same class of unverified-until-tested risk already
flagged for Profile/Curve/Tone elsewhere in this file, not a new kind of gap.

A real Lightroom-saved DNG (`DSC_6751.dng`, user-provided, not part of the tracked test set) was
used mid-investigation for an unrelated bug (see "Save" section's `JpgFromRaw` writeup) and
happened to confirm one relevant fact about this feature too: that file's own `crs:*` local
adjustment is a **`GradientBasedCorrections`** (the linear/graduated filter, roadmap item 6, not
built yet at the time) rather than a circular one, so DNGForge at that point correctly left it
alone on read rather than misreading it as a radial region — every other control from that same
file restored correctly, only the graduated filter didn't yet, exactly as expected given
`read_radial_filters()` only ever looks for `CircularGradientBasedCorrections`. Once the graduated
filter itself was built (see below), re-reading that same file correctly surfaced it: `zero_x/y` =
0.496446/0.193171, `full_x/y` = 0.493085/0.945426, `exposure` = -0.186 — an exact match against the
raw `-struct` dump of that file taken during the investigation, not just "a plausible-looking
region."

### Graduated filter persistence (`build_gradient_xmp_value()`, `read_gradient_filters()`)

Writes/reads `crs:GradientBasedCorrections` — same `Correction`/`CorrectionMasks` struct shape and
same write mechanism (JSON-like struct-literal syntax) as the radial filter's
`CircularGradientBasedCorrections`, see "Radial filter persistence" above for that mechanics. The
one real difference is the mask itself: `Mask/Gradient` (vs `Mask/CircularGradient`) uses just four
geometry fields — `ZeroX`/`ZeroY`/`FullX`/`FullY` — no `Angle`/`Feather`/`Midpoint`/`Roundness`/
`SizeX`/`SizeY`/`Flipped` at all.

**This is the one place in the project where the schema is verified against real Lightroom output,
not just exiftool's source** — `DSC_6751.dng` (see above) has a genuine `Mask/Gradient` entry from
Lightroom 6.8, and its exact field set matches what's implemented here field-for-field. Unlike the
radial filter's `Angle`/`Midpoint`/`Roundness` (still unverified — no Lightroom install to check
against, see that section), there's no equivalent open question here: the two-point model **is**
confirmed to be Adobe's actual representation, not a simplification.

**No `Flipped`/invert field exists in this mask** (confirmed on the same real file) — real
Lightroom doesn't need one, since dragging a gradient the other direction already swaps which side
is affected. `build_gradient_xmp_value()` reconciles this with the app's own Invert checkbox by
swapping the Zero/Full points on write when `invert` is set, rather than inventing a nonstandard
field: the rendered mask ends up identical either way (verified: rendered a filter with
`invert=True` before saving, reopened the saved file — which reads back `invert=False` with
swapped coordinates — and rendered again; mean pixel difference between the two renders was
exactly 0.0). `read_gradient_filters()` always sets `invert=False` accordingly — there's nothing
left to invert once the geometry already encodes the effect.

Multi-region round-trip verified the same way as the radial filter: two regions with distinct
values (including one with `invert=True`), saved, reopened in a second independent `DNGForge`
instance, every field matched (modulo the by-design `invert=False` after the geometry swap).

### Spot Removal (placement and UI — see "Spot Removal persistence" below for the XMP schema)

Roadmap item 13 — flagged from the start as "a materially different kind of feature from anything
built so far," and it is: every other local tool (radial/gradient) adjusts *tone*, this one copies
*pixels* from one place to another. Two circles per region — destination (the blemish) and source
(clean texture to copy from) — both a single shared `radius` (a fraction of image *width*, a true
circle, unlike the radial filter's independent `rx`/`ry`), matching Lightroom's own circular spot
tool. Written to `-XMP-crs:RetouchAreas` (see "Spot Removal persistence" note below) for real
Lightroom to read — **Adobe DNG Converter itself ignores this tag when rendering**, exactly like
the radial/graduated filters (see "Local adjustments are not rendered by Adobe DNG Converter"
below). DNGForge's own preview/save pixels for a spot come from `_apply_spot_heal()`/
`_apply_spot_clone()` — `cv2.seamlessClone()`/a feathered copy — applied directly on top of
whatever Adobe already rendered, not from Adobe interpreting `RetouchAreas` itself.

**The `RetouchAreas` schema itself remains unverified against a real Lightroom-saved retouch
region** (unlike the radial/gradient/crop schemas, each checked against a genuine `DSC_6751.dng`
value) — the odd `SourceX`/`OffsetY` naming asymmetry (one field reads as an absolute coordinate,
the other as a delta) is inferred from field names and general Adobe-schema conventions, not
confirmed fact. This no longer affects what DNGForge itself shows (see above — rendering doesn't
route through this tag at all anymore), but it's still worth checking before assuming a
DNGForge-saved spot will look right if the file is later opened in real Lightroom.

**Two real methods**, both real algorithms actually applied by this app itself again (see "Local
adjustments are not rendered by Adobe DNG Converter" below for why that's back):
- **Heal** (default) — genuine Poisson blending via `cv2.seamlessClone(..., cv2.NORMAL_CLONE)`:
  adapts the copied texture to the destination's local color/lighting, the same class of algorithm
  Lightroom's own Heal mode uses. Falls back to Clone when the source/destination circle doesn't
  fit entirely within the frame (`seamlessClone` requires its mask region fully interior).
- **Clone** — a plain feathered alpha copy, no color adaptation. Exists because Heal's automatic
  blending can pick the wrong tone when copying across a hard edge (e.g. a spot that straddles two
  very different-colored regions), the same reason Lightroom keeps both modes rather than just Heal.

**CONFIRMED weak-effect report, fixed: both methods looked too subtle even at Opacity=100%.**
Reported directly by the user right after confirming the zoom-detail fix worked: "l'effetto è
troppo poco marcato anche al 100%... si potrebbe aumentare l'intensità... magari anche molto più
marcato, poi regolo io l'intensità." Two changes:
1. **`spot_opacity_slider`'s range extended from 0-100 to 0-200.** Both `_apply_spot_heal()`/
   `_apply_spot_clone()` were already a plain linear blend between the original pixels and the
   healed/cloned result, so Opacity > 100% is a well-defined extrapolation *past* that result
   (pushed further in the same direction, clamped to a valid pixel range only at the very end) —
   not an error case, just headroom the slider hadn't been offering.
2. **`_apply_spot_clone()`'s falloff shape fixed** — the original was a plain center-to-edge cone
   (`mask = 1-dist` across the *entire* radius), so only the exact center pixel ever reached full
   strength; every other pixel, including ones well inside the circle, was already partially
   diluted back toward the original even at Opacity=100%. Not how a real retouching brush reads (a
   solid disc with a soft edge only near the boundary). Fixed with a two-zone falloff
   (`_SPOT_CLONE_CORE_FRAC = 0.6`): the inner 60% of the radius is held at full strength,
   feathering only in the outer ring. `_apply_spot_heal()`'s own Poisson-blend mask (a plain solid
   disc, no feathering — `seamlessClone` computes its own correct edge blending internally) didn't
   need the same fix, only the final opacity-amplification extension.

**Verified**: sampled a Clone result at several fractions of the radius from center — under the
old cone falloff, even a point at 30% of the way to the edge was visibly diluted; under the fix,
everything through 60% of the radius reads as a full-strength copy, feathering only beyond that.
Confirmed opacity > 100% is a real, working extrapolation (not silently clamped) by comparing a
100%- vs 200%-opacity Clone on synthetic data with a deliberately extreme color contrast (a red
"blemish" cloned from a green source) — the 200% result pushed measurably further past the plain
100% copy, not just repeating it. Also confirmed visually through the real Adobe render pipeline
(a Heal spot at Opacity=150 rendered through `DNGForge` end-to-end): a large, unmistakably bright
patch now visible where the source texture was pasted in, nothing like the barely-perceptible
result the un-amplified version produced.

**Auto-placed source, not a content-aware search.** Lightroom auto-suggests a source patch by
searching for similar nearby texture; this app doesn't attempt that (a real search would be a
project of its own) — instead `_place_spot()` offsets the source by a fixed `2.5×radius` to one
side (flipping to the other side if that would push it off-frame), and the user drags the source
circle wherever actually looks right. Simplified, but honest about being simplified, rather than a
fake "smart" placement that's really just the same fixed offset dressed up.

**Interaction**: mirrors the radial/gradient filters' established pattern (Place/Remove buttons,
multi-region list `self.spots`/`self.spot_selected`, "Show handles / edit" checkbox) with one
UX difference driven by the tool's own shape — dragging the destination or source circle is done
by clicking *anywhere in that circle's body*, not a tiny center handle (`ImageLabel._move_spot()`);
only the resize grip (on the destination circle's edge) is a small precise target, since requiring
a pixel-perfect click on a dot to move a potentially large circle would be poor UX. Verified with a
rendered screenshot: a selected spot shows a solid destination circle, dashed source circle,
connecting line, and resize handle; an unselected second spot renders as a thin dim outline,
consistent with — and not visually conflicting with — the radial/gradient filters' own overlay
conventions on the same widget.

**Verified**: placing a spot via the click handler auto-offsets the source correctly and clamps it
within frame bounds; placing, undoing, and redoing a spot round-trips correctly through the
Undo/Redo system (see that section above) — `spots` is included in `_capture_edit_state()`/
`_apply_edit_state()` like every other editable control; `spots` round-trips through
`build_spot_xmp_value()`/`read_spot_removals()` (structural round-trip through this app itself, and
through exiftool's own `-struct` reader — see "Spot Removal persistence" below). **Not verified**:
whether Adobe DNG Converter actually renders a visible heal/clone effect from this app's own
`RetouchAreas` output — see the warning above.

### Spot Removal persistence (`build_spot_xmp_value()`, `read_spot_removals()`)

Same struct-literal write mechanism as the radial/gradient filters (see "Radial filter
persistence" below), `-XMP-crs:RetouchAreas` instead of `CircularGradientBasedCorrections`/
`GradientBasedCorrections`. Field names pulled from exiftool's own `XMP.pm` source, not guessed —
but, unlike those two schemas, never checked against a real Lightroom-saved file (no such sample
exists in this project). See the warning above: this is no longer just a documentation gap, it's
the one place a schema mistake would now be invisible in the app's own preview too.

### Crop & Straighten (`max_inscribed_rect()`, `clamp_crop_to_rotation()`)

Built as one combined tool, per the roadmap's own suggestion when item 9 was written ("likely
paired with crop, e.g. a rotation control that also auto-adjusts the crop rectangle to stay
within the rotated image bounds") — that's exactly the shape this took. Only ever touches the
*rendered* image, never `self.raw` or the DNG's own pixel data — confirmed explicitly with the
user before starting ("per crop/raddrizzamento mi riferisco sempre all'anteprima jpg mai al file
dng di partenza"), same non-destructive convention every other adjustment in this app follows: the
crop rectangle is written as `-XMP-crs:HasCrop`/`Crop*` tags (see "Persisted to XMP" below) and
Adobe DNG Converter applies the actual rotate-then-crop, not this app's own pixel code (which used
to do this locally, `apply_crop_straighten()`, `cv2.warpAffine()` + array slicing — gone along
with the rest of the local render pipeline, see "Adobe DNG Converter is the sole renderer" above).

**Geometry**: `self.crop` is `None` (no crop) or `{"angle", "left", "top", "right", "bottom"}` —
angle in degrees, the rest are fractions (0..1) of the *rotated-in-place* canvas, matching Adobe's
own `CropAngle`/`CropTop` etc. convention exactly (see "Persisted to XMP" below), so no unit
conversion happens anywhere in this pipeline. Straighten necessarily introduces black wedges at
the corners once the frame is no longer axis-aligned; `max_inscribed_rect()` is the standard
closed-form solution for the largest axis-aligned rectangle that avoids them (verified: at 30° on
a 4288×2848 frame, the max crop shrinks to 1644×2848 — matches hand-computed geometry, not just "a
formula that runs without erroring"). `clamp_crop_to_rotation()` re-centers/shrinks the *current*
crop rectangle to fit whenever Rotation changes — it only ever shrinks a rectangle that no longer
fits, never grows one back that the user deliberately made smaller. Both are pure geometry helpers
for the UI's own drag-handle math and are unaffected by the move to Adobe rendering.

**UI/rendering split — "guide" vs "committed" view, same idea as Lightroom's own Crop tool**: while
`_crop_editing` is true (the **Show crop guide / edit** checkbox), the Adobe-rendered preview shows
the *full* straightened frame — via `_effective_crop_for_render()`, consumed by
`_build_xmp_crs_args()` (not `self.crop` directly) so the crop tags sent to Adobe reflect "no crop"
while editing — with `ImageLabel.overlay_crop` drawn on top (darkened outside the rectangle,
rule-of-thirds grid, 4 corner drag handles + drag-the-body-to-move). Turning that checkbox off
switches the *live preview itself* to the actually-cropped frame, unlike the radial/gradient
filters' own "Show handles" toggle (which only hides handles — the rendered *effect* stays
identical either way, since those tools don't change framing). **Saving always applies the real
crop** regardless of whether the guide happens to be showing at that moment
(`save_to_dng()`/`export_preview_jpeg()` call `_build_all_edit_xmp_args(for_save=True)`), so
leaving the guide open by accident can't silently save the wrong (full, uncropped) frame. See
"Background Adobe DNG Converter render pipeline" above for the companion fix that makes toggling
the guide actually trigger a fresh render (the dedup key covering `_crop_editing` too).

Corner-drag math is deliberately simpler than the radial filter's ellipse handles: no rotation
math is needed for the overlay itself, since by the time the guide is shown the underlying pixmap
is *already* rendered straightened — the crop rectangle overlay is just a plain axis-aligned
rectangle in that already-rotated pixmap's own screen coordinates.

**Aspect ratio presets** (`CROP_ASPECT_RATIOS`, added right after the base tool at the user's
request — "il crop dovrebbe anche mostrare i rapporti di aspetto tipici da scegliere"): a combo
box (Freeform/Original/1:1/4:3/3:2/16:9/5:4) above the Rotation slider. Ratios are expressed in
*output pixels*; converting to the fraction-space ratio `_move_crop()`/`_apply_crop_aspect_ratio()`
actually work with needs the raw image's own W/H (`frac_ratio = pixel_ratio * h / w`), since a
fraction of width and a fraction of height aren't the same number of pixels unless the image is
square. Picking a preset immediately reshapes the current rectangle (centered, shrunk to fit its
own bounding box) to that ratio; corner-dragging afterward is constrained to keep it — verified
both the reshape (1:1 on a 4288×2288 raw produces an exact 2848×2848 px crop; 4:3 produces
2848×2136, ratio 1.3333) and the constrained-drag math (dragging the `br` handle far right and only
slightly down under a 4:3 constraint still produces exactly a 4:3 result, 3139×2354 px, not a
free-form rectangle). Straightening re-applies the active ratio after `clamp_crop_to_rotation()`
shrinks the rectangle, since clamping the two axes independently can otherwise throw the ratio off.
The chosen *preset* isn't itself persisted (see below) — only the resulting geometry is, so
reopening a file with a saved crop always shows "Freeform" in the combo even if that crop happens
to be exactly 3:2.

**Persisted to XMP** (`-XMP-crs:HasCrop`/`CropTop`/`CropLeft`/`CropBottom`/`CropRight`/`CropAngle`,
added right after the base tool at the user's request — "ma anche crop & raddrizza deve persistere
in xmp"). Unlike the radial/gradient filters' nested `Correction`/`CorrectionMasks` struct, these
are **flat** tags — confirmed against exiftool's own `XMP.pm` source *and* a genuine Lightroom-saved
crop in `DSC_6751.dng` (`HasCrop=True, CropTop=0.20565, CropLeft=0.125304, CropBottom=0.938289,
CropRight=0.857943, CropAngle=0`) — real ground truth, and it happens to use the exact same
fraction/degree convention `self.crop` already used internally, so no unit conversion was needed
on either side once the schema was confirmed. `_build_xmp_crs_args()` always writes `HasCrop`
(`True` with the four boundaries + angle, or `False` alone) so a crop from a *previous* save gets
properly cleared if the user removes it, rather than left stranded — same "always write, even the
absent/off state" pattern the radial/gradient filters and White Balance mode already use.
`read_crop()` returns `None` when `HasCrop` is false/absent, distinguishing "no crop" from "a crop
that happens to be full-frame."

`_restore_crop_ui()` syncs the panel to a restored crop with `_crop_editing` left `False` —
reopening a file shows the *committed* cropped result (matching what `save_to_dng()` would
actually produce), not an editing guide the user never asked to see, same reasoning
`_select_radial_filter(-1)`/`_select_gradient_filter(-1)` already use for those tools.

Verified against the old local-render pipeline (before the Adobe migration): with no crop, the
render output was the full frame; enabling crop and setting a rectangle left the *editing* preview
at full size (guide mode); disabling the guide cropped the preview to the expected pixel
dimensions (computed from the rectangle fractions, not hardcoded); `for_save=True` cropped
correctly even if the guide was re-enabled afterward; rotating clamped the rectangle to
`max_inscribed_rect()`'s own bound — all of that geometry logic is unchanged by the Adobe
migration, only which code actually rasterizes the crop changed. Also checked with a rendered
screenshot (`ImageLabel.grab()`) that the guide draws correctly — darkened surround, grid, and
handles all visible and not overlapping the frame's own picture content confusingly. Full
round-trip verified end to end: enabled crop, applied a 3:2 aspect preset, rotated 3°, saved,
reopened in a second independent `DNGForge` instance — every field (`angle`/`left`/`top`/`right`/
`bottom`) matched to within float rounding, and the aspect combo correctly came back showing
"Freeform." Re-verified this session (Adobe-only pipeline): toggling the crop guide checkbox on a
loaded file correctly triggers a fresh background Adobe render (previously a latent gap — see
"Background Adobe DNG Converter render pipeline" above).

### Debug info dialogs — XMP-crs vs. EXIF (`_show_xmp_info()`, `_show_exif_info()`, `_show_tag_info_dialog()`)

User-requested debugging tool, right after the `ColorTemperature=13200` bug above was found and
fixed: "puoi implementare un tasto info (con una I) che mi mostra tutti i campi del file xmp
interno al dng? così anche io posso aiutare col debug? una cosa del genere c'è in rethinkraw." Two
small buttons in the zoom toolbar (above the image, right-aligned) — **"ⓘ XMP"** and **"ⓘ EXIF"**
— each open a modal dialog with a read-only, monospace, copy-to-clipboard-able exiftool dump,
scoped to a different tag group, of the file actually saved *on disk* right now — deliberately the
file's real current state, not this app's own in-memory edit state, so it reflects reality
regardless of unsaved changes. Both share `_show_tag_info_dialog(title, exiftool_args,
empty_message)` for the actual dialog plumbing; only the exiftool group scope differs.

**Originally one combined button/dialog**, split into two right after the user — investigating a
*different* file that turned out to have no XMP-crs data at all — asked why a freshly-downloaded,
never-edited DNG has "nessun tag xmp interno", and then what to call "questi altri tag" (the
camera's own capture/calibration data: `ColorMatrix1/2`, `CalibrationIlluminant1/2`,
`AsShotNeutral`, `Make`/`Model`/`Lens`, ...). The answer is `EXIF` — confirmed directly from real
exiftool output, every one of those tags prints under the `[EXIF]` group prefix, not an invented
label. **XMP-crs is the editing "recipe"** (only exists once some tool — Lightroom, Camera Raw,
RethinkRAW, or DNGForge itself — actually develops and saves settings); **EXIF is the camera's own
capture/calibration data**, present on every DNG from the moment of RAW→DNG conversion, with
nothing to do with editing. One combined dump was conflating two genuinely different questions
("what did I edit" vs. "what does the camera say about this shot") into a single undifferentiated
wall of text.

**CONFIRMED BUG, caught before this ever shipped**: `-XMP-crs:all` alone silently omits some
tags — `WhiteBalance` and `ColorTemperature` specifically, confirmed missing with or without
`-struct`, and regardless of whether the wildcard is scoped to the crs group directly or left at
the broader `-XMP:all` (group-scoping the wildcard doesn't fix it — tried both). Found by testing
against `RAW_LEICA_M8.DNG`, the exact file the `Slider` bug above was diagnosed on — expected this
dialog to show `ColorTemperature 13200` and it silently didn't. Almost certainly because exiftool
marks both `Avoid => 1` in its own crs table (ambiguous short names that collide with same-named
tags in other groups) and skips Avoid-flagged tags from *any* wildcard group request unless named
explicitly — even though a direct `-XMP-crs:ColorTemperature` request (what every other read in
this app already does, one tag at a time) returns it fine. A debug tool whose whole purpose is
"show me everything" silently hiding the exact tag that caused the bug it was built to help
diagnose would have been a real problem, not a cosmetic one. Fixed by also explicitly requesting
every tag in `XMP_CRS_TAGS` alongside the `-XMP-crs:all` wildcard in the same exiftool call —
guarantees everything this app itself reads/writes is always visible regardless of what the
wildcard alone would have shown. `-EXIF:all` needed no equivalent workaround — verified directly
that `ColorMatrix1`, `AsShotNeutral`, etc. all come through the plain wildcard with no omissions.

**Verified**: the combined XMP-crs query recovers `WhiteBalance`/`ColorTemperature` both against
the file as originally found (`ColorTemperature=13200`) and after writing a fresh value
(`ColorTemperature=6500`) directly via exiftool — the recovery isn't a fluke tied to one specific
saved number. Confirmed the two dialogs stay cleanly scoped to their own group (the XMP-crs dump
contains zero `[EXIF]`-prefixed lines and vice versa) and that a genuinely virgin copy of
`RAW_LEICA_M8.DNG` (freshly re-downloaded by the user, confirming the original `13200K` had been
this app's own earlier test data, not a real bug — see the `Slider` section above) correctly shows
0 XMP-crs tags while still showing a full ~64-line EXIF dump.

### Metadata panel (`read_metadata()`, `METADATA_FIELDS`)

A read-only panel, `METADATA_FIELDS` holding the exact field list/order the user specified for
roadmap item 10. Populated by `_update_metadata_panel()`, called from `load_dng()` after every
other per-file state (camera profile, XMP restore, radial/gradient filters) so it always reflects
whatever file is currently open.

**Uses print-conversion, unlike every other exiftool read in this app.** Every other call site
builds `ExifToolHelper` with the app's usual `common_args=["-G", "-n"]` default (`-n` = disable
print conversion, for machine-parsable values — see `load_dng_camera_profile()`,
`read_xmp_crs_state()`, etc.). This panel wants the opposite: human-readable strings for direct
display ("1/60", "On, Return detected", "Program AE", "50.0 mm (35 mm equivalent: 75.0 mm)"), so
it constructs its own `ExifToolHelper(common_args=["-G"])` — group names kept, print conversion
left on. Verified against the real test file's actual tag groups (`EXIF:ExposureTime`,
`Composite:FocalLength35efl`, `EXIF:LensModel`, etc. — checked with a live exiftool call first,
not assumed) rather than guessing at group prefixes the way a stale assumption could silently
return `None` for every field.

**"Dimensions" is judged against the caller's own already-resolved raw dimensions**
(`self.raw.sizes.width/height`), not by re-reading an "ImageWidth" EXIF tag — this DNG's own
structure can have several same-named `ImageWidth`/`ImageHeight` tags across different IFDs at
different sizes (see the `JpgFromRaw` writeup under "Save" above), so trusting an unqualified
`-ImageWidth` read here would risk the exact ambiguity that bug already taught this project to
avoid.

**CONFIRMED BUG, fixed: "Cropped" checked the wrong tag entirely.** The first version compared
`EXIF:DefaultCropSize` against the raw dimensions — but `DefaultCropSize` is a DNG-spec
*sensor-level* tag (the valid-pixel boundary within the raw buffer), essentially unrelated to
whether a human actually cropped the shot in an editor. It reported "No" even on `DSC_6751.dng`,
which has a real, substantial Lightroom crop (confirmed by the user opening the file and asking
why the panel disagreed with what `XMP-crs:HasCrop`/`Crop & Straighten`'s own read already showed).
Fixed to check `XMP-crs:HasCrop` instead — the exact same real edit-state tag `read_crop()` uses
for Crop & Straighten (see that section above), so the two can no longer disagree with each other.
Verified against both directions: `DSC_6751.dng` (real crop) now reports "Yes", the pristine test
file (never cropped by anyone) still correctly reports "No".

### Gallery filmstrip (`FilmstripWidget`, `ThumbnailScanThread`, `DNGForge.open_folder()`)

User-requested (roadmap item 26): "sarebbe carino implementare una sezione libreria, in cui carico
le immagini della cartella dei dng che voglio processare... non serve una vera libreria permanente
visto che di solito processo la cartella e poi sposto tutto in un hard disk a parte con le foto
organizzate per data. Basterebbe una sezione galleria anteprime temporanea, usa e getta." —
explicitly **not** a persistent catalog/database: **File → Apri cartella…** (Ctrl+Shift+O) scans
a folder for `.dng` files and populates a thumbnail strip; nothing about it is saved between
folders or sessions, `FilmstripWidget.set_photos()` wipes and rebuilds from scratch every time.
Confirmed via `AskUserQuestion` before building: a filmstrip docked at the bottom of the window,
always visible alongside the develop panel (Lightroom's own placement — chosen over a full
separate "Library module"-style view to switch to, since the user's actual workflow is picking
the next photo in a folder without losing the editor), and a dedicated **File → Apri cartella…**
entry (over drag-and-drop) to populate it.

**Layout**: the filmstrip spans the *full window width* at the bottom — below both the image and
the develop panel, not just under the image — so it stays visible regardless of which panel
section is scrolled into view on the right. Required wrapping the pre-existing
`splitter` (image + controls) in one more outer `QVBoxLayout` (`splitter` on top, stretch factor
1; `self.filmstrip` below, fixed height) as the new central widget, rather than adding the
filmstrip as a third `QSplitter` pane — a `QSplitter` pane would let the user accidentally resize
the filmstrip to near-nothing or crowd out the image, neither of which makes sense for a strip of
fixed-size thumbnails. Hidden entirely (`FilmstripWidget.hide()`) until a folder with at least one
`.dng` is actually opened, so it doesn't sit there as a bare empty bar for users who never use
this feature.

**Thumbnail extraction is one exiftool process for the *entire* folder, not one per file** —
`ThumbnailScanThread` uses exiftool's own `-w` (write) option
(`-b -PreviewImage -w <scratch>/%f_thumb.jpg <path1> <path2> ...`) to dump every file's preview
straight to its own JPEG in a single spawn, the same "batch instead of N spawns" discipline
`read_combined_edit_tags()` already established for file-load reads (~440-470ms *per spawn*,
almost entirely process-startup overhead — see "Performance: consolidated exiftool reads on
load" above; for a folder of dozens of shots that difference is the entire feature feeling
instant vs. sluggish). **Uses `PreviewImage`, not `ThumbnailImage`** — confirmed directly that a
DNG converted by Adobe DNG Converter (which every file this app can open already has to be, see
"Adobe DNG Converter is the sole renderer") generally has no separate small IFD0 thumbnail at
all, only `PreviewImage`/`JpgFromRaw` at real preview resolution: `RAW_NIKON_D90.dng` and
`DSC_6751_prova.dng` both have zero `ThumbnailImage` tag. A source file with *neither* tag at all
(`RAW_LEICA_M8.DNG`, an unconverted raw-samples test file predating this app's own Adobe DNG
Converter requirement, not a file DNGForge itself could have produced) simply gets no output —
`FilmstripWidget.set_thumbnail()` falls back to showing the bare filename as button text instead
of a blank icon for that case, rather than treating it as an error.

**User asked directly whether this specific file actually has *some* small embedded preview that
just isn't being found** — it does, but not in a form this extraction can use, worth recording
precisely rather than leaving "no preview" as the assumed explanation. `RAW_LEICA_M8.DNG` has a
genuine 320×240 `SubfileType=Reduced-resolution image` in IFD0 — but stored as
**uncompressed raw RGB strip data** (`Compression=Uncompressed`, `StripOffsets`/`StripByteCounts`),
not a JPEG blob, the older/plainer DNG-spec convention that predates Adobe's now-universal
practice of embedding a JPEG-compressed `PreviewImage`/`JpgFromRaw`. `exiftool -b` only pulls out
already-JPEG-compressed binary tags; extracting this would mean decoding raw TIFF strip data by
hand, not a simple binary dump. Decided not worth building: every file this app can actually
*edit* already has to have gone through Adobe DNG Converter (see "Adobe DNG Converter is the sole
renderer"), which always embeds a proper compressed `PreviewImage`/`JpgFromRaw` — this legacy
strip-based case only affects `RAW_LEICA_M8.DNG` specifically (an f-spot/raw-samples reference
file, not a file the user's own real workflow ever produces), so the existing filename-text
fallback was kept as-is rather than adding raw-strip decoding for a case that can't occur on a
real file this app would ever open.

**Threading and cleanup**: matches the render pipeline's own established generation-counter
pattern (`self._gallery_generation`) so a second `open_folder()` call while an earlier scan is
still running discards the earlier scan's now-stale results instead of letting them land on the
new folder's filmstrip — same reasoning as `DngConverterRenderThread`'s own `generation` field.
**Each scan's scratch directory is tracked in a dict keyed by generation**
(`self._thumbnail_scan_dirs`), not a single shared attribute — a single shared attribute was the
first version's design and had a real bug: overwriting it for a second scan before the first
scan's own `finished_scan` signal had fired would lose track of the first scan's directory
entirely (leaked, never cleaned up). Caught before shipping by reasoning through the
overlapping-scans case directly, not by hitting it in testing. Individual thumbnail files are
deleted by the **main-thread** signal handler (`_on_thumbnail_ready()`) right after decoding each
one into a `QPixmap` — not by the worker thread itself — because Qt's cross-thread signal
delivery is asynchronous: deleting the scratch directory from inside `ThumbnailScanThread.run()`
right after emitting its last signal could race the main thread's handler for an
earlier-emitted-but-not-yet-processed file, the exact same class of ordering hazard already
documented for `DngConverterRenderThread`'s per-instance filename tokens.

**Loose coupling with the existing editor**: clicking a thumbnail (`FilmstripWidget.photoActivated`)
is wired directly to the pre-existing `self.load_dng` — no new loading logic, no unsaved-changes
prompt (matching **File → Open DNG…**'s own existing behavior, which also never prompts; only
`revert_to_saved()` does, since that specifically has no undo path). `load_dng()` gained one line,
`self.filmstrip.set_current(path)`, right after `self.raw_path = path` — a safe no-op when `path`
isn't part of the currently-open folder (e.g. a file opened via **File → Open DNG…** directly
rather than picked from the gallery), so this didn't need any conditional guard.

**Verified end-to-end** through a real `DNGForge` instance (`QT_QPA_PLATFORM=offscreen`, mocked
folder picker, a disposable scratch folder containing `RAW_NIKON_D90.dng`, `DSC_6751_prova.dng`,
`RAW_LEICA_M8.DNG`, and one non-DNG file): `open_folder()` correctly finds only the 3 `.dng`
files (the `.txt` file excluded); the async scan completes and correctly cleans up its scratch
directory; the Nikon and `DSC_6751_prova` buttons end up with real extracted-thumbnail icons,
while the Leica button (no embedded preview) correctly falls back to filename text instead of a
blank icon; clicking a thumbnail loads that exact file into the main editor (`raw_path` updates,
a real Adobe DNG Converter render completes) and correctly re-highlights that thumbnail as the
current one (`set_current()`).

### Copy/Paste Settings (`copy_settings()`, `paste_settings()`)

User-requested (roadmap item 27): "una funzione stile lightroom per copiare ed incollare le
impostazioni da una foto all'altra... molto utile per scatti simili, senza dover ripartire ogni
volta da zero." Confirmed via `AskUserQuestion` before building, same as the gallery filmstrip:
paste targets only the single photo currently open (not a multi-select-in-filmstrip "Sync
Settings" operation across many files at once — a materially bigger feature, since applying to
photos *other* than the one currently open would mean a full render-and-save pass per target
file, not just an in-memory widget restore), and copies the entire edit state in one shot rather
than a Lightroom-style per-category checklist (Basic only, exclude Crop, etc.).

**Needed zero new state-serialization logic** — `copy_settings()` is a one-line wrapper around
`_capture_edit_state()`, the exact same function Undo/Redo's own history already builds from, and
`paste_settings()` is a one-line wrapper around `_apply_edit_state()`, Undo/Redo's own restore
path. `self._settings_clipboard` just holds whatever `_capture_edit_state()` returned, in memory,
for the rest of the session — no disk persistence, matching the same "usa e getta" spirit as the
gallery filmstrip (a new app launch starts with an empty clipboard).

**Undo integration needed no paste-specific code at all.** `_apply_edit_state()` already ends
with a plain `self._schedule_update()` call once its own `_restoring_state` guard clears — the
same debounced `_render_preview()` → `_maybe_push_undo_state()` path a normal slider drag
triggers, which compares the just-applied state against `self._current_edit_state` and pushes the
*old* state onto the undo stack when they differ. A paste is, from that path's point of view,
indistinguishable from any other edit — so Ctrl+Z after a paste correctly reverts it, for free,
without `paste_settings()` touching `_undo_stack`/`_redo_stack` itself the way `undo()`/`redo()`
do for history navigation.

**Clipboard is immune to aliasing** — both `_capture_edit_state()` (on copy) and
`_apply_edit_state()` (on paste) already `copy.deepcopy()` the mutable fields
(`radial_filters`/`gradient_filters`/`spots`/`crop`), a guarantee Undo/Redo already needed for its
own correctness (see that section above). Reusing both functions verbatim means Copy/Paste
inherits that guarantee automatically: mutating the pasted-into photo's own filters afterward
(e.g. placing a new radial filter) can never reach back and corrupt what's sitting in the
clipboard, confirmed directly rather than assumed.

`self.paste_settings_action` (the Edit-menu `QAction`) starts disabled and is only enabled once
`copy_settings()` actually runs — no menu item that does nothing on a fresh launch.

**Verified end-to-end** through two real, independently-loaded `DNGForge` instances/photos (not
just one window edited twice, since the real use case is copying *across different files*):
copied a set of distinctive values (Exposure, Contrast, Saturation, a Split Toning hue, Black &
White) from photo A, loaded a *different* photo B, confirmed B's own sliders read their own
values (not A's) before pasting, pasted, and confirmed every one of A's copied values now showed
correctly on B; settled the debounced render and confirmed the paste was recorded as a new
undoable change with zero paste-specific bookkeeping; confirmed `undo()` correctly reverted B back
to its pre-paste exposure; and confirmed mutating B's `radial_filters` after a second paste never
touched the clipboard's own stored (empty) list.

### Close confirmation (`closeEvent()`)

**CONFIRMED gap, fixed**: closing the window (the title-bar × button, Alt+F4, or any other path
that reaches `closeEvent()`) previously never asked anything at all — it silently discarded
whatever in-progress edit was on screen and tore the app down. Reported directly by the user as
feeling unpolished ("il tasto di uscita/chiusura dovrebbe avere un controllo di conferma e stato
del file classico") — the standard behavior of essentially every other editor.

**First version scoped this too narrowly**: only prompted when there were genuinely unsaved
changes, silently closing with no dialog at all otherwise (including when no file was even open)
— a reasonable-sounding "don't bother the user when there's nothing to lose" design, but the
user pushed back directly after trying it ("se clicco sulla x in assenza di modifiche il
programma non mi chiede conferma. mi sembra poco professionale così") — they wanted a
confirmation shown *every* time, not conditionally. Fixed: `closeEvent()` now always shows a
`QMessageBox.question()`, but *which* one depends on file state — a plain "Uscire da DNGForge?"
Yes/No when there's nothing unsaved (including no file open at all), or the fuller Save/Don't
Save/Cancel when there is. Both the "always confirm" behavior and the "but the content should
still reflect file state" nuance are directly what the user's own original phrasing asked for
("un controllo di conferma **e** stato del file") — this fix is what actually satisfies it, the
first version only got half of it.

**Dirty-state detection reuses `_capture_edit_state()`** (the same function Copy/Paste Settings
and Undo/Redo both already build on) rather than tracking a separate boolean flag that could
drift out of sync with what those other features already consider "changed."
`self._saved_edit_state` — a snapshot of "what's on disk right now" — is set in two places: at
the end of `load_dng()` (once every control has finished restoring from the file's own saved
state, or Default for a virgin file) and at the end of a *successful* `save_to_dng()` (captured
only after the write actually succeeds, so a failed save correctly leaves the file still
considered dirty). `closeEvent()` simply compares a fresh `_capture_edit_state()` call against
this baseline.

**`save_to_dng()` gained a real return value** (`True`/`False`) purely for this — previously a
`None`-returning function whose callers only ever cared about its side effects. `closeEvent()`'s
Save path checks it and calls `event.ignore()` on failure, so choosing "Save" from the
close-confirmation dialog can't silently close (and lose) the file out from under a save that
`save_to_dng()`'s own `QMessageBox.critical` has already reported as failed.

**Verified** (`QT_QPA_PLATFORM=offscreen`, `QMessageBox.question` mocked to return each button in
turn, a real `QCloseEvent`, disposable copies of `RAW_NIKON_D90.dng`): no file open at all still
prompts (the plain Yes/No wording) and answering No correctly keeps the app running
(`event.isAccepted()` false); a cleanly-loaded file with no edits still prompts with the same
plain wording (not silently closing, the exact gap the user's follow-up caught) and Yes closes;
a dirtied file gets the distinct Save/Don't Save/Cancel dialog instead; Cancel leaves the window
and file handle fully intact; Discard closes without writing anything to disk (file mtime
unchanged); Save actually persists the change to disk before closing (confirmed via a direct
`exiftool` read of the saved `Exposure2012` tag afterward, not just "the function returned").

### Close Photo vs. Exit (`close_photo()`, File menu)

User-requested addition: "nel menu file vorrei un close (della foto) e un exit" — until this
point the File menu had no way to stop editing a file without either opening a different one or
quitting the whole app outright, and no explicit "quit" menu entry existed at all (only the
window's own × button/Alt+F4, which already went through `closeEvent()`'s confirmation).

**Esci** (Ctrl+Q) is a thin wrapper around `self.close()` — reuses `closeEvent()`'s existing
Save/Don't Save/Cancel (or plain Yes/No when nothing's unsaved) confirmation verbatim rather than
duplicating that logic, so the menu item and the window's × button always behave identically.

**Chiudi foto** (Ctrl+W, `close_photo()`) unloads the current file and returns the editor to its
literal startup state — no file open, placeholder text back on the image label — without closing
the window. Mirrors `closeEvent()`'s own dirty-state check (`_capture_edit_state()` vs.
`self._saved_edit_state`) and Save/Don't Save/Cancel dialog field-for-field, a no-op when nothing
is open. Needed no new reset logic for the sliders/filters/crop/overlays — `reset_controls()`
already does exactly that (it's the same function `load_dng()` itself calls before restoring a
newly opened file's own saved state), and its own trailing `_schedule_update()` was already safe
to call with no file open (`_render_preview()` already guards `if self.raw is None: return`,
needed for the pre-first-render window at app startup). What `close_photo()` adds on top: closing
`self.raw`, nulling `raw`/`raw_path`/`camera_profile`/`camera_wb`/`metadata_values`/
`_lens_correction` (+ clearing `lens_match_label`, see "Lens Corrections" above), clearing the
undo/redo stacks and `_saved_edit_state`, resetting the image label back to its placeholder text
(`_update_image_label()` itself only *skips* drawing when `_preview_pixmap` is `None`, it never
clears an already-displayed pixmap — this needed an explicit `image_label.clear()` +
`setText(...)` restore), and the same generation-counter bump + old-work-dir queuing
`_reset_dngconv_pipeline()` already does when switching files (just with no replacement file, so
no new working copy gets built).

**Verified** (`QT_QPA_PLATFORM=offscreen`, disposable copy of `RAW_NIKON_D90.dng`, real
`QMessageBox.question` mocked per case): `close_photo()` with nothing open is a harmless no-op;
opening a file with no edits and closing it prompts nothing extra (not dirty) and correctly
resets `raw`/`raw_path`/window title/image-label placeholder/`lens_match_label`/
`metadata_values`/`radial_filters`/`crop` all back to their startup values; dirtying a slider and
closing with Cancel leaves the file fully open and untouched; Discard closes without saving;
Save actually calls `save_to_dng()` and only proceeds to close on success. File menu order
verified as Open DNG…/Apri cartella…/Save/Export/—/Revert to Last Save/Chiudi foto/—/Esci, with
Ctrl+W and Ctrl+Q not colliding with any pre-existing shortcut in this app.

### Histogram (`HistogramWidget`, `compute_rgb_histogram()`)

User-requested: "si può mettere un grafico dell'istogramma in alto a destra come in
lightroom?" — a Lightroom-style RGB histogram, docked at the very top of the right-hand
control panel (`_build_controls()`, above the Basic panel), matching where real Lightroom's
own Develop-module histogram sits.

**Purely a display, computes nothing about the photo itself** — `compute_rgb_histogram()`
reads the actual JPEG bytes Adobe DNG Converter just rendered (`np.bincount()` per channel,
256 bins) from whatever `jpeg_path` `_on_dngconv_render_ready()` already has in hand, before
that temp file gets deleted in its own `finally` block. Wired into **only** that one call
site — the main live-preview pipeline's single choke point (`DngConverterRenderThread` and,
when local adjustments are active, `LocalAdjustmentRenderThread`'s own final composite both
land there via the same `ready` signal) — never the zoom-detail pipeline
(`_on_zoom_detail_render_ready()`), so panning/zooming into a crop never skews the histogram
to represent only the visible patch; it always reflects the whole photo, same as real
Lightroom. `close_photo()` clears it back to its empty placeholder state alongside every
other per-file reset (see that method above).

**Square-root scale, not linear or full log** — chosen after directly comparing rendered
screenshots of both alternatives on a real file (`DSC_6751_prova.dng`): plain linear made most
bins invisible next to a single tall peak (the expected problem with any natural photo's
histogram); a full `log1p` scale was tried first and produced a nearly flat-topped block for
most of the width — since log compresses so aggressively that almost any non-empty bin reads
as "near-max-height," destroying the actual peak/valley shape a histogram is supposed to show.
Square-root sits in between: still pulls small bins up out of invisibility, but keeps the real
mountain-range shape legible.

Red/green/blue channels are drawn as three semi-transparent filled paths under
`QPainter.CompositionMode_Plus` (additive blending) — where channels overlap (the common case
for most natural-image midtones, where R≈G≈B), colors add toward white instead of the
later-drawn channel simply occluding the earlier ones, the same visual signature real
Lightroom's own overlaid-RGB histogram has.

**Verified end-to-end** (`QT_QPA_PLATFORM=offscreen`, real Adobe DNG Converter renders, not
mocked, waited on via a real Qt event loop): loading `RAW_NIKON_D90.dng` populates all three
256-bin channel arrays with the correct total pixel count (2,448,000 = the work-copy preview's
own resolution) once the real background render lands; a rendered screenshot of the widget
shows a proper varied mountain-shape, not a flat block; pushing Exposure to +2.0 EV on
`DSC_6751_prova.dng` and re-rendering visibly shifts the histogram's mass toward the right
(highlights/clipping) edge in a second screenshot, confirming the widget tracks real edits
through the real render pipeline, not a static/cached image; `close_photo()` correctly resets
it to the "Istogramma non disponibile" placeholder state.

## Reference material

- Reference implementation studied for the embedded-save mechanism: RethinkRAW
  (github.com/ncruces/RethinkRAW, Go + Adobe DNG Converter). Findings are in this project's
  Claude memory (`dngforge-rethinkraw-research`), not duplicated here since they concern a
  different codebase, not this one.
- **Primary test file: `RAW_NIKON_D90.dng`** — converted from a Nikon D90 NEF via Adobe DNG
  Converter (`-c -p2 -fl -lossy`) using the user's own `nef2dnglossya.bat`. Preferred over the
  older `RAW_LEICA_M8.DNG` (from github.com/f-spot/raw-samples, kept around but secondary) since
  it's a Nikon, much closer to the real target camera (D7200 + Sigma 17-50mm, see Roadmap item
  4) than a Leica M8. Confirmed opens fine in `rawpy` (4288x2848) despite an unrelated, benign
  `exiv2` warning ("Directory Nikon1 ... considered invalid") thrown by the conversion batch
  script itself — a known exiv2 limitation parsing Nikon MakerNotes after DNG conversion, not a
  problem with the file or with DNGForge.
