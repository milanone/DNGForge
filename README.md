# DNGForge

Non-destructive RAW editor for DNG files, in the style of Lightroom/Camera Raw: white balance,
tone curve, saturation/sharpness, radial and graduated filters, spot removal, lens correction,
crop/straighten, undo/redo, EXIF metadata panel, filmstrip gallery.

**Why**: a zero-cost alternative to a Lightroom subscription, not an alternative to Adobe as a
technology. DNGForge writes edits into the real Adobe `crs` XMP tags (Adobe DNG Converter is free,
unlike Lightroom) — a file processed here, opened in real Lightroom, comes out practically
identical and still fully editable, and vice versa. No external sidecar files: saving **always and
only** writes inside the DNG itself.

![DNGForge screenshot](screenshot.png)

## How it works

Every pixel shown or saved is rendered by **Adobe DNG Converter**, a required runtime dependency:
`DNGForge.pyw` only builds the editing XMP tags and hands them to the converter — it never touches
pixels directly.

One exception: the three local-adjustment tools — radial filter, graduated filter and spot
removal — aren't rendered by Adobe DNG Converter (unsupported by its CLI), so for those three
tools only, the app composites pixels locally (OpenCV/Pillow) on top of Adobe's own render.

## Requirements

- Python 3.10+ and the dependencies in `requirements.txt` (`pip install -r requirements.txt`)
- [Adobe DNG Converter](https://helpx.adobe.com/camera-raw/using/adobe-dng-converter.html)
  installed at the standard Windows path
- a real `exiftool` binary (not just the library) version ≥ 12.15

## Running

```
pythonw DNGForge.pyw [file.dng]
```

The (optional) argument loads a DNG on startup.

## Main features

- White balance: color picker on the raw sensor data, presets, colorimetric temperature/tint
- Basic corrections (exposure, contrast) and tone (highlights/shadows/whites/blacks) as
  independent panels — a Reset in one never touches the other
- "Eye Perception" one-click tone preset: perceptual dynamic-range compression paired with
  local contrast, a midtone curve and a targeted skin-tone correction, tuned for a natural
  (not flat) look straight out of the camera
- Custom tone curve, saturation, sharpness
- Radial and graduated filters, multi-region, with Lightroom/ACR-compatible XMP persistence
- Spot removal
- Lens correction (via `lensfunpy`)
- Crop and straighten
- Default-preview toggle to compare the current edit against the untouched starting point
- Undo/redo (20-state stack) + revert to last save
- Per-session edit cache: switching between photos while working through a folder never
  loses unsaved adjustments, even without saving each one first
- EXIF metadata panel, filmstrip gallery to browse a folder, with a green/red dot on each
  thumbnail showing whether it's saved with edits or has changes still pending, and a
  right-click "Delete" that moves the file to the Recycle Bin
- Copy/paste settings between photos
- RGB histogram

## Save output

The embedded preview is capped at 12 megapixels by default — large enough for full-screen
viewing and most prints, and crop-aware (a crop keeps native resolution up to that cap rather
than being additionally downscaled, but is never upscaled past what the sensor actually
captured). The original raw data inside the DNG is never touched or re-rendered by this cap;
Export Preview as JPEG always renders at full resolution regardless. Saving reuses a single
background `exiftool` process instead of spawning one per write, and writes exactly one
full-resolution embedded copy plus small thumbnails instead of duplicating the full-resolution
preview into every legacy preview slot a DNG can have.

## Batch RAW converter (`dng_converter_gui.pyw`)

A separate, standalone GUI for batch-converting a folder of NEF/DNG files — the step that
typically comes *before* editing in DNGForge itself (e.g. converting a card full of NEFs to DNG,
or producing delivery JPEGs from already-edited DNGs). Not integrated into `DNGForge.pyw`'s own
window; run independently.

Output options:
- **JPG** — extracts the embedded preview (via `exiftool`) and optionally resizes/recompresses it
  with Pillow; at original resolution, extraction can be lossless (no recompression at all)
- **DNG lossy** / **DNG lossless** — via Adobe DNG Converter

Resolution is a cap, never an upscale: original, a target megapixel count, 4K (3840px long edge),
or 2048px. JPEG quality is editable (default 69). Optionally sets each output file's
modification date from its EXIF shooting date, so converted files keep sorting correctly in a
file browser. Detects Adobe DNG Converter and `exiftool` automatically and shows which required
tools are missing. Preferences (format/resolution/quality) persist between runs in
`dng_converter_gui.cfg.json` (not tracked in the repo).

Run with:

```
pythonw dng_converter_gui.pyw
```

## Structure

- `DNGForge.pyw` — main application (a single `DNGForge(QMainWindow)` class)
- `dng_converter_gui.pyw` — standalone batch NEF/DNG → JPG/DNG converter, see above
- `backups/` — significant earlier versions, kept for reference
- `reference/` — third-party code consulted as reference (not included in the repo)

## License

[MIT](LICENSE)
