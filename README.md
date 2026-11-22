# quick-editor

quick-editor is a little tool by [Luma Montes][luma] for turning photos of drawings into clean, transparent SVGs. I made it to get drawings out of my notebooks and into [Lumassets][lumassets].

![Fofolete, a little character with glasses and a striped sweater, in Quick-editor’s ink preview. The cleaning controls sit to the right.](docs/studio-screenshot.png)

Start with a photo or scan, pick the drawing you want to keep, and save it as an SVG. Everything runs on your machine.

## Install and run

Install [uv][uv] and potrace. On macOS with Homebrew:

```sh
brew install uv potrace
uv tool install git+https://github.com/lumamontes/quick-editor
```

Open a terminal in the folder where you keep your drawings:

```sh
quick-editor init
mkdir -p inbox
quick-editor
```

Copy a photo to `inbox/` and pick it in the sidebar at [localhost:4400](http://localhost:4400). JPG, PNG, HEIC, AVIF, TIFF, and WebP all work.

1. **Frame.** Mark the corners to straighten the page. For a flat scan, choose **Whole photo**.
2. **Clean.** Adjust the preview until you can see the lines you want to keep.
3. **Pieces.** Draw a box or lasso around each drawing you want to save.
4. **Review.** Give each piece a name and choose **Save checked pieces**.

Quick-editor saves your place as you work. Use `quick-editor --project <folder>` to open a different project.

## What it saves

- A transparent, recolorable SVG per drawing in `out/vector/`. Add a solid backing shape in Review if you want one.
- A cleaned PNG at the cropped source resolution in `out/originals/`.
- A draft entry in `out/catalog.json`, and your per-photo progress in `sessions/`.

Change these paths in `quick-editor.toml` to use your own project layout. The [Lumassets asset guide][guide] shows how it works with a real library.

## Refit a drawing

If you find a sharper scan, put it in the inbox and try:

```sh
quick-editor refit sq1-06
quick-editor refit sq1-06 --apply
```

The first command opens a before-and-after report. The second writes the new SVG and updates the catalog. Run `quick-editor refit` without an ID to review published drawings that still have a bitmap embedded in their SVG.

## Technology

**OpenCV** straightens pages and finds pieces, **scikit-image** separates ink from paper, and **potrace** traces the ink into SVG paths. **Pillow** and **pillow-heif** open the photos. The editor is plain HTML and JavaScript, served locally by **Starlette** and **Uvicorn**.

To run the tests from a clone:

```sh
uv run --with pytest pytest -q
```

## License

The code is [MIT](LICENSE).

[luma]: https://lumamontes.com
[lumassets]: https://lumassets.bibliotecadezines.workers.dev/
[uv]: https://docs.astral.sh/uv/
[guide]: https://github.com/lumamontes/lumassets/blob/main/docs/adding-assets.md
