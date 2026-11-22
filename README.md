# quick-editor

A local tool for turning photos and scans of drawings into clean, transparent vector SVGs, with a human in the loop for every decision.

```text
photo ──▶ 1 Frame ──▶ 2 Clean ──▶ 3 Pieces ──▶ 4 Review ──▶ <id>.svg  (+ cleaned PNG, catalog entry)
          corners,     lighting,    detect,      name, tag,
          rotate       paper, ink,  box, lasso,  trace,
                       erase areas  merge        save
```

It runs on your machine, in the browser, and writes straight into your project folder. Nothing is uploaded anywhere.

## Install

```sh
brew install uv potrace
uv tool install --editable ~/www/quick-editor   # from a local clone
# later, from GitHub:  uv tool install git+https://github.com/<you>/quick-editor
```

- **uv** installs Python and the packages.
- **potrace** is the tracer (the one inside Inkscape's "Trace Bitmap").

## Use

```sh
cd my-project
quick-editor init     # once: writes quick-editor.toml (optional)
quick-editor          # opens http://localhost:4400
```

Put photos in the inbox folder (JPG, PNG, HEIC, AVIF, TIFF, WebP) and pick one in the sidebar.

1. **Frame.** Drag four corners onto the page: it gets straightened (perspective correction) and the rest of the photo is ignored. *Auto-detect* gives a starting point; *Rotate* fixes sideways photos.
2. **Clean.** Live preview of the ink.
   - **Lighting**: how broad the shadows are that get flattened out.
   - **Grain smoothing**: against paper texture and camera noise.
   - **Sensitivity** and **Faint lines**: keep or drop lighter strokes.
   - **Remove specks**, **Line weight**, **Light ink on dark surface**.
   - **Areas**: draw any shape to **erase** ink inside it (spiral binding, fingers, notes, show-through) or to **keep only** what's inside (the real edge of a curled page). Click points, click the first point to close.
   - *Use last settings* reuses the settings of the last photo you saved.
3. **Pieces.** *Detect pieces* suggests boxes; fix them by hand:
   - **Box** tool: drag to draw. **Lasso** tool: click points around a drawing, any shape.
   - Click to select (shift-click for several), **Merge**, **Delete**, drag to move, drag a box's corners to resize.
   - *Only this piece's strokes* ignores neighbouring drawings that poke into a box.
4. **Review.** Each piece is traced to SVG and shown on a transparency checkerboard (or dark, or as a cutout). Name it, tag it, pick kind and collection (or **Replaces** to give an existing entry a better source), optionally add a **solid backing shape**, and **Save**.

Progress on each photo (frame, settings, areas, pieces, names) is saved as you go, so you can stop and come back, or redo a photo with other settings.

## What gets saved

| Output | What it is |
| --- | --- |
| `<vectors>/<id>.svg` | Ink paths only, transparent background, `fill="currentColor"` (any colour, anywhere) |
| `<vectors>/<id>-backing.svg` | Optional solid silhouette, for placing a drawing over busy backgrounds like a paper cutout |
| `<originals>/<collection>/<id>.png` | The cleaned ink at full photo resolution, black on transparent |
| catalog | A **draft** entry per piece, with a `provenance` record pointing back to the photo, session and box/outline |
| `<sessions>/<photo>.studio.json` | Your progress on that photo |

## Project settings: `quick-editor.toml`

quick-editor looks for `quick-editor.toml` in the current folder or a parent (or use `--project <folder>`). Without one it uses the defaults below, relative to the current folder.

```toml
[paths]
inbox = "inbox"                # photos to process
sessions = "sessions"          # progress per photo
vectors = "out/vector"         # SVGs
originals = "out/originals"    # cleaned PNGs, per collection

[catalog]
assets = "out/catalog.json"        # a JSON list, or {"assets": [...]}
collections = "out/catalog.json"   # {"collections": [...]}; may be the same file
# categories = ["..."]   # optional: adds a "Kind" field to each drawing
```

Collections can carry an `idPrefix` (e.g. `"sq3"`), giving ids like `sq3-27`.

## How it works

No custom image science, just standard, well-tested tools:

- **OpenCV**: perspective correction, lighting flattening (estimate the paper by closing away the ink, then divide it out), free-form areas (`fillPoly`), connected-component piece detection.
- **scikit-image**: Sauvola local thresholding (made for unevenly lit documents) and speck removal.
- **potrace**: bitmap to smooth Bézier curves (via the `potrace` command, or its C library).
- **Pillow** + pillow-heif: every photo format, including iPhone HEIC.

The server is Starlette + uvicorn; the UI is plain HTML and JavaScript in `quick_editor/static/`.

## Develop

```sh
uv run quick-editor --project ~/www/lumassets   # run from the clone against a project
uv run python -m unittest discover tests        # tests
```

## Re-extracting drawings you already have: `refit`

Drawings in your catalog that are still images inside an SVG (or that you want to redo from a sharper scan) can be fixed in one go:

```sh
quick-editor refit            # preview only: opens a before/after page, changes nothing
quick-editor refit --apply    # writes the traced vectors and updates the catalog
quick-editor refit sq1-06 sq1-10 --apply   # only these
```

For each drawing it:

1. Looks for it in every photo and scan in the inbox, with SIFT features and a RANSAC homography (OpenCV). It finds it even when the photo is rotated, taken at an angle or at another size, and ignores false matches.
2. Uses that photo only when it has at least 1.25x more pixels than the current drawing (`--min-gain`), straightened at the photo's full resolution. Otherwise it uses the current image.
3. Cleans it like the studio does, traces it with potrace (hard-edged pixel art is softened a little first, so it doesn't trace as a staircase), and saves `<id>.svg` plus the cleaned ink PNG.
4. Records the source and its size in pixels in the catalog. A drawing under `[quality] min_pixels` keeps a `qualityException` saying it needs a sharper source.

The workflow for low-resolution drawings: scan the page again (600 dpi or more) or take a closer photo, put it in the inbox, run `quick-editor refit --apply`. The new source is found and used automatically.

## License

MIT. Free and open source. See [`LICENSE`](LICENSE).
