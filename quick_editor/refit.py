"""`quick-editor refit`: re-extract drawings that are already in the catalog, in one go.

For each drawing whose SVG is still an embedded image (not traced yet):

1. Find it in the photos and scans in the inbox. SIFT features + a RANSAC homography (OpenCV),
   so it is found even in a photo taken at an angle, rotated, or at a different size.
2. Pick the source with the most real pixels: that photo region (straightened at full
   resolution), or the image already inside the SVG.
3. Clean it (the same lighting flattening + Sauvola threshold as the studio) and trace it with
   potrace into a vector SVG.
4. Write a before/after review page. Only with --apply are files and the catalog changed.

Run it again whenever you add a sharper scan to the inbox: drawings it can now take from more
pixels get re-extracted from it.
"""
from __future__ import annotations

import base64
import html
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from . import catalog as K
from . import process as P
from . import trace as T
from .config import Project

DATA_IMAGE = re.compile(r'(?:xlink:)?href="data:image/(?:png|jpe?g);base64,([^"]+)"')
MIN_INLIERS = 10


@dataclass
class Found:
    photo: Path
    H: np.ndarray       # template pixels -> photo pixels
    scale: float        # photo pixels per template pixel
    inliers: int


# ---------------------------------------------------------------- reading what's there

def embedded_image(svg_text: str) -> np.ndarray | None:
    """The largest image embedded in an SVG, as ink coverage 0..1 (float32), or None."""
    blobs = DATA_IMAGE.findall(svg_text)
    if not blobs:
        return None
    return _coverage(Image.open(io.BytesIO(base64.b64decode(max(blobs, key=len)))))


def _coverage(im: Image.Image) -> np.ndarray:
    """Ink coverage 0..1 of an image: dark and opaque = ink."""
    a = np.asarray(im.convert("RGBA")).astype(np.float32) / 255
    lum = 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]
    return np.clip((1 - lum) * a[..., 3], 0, 1)


# ---------------------------------------------------------------- finding it in a photo

class Locator:
    """SIFT features for every photo in the inbox, computed once."""

    def __init__(self, photos: list[Path]):
        self.sift = cv2.SIFT_create(nfeatures=40000)
        self.matcher = cv2.FlannBasedMatcher({"algorithm": 1, "trees": 5}, {"checks": 64})
        self.photos = []
        for p in photos:
            gray = cv2.cvtColor(P.load_image(p), cv2.COLOR_BGR2GRAY)
            kp, des = self.sift.detectAndCompute(gray, None)
            if des is not None and len(kp) > 20:
                self.photos.append((p, kp, des))

    def find(self, coverage: np.ndarray) -> Found | None:
        img = (255 - coverage * 255).astype(np.uint8)
        h, w = img.shape
        # tiny drawings have too few features at their own size; bigger ones don't need all pixels
        k = 600 / max(h, w) if max(h, w) < 600 else min(1.0, 2500 / max(h, w))
        work = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC if k > 1 else cv2.INTER_AREA)
        kp, des = self.sift.detectAndCompute(work, None)
        if des is None or len(kp) < 6:
            return None
        best: Found | None = None
        for path, pk, pd in self.photos:
            pairs = [m for m in self.matcher.knnMatch(des, pd, k=2) if len(m) == 2]
            good = [a for a, b in pairs if a.distance < 0.75 * b.distance]
            if len(good) < MIN_INLIERS:
                continue
            src = np.float32([kp[m.queryIdx].pt for m in good]) / k
            dst = np.float32([pk[m.trainIdx].pt for m in good])
            H, inl = cv2.findHomography(src, dst, cv2.RANSAC, 4.0)
            if H is None:
                continue
            n = int(inl.sum())
            scale = _plausible_scale(H, w, h)
            if n >= MIN_INLIERS and n >= 0.3 * len(good) and scale and (best is None or n > best.inliers):
                best = Found(path, H, scale, n)
        return best


def _plausible_scale(H: np.ndarray, w: int, h: int) -> float | None:
    """Photo pixels per drawing pixel, or None when the homography is degenerate
    (folded, collapsed or wildly stretched): a false match."""
    c = cv2.perspectiveTransform(np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(-1, 1, 2), H).reshape(-1, 2)
    if not cv2.isContourConvex(c.astype(np.float32)):
        return None
    sides = [np.linalg.norm(c[(i + 1) % 4] - c[i]) for i in range(4)]
    sx, sy = (sides[0] + sides[2]) / 2 / w, (sides[1] + sides[3]) / 2 / h
    if min(sx, sy) < 0.2 or max(sx, sy) > 20 or max(sx, sy) / min(sx, sy) > 1.5:
        return None
    return float((sx + sy) / 2)


def straighten(photo: Path, found: Found, shape: tuple[int, int]) -> np.ndarray:
    """The drawing's region of the photo, straightened, at the photo's own resolution."""
    h, w = shape
    s = found.scale
    to_photo = found.H @ np.diag([1 / s, 1 / s, 1])  # output pixels -> photo pixels
    return cv2.warpPerspective(P.load_image(photo), to_photo, (round(w * s), round(h * s)),
                               flags=cv2.INTER_CUBIC | cv2.WARP_INVERSE_MAP, borderValue=(255, 255, 255))


# ---------------------------------------------------------------- the run

def run(project: Project, ids: list[str] | None = None, apply: bool = False, min_gain: float = 1.25,
        log=print) -> Path:
    catalog = K.Catalog(project.assets_json, project.collections_json)
    assets = catalog.assets()
    todo = []
    for a in assets:
        if ids and a["id"] not in ids:
            continue
        if not ids and a.get("status") != "published":
            continue
        src = project.root / a.get("sourcePath", "")
        if not src.is_file():
            log(f"  skip {a['id']}: {a.get('sourcePath')} not found")
            continue
        cov = embedded_image(src.read_text("utf-8"))
        if cov is None and not ids:
            continue  # already a traced vector
        if cov is None:
            orig = project.root / a.get("originalPath", "")
            if not orig.is_file():
                log(f"  skip {a['id']}: already a vector, and no original PNG to re-trace from")
                continue
            cov = _coverage(Image.open(orig))
        todo.append((a, cov))
    if not todo:
        log("Nothing to refit: every published drawing is already a traced vector.")
    photos = sorted(p for p in project.inbox.iterdir() if p.suffix.lower() in P.IMAGE_EXTS) if project.inbox.is_dir() else []
    log(f"Refitting {len(todo)} drawing(s); looking for them in {len(photos)} photo(s) in {project.rel(project.inbox) if project.inbox.is_dir() else 'no inbox'}…")
    locator = Locator(photos) if photos and todo else None

    report = project.root / ".quick-editor" / "refit"
    report.mkdir(parents=True, exist_ok=True)
    rows = []
    for a, cov in todo:
        h, w = cov.shape
        found = locator.find(cov) if locator else None
        if found and found.scale >= min_gain:
            crop = straighten(found.photo, found, (h, w))
            _, ink = P.clean(crop, P.Settings())
            # keep only this drawing: its old silhouette, grown a little, marks the area
            area = cv2.resize((cov > 0.15).astype(np.uint8), (crop.shape[1], crop.shape[0]), interpolation=cv2.INTER_NEAREST)
            r = P.odd(max(crop.shape) * 0.02, 3)
            area = cv2.dilate(area, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (r, r))).astype(bool)
            mask = ink & area
            source, px = f"{found.photo.name} (×{found.scale:.1f})", int(max(crop.shape[:2]))
        else:
            mask, px = cov, int(max(h, w))
            # a hard-edged (non anti-aliased) image traces as a staircase: soften its edges a
            # little first. 0.6 px removes the steps and keeps thin lines (1 px loses them).
            inked = mask > 0.1
            if inked.any() and ((mask > 0.1) & (mask < 0.9)).sum() < 0.05 * inked.sum():
                mask = cv2.GaussianBlur(mask.astype(np.float32), (0, 0), 0.6)
            source = "the image inside the current SVG" + (f" (best photo match only ×{found.scale:.1f}: {found.photo.name})" if found else "")
        name = a.get("name", a["id"])
        traced = T.trace(mask, curves=0.5, title=name)
        small = px < project.min_pixels
        (report / f"{a['id']}.svg").write_text(traced["svg"], "utf-8")
        before = report / f"{a['id']}-before.png"
        Image.fromarray(((1 - np.clip(cov, 0, 1)) * 255).astype(np.uint8)).save(before)
        rows.append(dict(id=a["id"], name=name, px=px, source=source, small=small, inliers=found.inliers if found else 0))
        log(f"  {a['id']:<8} {px:>5} px  from {source}{'   below ' + str(project.min_pixels) + ' px' if small else ''}")

        if apply:
            vector = project.vectors / f"{a['id']}.svg"
            vector.parent.mkdir(parents=True, exist_ok=True)
            vector.write_text(traced["svg"], "utf-8")
            original = project.originals / a.get("collection", "misc") / f"{a['id']}.png"
            original.parent.mkdir(parents=True, exist_ok=True)
            alpha = (np.clip(mask, 0, 1) * 255).astype(np.uint8) if mask.dtype != bool else mask.astype(np.uint8) * 255
            rgba = np.zeros((*alpha.shape, 4), np.uint8)
            rgba[..., 3] = alpha
            Image.fromarray(rgba, "RGBA").save(original, optimize=True)  # black ink, transparent
            vb = re.search(r'viewBox="0 0 (\d+) (\d+)"', traced["svg"])
            old = a.get("sourcePath")
            a.update({
                "sourcePath": project.rel(vector), "originalPath": project.rel(original), "format": "svg",
                "width": int(vb.group(1)), "height": int(vb.group(2)), "viewBox": f"0 0 {vb.group(1)} {vb.group(2)}",
                "fileSize": vector.stat().st_size, "containsRaster": False, "version": K.bump(a.get("version", "1.0.0")),
            })
            if old and old != a["sourcePath"]:
                a.setdefault("previousSources", []).append(old)
            prov = a.setdefault("provenance", {})
            prov.update({"method": "refit", "from": source, "pixels": px})
            if small:
                a["qualityException"] = (f"Traced from only {px} px: needs a sharper scan or a closer photo. "
                                         "Put it in the inbox and run `quick-editor refit` again.")
            else:
                a.pop("qualityException", None)

    if apply and todo:
        catalog._save_assets(assets)
    _write_report(report, rows, project, apply)
    return report / "index.html"


def _write_report(folder: Path, rows: list[dict], project: Project, applied: bool) -> None:
    rows = sorted(rows, key=lambda r: r["px"])
    cards = "".join(
        f'<figure class="{"small" if r["small"] else ""}"><div class="pair">'
        f'<img src="{r["id"]}-before.png" alt=""><img src="{r["id"]}.svg" alt=""></div>'
        f'<figcaption><b>{html.escape(r["id"])}</b> {html.escape(r["name"])}<br>{r["px"]} px · {html.escape(r["source"])}</figcaption></figure>'
        for r in rows)
    n_small = sum(r["small"] for r in rows)
    (folder / "index.html").write_text(f"""<!doctype html><meta charset="utf-8"><title>refit review</title>
<style>body{{font:14px system-ui;margin:2rem}}.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:1.5rem}}
.pair{{display:grid;grid-template-columns:1fr 1fr;gap:4px}}.pair img{{width:100%;aspect-ratio:1;object-fit:contain;border:1px solid #ddd;background:#fff}}
figure{{margin:0}}figcaption{{font:12px ui-monospace,monospace;margin-top:.4rem}}.small figcaption{{color:#a15c00}}</style>
<h1>refit: {len(rows)} drawing(s){' (applied)' if applied else ' (preview, nothing changed)'}</h1>
<p>Left: what the site uses now (an image). Right: the traced vector. Both are enlarged to the same size.
{n_small} drawing(s) are under {project.min_pixels} px (orange): they stay marked until a sharper scan or photo is in the inbox.</p>
<div class="grid">{cards}</div>""", "utf-8")
