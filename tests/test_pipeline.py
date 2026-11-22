"""Run with: uv run python -m unittest discover tests"""
import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from quick_editor import config as C
from quick_editor import process as P
from quick_editor import trace as T
from quick_editor.catalog import Catalog, slugify


def fake_photo(w=1600, h=1100):
    """A page with uneven lighting, cream paper, and two separate ink drawings."""
    yy, xx = np.mgrid[0:h, 0:w]
    light = 150 + 90 * (xx / w) - 40 * (yy / h)  # strong gradient, like a desk lamp
    img = np.dstack([light * 0.93, light * 0.98, light]).clip(0, 255).astype(np.uint8)  # warm paper
    cv2.polylines(img, [np.array([[200, 200], [600, 250], [350, 700]])], True, (40, 35, 30), 6)
    cv2.line(img, [250, 300], [500, 300], (40, 35, 30), 4)
    cv2.polylines(img, [np.array([[1000, 300], [1400, 350], [1250, 900], [1050, 800]])], True, (40, 35, 30), 6)
    img[100, 100] = (0, 0, 0)  # a speck
    return img


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.img = fake_photo()
        self.flat, self.ink = P.clean(self.img, P.Settings())

    def test_flattening_whitens_uneven_paper(self):
        paper = self.flat[50:150, 700:900]
        self.assertGreater(paper.mean(), 240)
        self.assertLess(paper.std(), 10)

    def test_ink_found_and_speck_removed(self):
        self.assertTrue(self.ink[200:700, 200:600].any())
        self.assertFalse(self.ink[95:105, 95:105].any())
        self.assertLess(self.ink.mean(), 0.05)  # paper isn't ink

    def test_two_pieces(self):
        boxes = P.find_pieces(self.ink, gap=0.004)
        self.assertEqual(len(boxes), 2)
        self.assertLess(boxes[0].x, boxes[1].x)  # reading order

    def test_isolate_ignores_neighbours(self):
        big = P.Box(0.1, 0.1, 0.85, 0.8, isolate=True)
        mask = P.piece_mask(self.ink, big)
        self.assertLess(mask.shape[1], 0.6 * self.ink.shape[1])  # only one drawing kept

    def test_backing_is_solid(self):
        mask = P.piece_mask(self.ink, P.find_pieces(self.ink)[0])
        solid = P.backing_mask(mask)
        self.assertGreater(solid.sum(), mask.sum() * 3)

    def test_erase_and_keep_areas(self):
        left = [[0, 0], [0.5, 0], [0.5, 1], [0, 1]]
        erased = P.apply_areas(self.ink, [{"mode": "erase", "points": left}])
        self.assertFalse(erased[:, : self.ink.shape[1] // 2 - 2].any())
        self.assertTrue(erased[:, self.ink.shape[1] // 2 + 2:].any())
        kept = P.apply_areas(self.ink, [{"mode": "keep", "points": left}])
        self.assertFalse(kept[:, self.ink.shape[1] // 2 + 2:].any())
        self.assertEqual(len(P.find_pieces(kept)), 1)

    def test_lasso_piece_cuts_a_free_shape(self):
        h, w = self.ink.shape
        # a triangle around only the left drawing's top part
        tri = [[150 / w, 150 / h], [700 / w, 150 / h], [150 / w, 450 / h]]
        box = P.Box(150 / w, 150 / h, 550 / w, 300 / h, isolate=False, polys=[tri])
        lasso = P.piece_mask(self.ink, box)
        plain = P.piece_mask(self.ink, P.Box(150 / w, 150 / h, 550 / w, 300 / h, isolate=False))
        self.assertGreater(lasso.size, 0)
        self.assertLess(lasso.sum(), plain.sum())

    def test_perspective_warp(self):
        page = P.warp(self.img, [[100, 50], [1500, 80], [1550, 1050], [50, 1000]], max_side=800)
        self.assertEqual(max(page.shape[:2]), 800)

    def test_settings_roundtrip(self):
        s = P.Settings.from_dict({"lighting": "0.5", "invert": True, "unknown": 1, "rotate": 450})
        self.assertEqual((s.lighting, s.invert, s.rotate), (0.5, True, 90))


@unittest.skipUnless(T.available(), "potrace not installed")
class Tracing(unittest.TestCase):
    def test_trace_produces_clean_svg(self):
        mask = np.zeros((200, 300), bool)
        cv2.circle(mask.view(np.uint8), (150, 100), 60, 1, 8)
        out = T.trace(mask, title="Ring & co")
        svg = out["svg"]
        self.assertIn('fill="currentColor"', svg)
        self.assertIn('fill-rule="evenodd"', svg)
        self.assertIn("<title>Ring &amp; co</title>", svg)
        self.assertNotIn("<image", svg)  # pure vector, no embedded raster
        self.assertEqual(svg.count("M"), 2)  # outer and inner edge of the ring
        # path stays inside the viewBox and is the right way up
        nums = [float(n) for n in __import__("re").findall(r"-?\d+(?:\.\d+)?", svg.split(' d="')[1])]
        vb = [float(v) for v in svg.split('viewBox="')[1].split('"')[0].split()]
        self.assertTrue(all(0 <= n <= max(vb) for n in nums))


class CatalogIds(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "data").mkdir()
        (self.tmp / "data/collections.json").write_text(json.dumps({"releaseVersion": "0.1.0", "collections": [
            {"id": "squares-iii", "name": "Squares III", "license": "UNDECIDED", "idPrefix": "sq3"}]}))
        (self.tmp / "data/assets.json").write_text(json.dumps([
            {"id": "sq3-24", "slug": "a", "name": "A", "collection": "squares-iii", "status": "published", "version": "1.0.0"}]))
        self.c = Catalog(self.tmp / "data/assets.json", self.tmp / "data/collections.json")

    def test_next_id_and_new_collection(self):
        self.assertEqual(self.c.next_id("squares-iii"), "sq3-25")
        col = self.c.add_collection("Notebook Shards", "nb")
        self.assertEqual(self.c.next_id(col["id"]), "nb-01")
        with self.assertRaises(ValueError):
            self.c.add_collection("Other", "nb")

    def test_new_collection_copies_license(self):
        meta = json.loads((self.tmp / "data/collections.json").read_text())
        meta["collections"][0].update(license="CC0 1.0", licenseUrl="https://creativecommons.org/publicdomain/zero/1.0/", attribution="Luma")
        (self.tmp / "data/collections.json").write_text(json.dumps(meta))
        col = self.c.add_collection("Monstrinhos", "mon")
        self.assertEqual((col["license"], col["attribution"], col["idPrefix"]), ("CC0 1.0", "Luma", "mon"))

    def test_replace_keeps_curated_metadata(self):
        out = self.c.upsert({"id": "sq3-24", "name": "Untitled", "slug": "x", "status": "draft", "sourcePath": "assets/vector/sq3-24.svg"})
        self.assertEqual((out["name"], out["status"], out["version"]), ("A", "published", "1.1.0"))

    def test_collections_file_keeps_other_keys(self):
        self.c.add_collection("Notebook", "nb")
        meta = json.loads((self.tmp / "data/collections.json").read_text())
        self.assertEqual(meta["releaseVersion"], "0.1.0")

    def test_slugify(self):
        self.assertEqual(slugify("Ação Linha!"), "acao-linha")


class CombinedCatalogAndConfig(unittest.TestCase):
    def test_defaults_without_config_use_one_catalog_file(self):
        tmp = Path(tempfile.mkdtemp())
        proj = C.load(cwd=tmp)
        self.assertIsNone(proj.config_file)
        self.assertEqual(proj.assets_json, proj.collections_json)
        c = Catalog(proj.assets_json, proj.collections_json)
        self.assertEqual(c.next_id("drawings"), "d-01")
        c.upsert({"id": "d-01", "slug": "a", "name": "A", "collection": "drawings", "status": "draft"})
        col = c.add_collection("Shapes", "sh")
        c.upsert({"id": c.next_id(col["id"]), "slug": "b", "name": "B", "collection": "shapes", "status": "draft"})
        data = json.loads(proj.assets_json.read_text())
        self.assertEqual([a["id"] for a in data["assets"]], ["d-01", "sh-01"])
        self.assertEqual([x["id"] for x in data["collections"]], ["drawings", "shapes"])

    def test_config_found_from_a_subfolder(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "quick-editor.toml").write_text('[paths]\ninbox = "photos"\n[catalog]\nassets = "a.json"\ncollections = "c.json"\ncategories = ["x"]\n')
        (tmp / "deep/er").mkdir(parents=True)
        proj = C.load(cwd=tmp / "deep/er")
        self.assertEqual(proj.root, tmp.resolve())
        self.assertEqual(proj.inbox, tmp.resolve() / "photos")
        self.assertEqual(proj.vectors, tmp.resolve() / "out/vector")  # default fills the gap
        self.assertEqual(proj.categories, ["x"])
        self.assertEqual(proj.rel(proj.inbox / "p.jpg"), "photos/p.jpg")
        self.assertEqual(proj.min_pixels, 600)  # quality bar default

    def test_quality_bar_and_no_kind_by_default(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "quick-editor.toml").write_text('[quality]\nmin_pixels = 900\n')
        proj = C.load(cwd=tmp)
        self.assertEqual(proj.min_pixels, 900)
        self.assertEqual(proj.categories, [])


if __name__ == "__main__":
    unittest.main()
