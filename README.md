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
- Custom tone curve, saturation, sharpness
- Radial and graduated filters, multi-region, with Lightroom/ACR-compatible XMP persistence
- Spot removal
- Lens correction (via `lensfunpy`)
- Crop and straighten
- Undo/redo (20-state stack) + revert to last save
- EXIF metadata panel, filmstrip gallery to browse a folder
- Copy/paste settings between photos
- RGB histogram

## Structure

- `DNGForge.pyw` — main application (a single `DNGForge(QMainWindow)` class)
- `backups/` — significant earlier versions, kept for reference
- `reference/` — third-party code consulted as reference (not included in the repo)
