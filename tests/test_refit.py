"""refit: finds a drawing inside a bigger, rotated, perspective photo and re-extracts it from there."""
import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from quick_editor import config as C, refit, trace as T


def drawing(size=240, seed=3) -> np.ndarray:
    """A synthetic hand-drawing-ish image: random strokes, ink coverage 0..1."""
    rng = np.random.default_rng(seed)
    img = np.zeros((size, size), np.uint8)
    for _ in range(40):
        p = rng.integers(10, size - 10, 4)
        cv2.line(img, (int(p[0]), int(p[1])), (int(p[2]), int(p[3])), 255, int(rng.integers(1, 4)), cv2.LINE_AA)
    for _ in range(8):
        c = rng.integers(30, size - 30, 2)
        cv2.circle(img, (int(c[0]), int(c[1])), int(rng.integers(5, 25)), 255, 2, cv2.LINE_AA)
    return img.astype(np.float32) / 255


def svg_with_image(cov: np.ndarray) -> str:
    import base64, io
    rgba = np.zeros((*cov.shape, 4), np.uint8)
    rgba[..., 3] = (cov * 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG")
    h, w = cov.shape
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}"><image width="{w}" height="{h}" '
            f'href="data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"/></svg>')


class Refit(unittest.TestCase):
    def test_rejects_degenerate_homographies(self):
        self.assertIsNone(refit._plausible_scale(np.diag([0.001, 0.001, 1.0]), 100, 100))  # collapsed
        self.assertIsNone(refit._plausible_scale(np.diag([4.0, 1.0, 1.0]), 100, 100))      # stretched
        self.assertAlmostEqual(refit._plausible_scale(np.diag([3.0, 3.0, 1.0]), 100, 100), 3.0)

    @unittest.skipUnless(T.available(), "potrace not installed")
    def test_finds_the_drawing_in_a_sharper_photo_and_retraces_it(self):
        root = Path(tempfile.mkdtemp())
        (root / "quick-editor.toml").write_text('[paths]\ninbox = "inbox"\n[catalog]\nassets = "a.json"\ncollections = "a.json"\n')
        small = drawing()
        # the "photo": the same drawing 3x bigger, rotated and in perspective, on grey paper
        big = cv2.resize(small, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        page = np.full((1400, 1600), 0.0, np.float32)
        page[300:300 + big.shape[0], 400:400 + big.shape[1]] = big
        M = cv2.getPerspectiveTransform(np.float32([[0, 0], [1600, 0], [1600, 1400], [0, 1400]]),
                                        np.float32([[60, 20], [1560, 90], [1500, 1380], [10, 1300]]))
        page = cv2.warpPerspective(page, M, (1600, 1400))
        photo = (200 - page * 170).clip(0, 255).astype(np.uint8)
        (root / "inbox").mkdir()
        cv2.imwrite(str(root / "inbox" / "photo.png"), cv2.rotate(photo, cv2.ROTATE_90_CLOCKWISE))
        (root / "src").mkdir()
        (root / "src" / "d-01.svg").write_text(svg_with_image(small))
        (root / "a.json").write_text(json.dumps({"collections": [{"id": "d", "name": "D", "idPrefix": "d"}], "assets": [
            {"id": "d-01", "name": "Test", "collection": "d", "status": "published", "version": "1.0.0",
             "sourcePath": "src/d-01.svg", "containsRaster": True, "qualityException": "old"}]}))
        proj = C.load(cwd=root)

        refit.run(proj, apply=True, log=lambda *_: None)
        a = json.loads((root / "a.json").read_text())["assets"][0]
        self.assertFalse(a["containsRaster"])
        self.assertIn("photo.png", a["provenance"]["from"])
        self.assertGreater(a["provenance"]["pixels"], 600)       # ~720: 3x the old 240 px
        self.assertNotIn("qualityException", a)                  # above the bar now
        self.assertEqual(a["version"], "1.1.0")
        self.assertEqual(a["previousSources"], ["src/d-01.svg"])
        svg = (root / a["sourcePath"]).read_text()
        self.assertIn('fill="currentColor"', svg)
        self.assertNotIn("data:image", svg)
        self.assertTrue((root / ".quick-editor/refit/index.html").exists())

    @unittest.skipUnless(T.available(), "potrace not installed")
    def test_preview_changes_nothing(self):
        root = Path(tempfile.mkdtemp())
        (root / "src").mkdir()
        (root / "src" / "d-01.svg").write_text(svg_with_image(drawing(120)))
        cat = {"assets": [{"id": "d-01", "name": "T", "collection": "d", "status": "published", "version": "1.0.0",
                           "sourcePath": "src/d-01.svg", "containsRaster": True}]}
        (root / "a.json").write_text(json.dumps(cat))
        (root / "quick-editor.toml").write_text('[catalog]\nassets = "a.json"\ncollections = "a.json"\n')
        refit.run(C.load(cwd=root), apply=False, log=lambda *_: None)
        self.assertEqual(json.loads((root / "a.json").read_text()), cat)


if __name__ == "__main__":
    unittest.main()
