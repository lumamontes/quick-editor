"""Image pipeline: load → frame (perspective) → clean (lighting, paper, ink) → pieces → masks.

All tunable sizes are relative to the page's long side, so the same settings give the same
result on a 1600 px preview and on the full-resolution photo.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps
from skimage.filters import threshold_sauvola
from skimage.morphology import remove_small_objects

try:  # iPhone photos
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:  # pragma: no cover
    pass

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".heic", ".heif", ".avif", ".bmp"}


# ---------------------------------------------------------------- settings

@dataclass
class Settings:
    """Cleaning controls. Every slider in the studio maps to one field."""

    rotate: int = 0          # 0, 90, 180, 270 (clockwise), applied before framing
    invert: bool = False     # light ink on a dark surface
    lighting: float = 0.35   # 0..1: how broad the shadows/gradients to flatten are
    smooth: float = 0.3      # 0..1: pre-blur against paper grain and sensor noise
    sensitivity: float = 0.55  # 0..1: higher keeps fainter strokes
    faint: float = 0.75      # 0..1: brightest tone that may still count as ink
    specks: float = 0.3      # 0..1: remove dots and specks up to this size
    weight: float = 0.0      # -1..1: thin or thicken strokes
    curves: float = 0.5      # 0..1: tracing, 0 = sharper corners, 1 = rounder curves

    @classmethod
    def from_dict(cls, d: dict | None) -> "Settings":
        s = cls()
        for k, v in (d or {}).items():
            if hasattr(s, k):
                setattr(s, k, type(getattr(s, k))(v))
        s.rotate = int(s.rotate) % 360
        return s

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- loading

def load_image(path: str | Path) -> np.ndarray:
    """Any photo format (incl. HEIC/AVIF), EXIF orientation applied, as BGR uint8."""
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            rgba = im.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            im = bg
        rgb = np.asarray(im.convert("RGB"))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def rotate(img: np.ndarray, degrees: int) -> np.ndarray:
    k = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}.get(degrees % 360)
    return img if k is None else cv2.rotate(img, k)


def resize_max(img: np.ndarray, max_side: int) -> tuple[np.ndarray, float]:
    h, w = img.shape[:2]
    s = min(1.0, max_side / max(h, w))
    if s >= 1.0:
        return img, 1.0
    return cv2.resize(img, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA), s


# ---------------------------------------------------------------- framing

def order_quad(pts: np.ndarray) -> np.ndarray:
    """Top-left, top-right, bottom-right, bottom-left."""
    pts = np.asarray(pts, np.float32).reshape(4, 2)
    s = pts.sum(1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([pts[s.argmin()], pts[d.argmin()], pts[s.argmax()], pts[d.argmax()]], np.float32)


def full_quad(w: int, h: int) -> list[list[float]]:
    return [[0, 0], [w, 0], [w, h], [0, h]]


def detect_page(img: np.ndarray) -> tuple[list[list[float]], float]:
    """Suggests the page corners: the largest bright, low-saturation region.

    It's a suggestion only; the studio lets you drag the corners. Returns (quad, confidence).
    """
    h, w = img.shape[:2]
    small, k = resize_max(img, 800)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    v = cv2.GaussianBlur(hsv[..., 2], (9, 9), 0)
    _, bright = cv2.threshold(v, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = cv2.bitwise_and(bright, ((hsv[..., 1] < 90) * 255).astype(np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (25, 25)))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return full_quad(w, h), 0.0
    c = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(c) / (small.shape[0] * small.shape[1])
    if area < 0.15:
        return full_quad(w, h), 0.0
    hull = cv2.convexHull(c)
    peri = cv2.arcLength(hull, True)
    approx = None
    for eps in np.linspace(0.01, 0.08, 15):
        a = cv2.approxPolyDP(hull, eps * peri, True)
        if len(a) == 4:
            approx = a
            break
    quad = cv2.boxPoints(cv2.minAreaRect(hull)) if approx is None else approx
    quad = order_quad(np.asarray(quad, np.float32) / k)
    quad[:, 0] = quad[:, 0].clip(0, w)
    quad[:, 1] = quad[:, 1].clip(0, h)
    confidence = float(min(1.0, area / 0.5)) * (1.0 if approx is not None else 0.6)
    return quad.round(1).tolist(), round(confidence, 2)


def page_size(quad) -> tuple[int, int]:
    tl, tr, br, bl = np.asarray(quad, np.float32)
    w = max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl))
    h = max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr))
    return max(1, round(w)), max(1, round(h))


def warp(img: np.ndarray, quad, max_side: int | None = None) -> np.ndarray:
    """Straightens the framed page into a rectangle, optionally capped in size."""
    q = order_quad(np.asarray(quad, np.float32))
    w, h = page_size(q)
    if max_side:
        s = min(1.0, max_side / max(w, h))
        w, h = max(1, round(w * s)), max(1, round(h * s))
    dst = np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float32)
    m = cv2.getPerspectiveTransform(q, dst)
    return cv2.warpPerspective(img, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


# ---------------------------------------------------------------- cleaning

def odd(n: float, minimum: int = 3) -> int:
    n = max(minimum, int(round(n)))
    return n if n % 2 else n + 1


def clean(page: np.ndarray, s: Settings) -> tuple[np.ndarray, np.ndarray]:
    """Returns (flat, ink): the page with lighting and paper removed (uint8, white paper),
    and the ink mask (bool)."""
    gray = cv2.cvtColor(page, cv2.COLOR_BGR2GRAY).astype(np.float32)
    if s.invert:
        gray = 255 - gray
    long_side = max(gray.shape)
    unit = long_side / 2000  # 1 "unit" = 1 px on a 2000 px page

    if s.smooth > 0:
        gray = cv2.GaussianBlur(gray, (0, 0), 0.2 + s.smooth * 1.5 * unit)

    # Paper estimate: close away the ink (dark lines thinner than the kernel), then blur.
    # Dividing by it removes shadows, light colour and paper tint in one step.
    k = odd(long_side * (0.01 + s.lighting * 0.05), 15)
    paper = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    paper = cv2.GaussianBlur(paper, (0, 0), k / 2)
    flat = np.clip(gray / np.maximum(paper, 1) * 255, 0, 255).astype(np.uint8)

    # Sauvola: a local threshold designed for unevenly lit documents.
    window = odd(long_side / 50, 15)
    t = threshold_sauvola(flat, window_size=window, k=0.5 - 0.45 * s.sensitivity)
    ink = (flat < t) & (flat < 120 + 130 * s.faint)

    if s.weight:
        r = odd(abs(s.weight) * 4 * unit, 1)
        if r > 1:
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (r, r))
            op = cv2.dilate if s.weight > 0 else cv2.erode
            ink = op(ink.astype(np.uint8), kernel).astype(bool)

    if s.specks > 0:
        ink = remove_small_objects(ink, max_size=max(1, int((s.specks * 12 * unit) ** 2)))
    return flat, ink


# ---------------------------------------------------------------- areas (free-form masks)

def polygon_mask(shape: tuple[int, int], polygons, offset=(0, 0)) -> np.ndarray:
    """Fills polygons given in normalised page coordinates (0..1) into a bool mask of `shape`.
    `offset` (x, y in pixels) shifts them, for masks of a crop."""
    h, w = shape[:2]
    full_w, full_h = offset[2] if len(offset) > 2 else w, offset[3] if len(offset) > 3 else h
    m = np.zeros((h, w), np.uint8)
    pts = [np.round(np.asarray(p, np.float64) * (full_w, full_h) - offset[:2]).astype(np.int32) for p in polygons if len(p) >= 3]
    if pts:
        cv2.fillPoly(m, pts, 1, lineType=cv2.LINE_8)
    return m.astype(bool)


def apply_areas(ink: np.ndarray, areas: list[dict] | None) -> np.ndarray:
    """Free-form areas drawn in the studio: `keep` areas (if any) limit the ink to them,
    `erase` areas remove ink (spiral binding, fingers, show-through, notes)."""
    if not areas:
        return ink
    keep = [a["points"] for a in areas if a.get("mode") == "keep"]
    erase = [a["points"] for a in areas if a.get("mode") == "erase"]
    out = ink.copy()
    if keep:
        out &= polygon_mask(ink.shape, keep)
    if erase:
        out &= ~polygon_mask(ink.shape, erase)
    return out


# ---------------------------------------------------------------- pieces

@dataclass
class Box:
    """A piece, in page coordinates normalised to 0..1."""

    x: float
    y: float
    w: float
    h: float
    isolate: bool = True  # keep only the piece's own strokes, not neighbours poking into the box
    polys: list | None = None  # free-form outline(s), normalised; None = the whole box
    meta: dict = field(default_factory=dict)


def find_pieces(ink: np.ndarray, gap: float = 0.004, min_size: float = 0.02) -> list[Box]:
    """Groups strokes closer than `gap` (fraction of the long side) into pieces and drops
    pieces whose bounding box is smaller than `min_size` of the long side."""
    h, w = ink.shape
    long_side = max(h, w)
    r = odd(gap * long_side * 2, 1)
    grown = cv2.dilate(ink.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (r, r))) if r > 1 else ink.astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(grown, connectivity=8)
    boxes = []
    for i in range(1, n):
        ys, xs = np.nonzero((labels == i) & ink)
        if xs.size == 0:
            continue
        x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
        if max(x1 - x0, y1 - y0) < min_size * long_side:
            continue
        boxes.append(Box(x0 / w, y0 / h, (x1 - x0) / w, (y1 - y0) / h))
    # reading order: rows (by top edge, in bands), then left to right
    band = max(0.05, gap * 8)
    boxes.sort(key=lambda b: (round(b.y / band), b.x))
    return boxes


def piece_mask(ink: np.ndarray, box: Box, gap: float = 0.004) -> np.ndarray:
    """The ink of one piece, cropped tight. With `isolate`, only the stroke group that holds
    most of the ink in the box is kept."""
    h, w = ink.shape
    x0, y0 = max(0, int(box.x * w)), max(0, int(box.y * h))
    x1, y1 = min(w, int(np.ceil((box.x + box.w) * w))), min(h, int(np.ceil((box.y + box.h) * h)))
    crop = ink[y0:y1, x0:x1].copy()
    if box.polys:
        crop &= polygon_mask(crop.shape, box.polys, offset=(x0, y0, w, h))
    if crop.size == 0 or not crop.any():
        return np.zeros((0, 0), bool)
    if box.isolate:
        r = odd(gap * max(h, w) * 2, 1)
        grown = cv2.dilate(crop.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (r, r))) if r > 1 else crop.astype(np.uint8)
        n, labels = cv2.connectedComponents(grown, connectivity=8)
        if n > 2:
            counts = np.bincount(labels[crop], minlength=n)
            counts[0] = 0
            crop &= labels == counts.argmax()
    ys, xs = np.nonzero(crop)
    return crop[ys.min(): ys.max() + 1, xs.min(): xs.max() + 1]


def backing_mask(mask: np.ndarray) -> np.ndarray:
    """A solid silhouette of the piece (like paper cut around the drawing), for placing it
    over busy backgrounds."""
    long_side = max(mask.shape)
    pad = odd(long_side * 0.02, 3)
    m = cv2.copyMakeBorder(mask.astype(np.uint8) * 255, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (pad, pad))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k, iterations=2)
    contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    solid = np.zeros_like(m)
    cv2.drawContours(solid, contours, -1, 255, thickness=cv2.FILLED)
    solid = cv2.dilate(solid, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (odd(pad / 2), odd(pad / 2))))
    return solid[pad:-pad, pad:-pad] > 0


def ink_png(mask: np.ndarray) -> Image.Image:
    """Black ink on a transparent background: the cleaned raster original we keep."""
    a = mask.astype(np.uint8) * 255
    rgba = np.zeros((*mask.shape, 4), np.uint8)
    rgba[..., 3] = a
    return Image.fromarray(rgba, "RGBA")
