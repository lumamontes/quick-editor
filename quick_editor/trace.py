"""Bitmap → SVG with potrace, the tracer behind Inkscape's "Trace Bitmap".

Uses the `potrace` command if installed (`brew install potrace`), otherwise the potrace C
library directly (installed along with it). Output: one evenodd path filled with
currentColor on a transparent background, so it can be recoloured and placed anywhere.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np

# Small pieces are smoothly upscaled before tracing: smoother curves, fewer stair-steps.
TRACE_LONG_SIDE = 2400


class PotraceMissing(RuntimeError):
    pass


def available() -> str | None:
    """Which backend will be used, or None."""
    if shutil.which("potrace"):
        return "cli"
    if _lib() is not None:
        return "library"
    return None


def trace(mask: np.ndarray, curves: float = 0.5, title: str = "") -> dict:
    """Traces an ink mask: bool, or float 0..1 coverage (keeps sub-pixel edges).
    Returns {svg, width, height}, sized in mask pixels."""
    if mask.size == 0 or not (mask > 0.5).any():
        raise ValueError("Nothing to trace: the piece has no ink")
    h, w = mask.shape
    # Small pieces get more upscaling (up to 8x): with anti-aliased input, the 50% contour of the
    # upscaled coverage follows the real edge between pixels instead of the pixel staircase.
    scale = max(1.0, min(8.0, TRACE_LONG_SIDE / max(h, w)))
    pad = 2
    coverage = mask.dtype != bool  # 0..1 ink coverage (anti-aliased) rather than a hard mask
    m = cv2.copyMakeBorder(mask.astype(np.float32), pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0)
    if scale > 1:
        if not coverage:
            m = cv2.GaussianBlur(m, (0, 0), 0.6)
        m = cv2.resize(m, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    bitmap = m > 0.5

    params = dict(
        turdsize=int(round(3 * scale * scale)),   # ignore specks of a few source pixels
        alphamax=0.55 + 0.75 * curves,            # potrace corner threshold (default 1.0)
        opttolerance=0.2,
    )
    backend = available()
    if backend == "cli":
        d = _trace_cli(bitmap, **params)
    elif backend == "library":
        d = _trace_lib(bitmap, **params)
    else:
        raise PotraceMissing("potrace is not installed. On macOS: brew install potrace")

    # Coordinates are in upscaled pixels with padding; map back to mask pixels with a viewBox.
    vw, vh = bitmap.shape[1], bitmap.shape[0]
    out_w, out_h = round(vw / scale), round(vh / scale)
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {vw} {vh}" width="{out_w}" height="{out_h}">'
        + (f"<title>{esc(title)}</title>" if title else "")
        + f'<path fill="currentColor" fill-rule="evenodd" d="{d}"/></svg>\n'
    )
    return {"svg": svg, "width": out_w, "height": out_h}


# ---------------------------------------------------------------- CLI backend

def _trace_cli(bitmap: np.ndarray, turdsize: int, alphamax: float, opttolerance: float) -> str:
    h, w = bitmap.shape
    with tempfile.TemporaryDirectory() as tmp:
        pbm = Path(tmp) / "in.pbm"
        # P4 = binary PBM, 1 = black (ink)
        pbm.write_bytes(f"P4\n{w} {h}\n".encode() + np.packbits(bitmap, axis=1).tobytes())
        out = subprocess.run(
            [
                "potrace", str(pbm), "--backend", "svg", "--flat", "--output", "-",
                "--turdsize", str(turdsize), "--alphamax", f"{alphamax:.3f}",
                "--opttolerance", f"{opttolerance:.3f}", "--resolution", "72", "--unit", "10",
            ],
            check=True, capture_output=True, text=True,
        ).stdout
    # potrace writes coordinates ×10 with a flipped y axis: transform="translate(0,H) scale(0.1,-0.1)".
    paths = re.findall(r'<path[^>]*\sd="([^"]+)"', out)
    return _flip_scale(" ".join(paths), h)


_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _flip_scale(d: str, height: int) -> str:
    """Rewrites potrace's relative path data into absolute pixel coordinates (y down)."""
    tokens = re.findall(r"[MmLlCcZz]|-?\d+(?:\.\d+)?", d)
    out, i, cmd = [], 0, None
    x = y = sx = sy = 0.0
    def num():
        nonlocal i
        v = float(tokens[i]); i += 1
        return v / 10
    while i < len(tokens):
        t = tokens[i]
        if t.isalpha():
            cmd = t; i += 1
            if cmd in "Zz":
                out.append("Z"); x, y = sx, sy
            continue
        if cmd in ("M", "m"):
            nx, ny = num(), num()
            if cmd == "m" and out:
                nx, ny = x + nx, y + ny
            x, y, sx, sy = nx, ny, nx, ny
            out.append(f"M{_f(x)} {_f(height - y)}")
            cmd = "l" if cmd == "m" else "L"
        elif cmd in ("L", "l"):
            nx, ny = num(), num()
            if cmd == "l":
                nx, ny = x + nx, y + ny
            x, y = nx, ny
            out.append(f"L{_f(x)} {_f(height - y)}")
        elif cmd in ("C", "c"):
            pts = [num() for _ in range(6)]
            if cmd == "c":
                pts = [pts[0] + x, pts[1] + y, pts[2] + x, pts[3] + y, pts[4] + x, pts[5] + y]
            out.append("C" + " ".join(f"{_f(pts[k])} {_f(height - pts[k + 1])}" for k in (0, 2, 4)))
            x, y = pts[4], pts[5]
        else:  # pragma: no cover
            raise ValueError(f"unexpected path command {cmd}")
    return "".join(out)


def _f(v: float) -> str:
    s = f"{v:.1f}".rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


# ---------------------------------------------------------------- library backend (ctypes)

_word = ctypes.c_ulong
_BITS = ctypes.sizeof(_word) * 8


class _Bitmap(ctypes.Structure):
    _fields_ = [("w", ctypes.c_int), ("h", ctypes.c_int), ("dy", ctypes.c_int), ("map", ctypes.POINTER(_word))]


class _Progress(ctypes.Structure):
    _fields_ = [("callback", ctypes.c_void_p), ("data", ctypes.c_void_p),
                ("min", ctypes.c_double), ("max", ctypes.c_double), ("epsilon", ctypes.c_double)]


class _Param(ctypes.Structure):
    _fields_ = [("turdsize", ctypes.c_int), ("turnpolicy", ctypes.c_int), ("alphamax", ctypes.c_double),
                ("opticurve", ctypes.c_int), ("opttolerance", ctypes.c_double), ("progress", _Progress)]


class _DPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _Curve(ctypes.Structure):
    _fields_ = [("n", ctypes.c_int), ("tag", ctypes.POINTER(ctypes.c_int)), ("c", ctypes.POINTER(_DPoint * 3))]


class _Path(ctypes.Structure):
    pass


_Path._fields_ = [("area", ctypes.c_int), ("sign", ctypes.c_int), ("curve", _Curve),
                  ("next", ctypes.POINTER(_Path)), ("childlist", ctypes.POINTER(_Path)),
                  ("sibling", ctypes.POINTER(_Path)), ("priv", ctypes.c_void_p)]


class _State(ctypes.Structure):
    _fields_ = [("status", ctypes.c_int), ("plist", ctypes.POINTER(_Path)), ("priv", ctypes.c_void_p)]


_LIB = None


def _lib():
    global _LIB
    if _LIB is not None:
        return _LIB or None
    candidates = [ctypes.util.find_library("potrace"), "libpotrace.so.0", "/opt/homebrew/lib/libpotrace.dylib",
                  "/usr/local/lib/libpotrace.dylib"]
    for c in filter(None, candidates):
        try:
            lib = ctypes.CDLL(c)
        except OSError:
            continue
        lib.potrace_param_default.restype = ctypes.POINTER(_Param)
        lib.potrace_trace.restype = ctypes.POINTER(_State)
        lib.potrace_trace.argtypes = [ctypes.POINTER(_Param), ctypes.POINTER(_Bitmap)]
        lib.potrace_state_free.argtypes = [ctypes.POINTER(_State)]
        lib.potrace_param_free.argtypes = [ctypes.POINTER(_Param)]
        _LIB = lib
        return lib
    _LIB = False
    return None


def _trace_lib(bitmap: np.ndarray, turdsize: int, alphamax: float, opttolerance: float) -> str:
    lib = _lib()
    h, w = bitmap.shape
    dy = (w + _BITS - 1) // _BITS
    # pack rows MSB-first into machine words
    padded = np.zeros((h, dy * _BITS), np.uint8)
    padded[:, :w] = bitmap
    bytes_ = np.packbits(padded, axis=1).reshape(h, dy, _BITS // 8)
    words = np.zeros((h, dy), np.uint64)
    for b in range(_BITS // 8):
        words = (words << np.uint64(8)) | bytes_[:, :, b].astype(np.uint64)
    buf = (_word * (h * dy))(*words.ravel().tolist())
    bm = _Bitmap(w, h, dy, buf)

    param = lib.potrace_param_default()
    param.contents.turdsize = turdsize
    param.contents.alphamax = alphamax
    param.contents.opttolerance = opttolerance
    state = lib.potrace_trace(param, ctypes.byref(bm))
    try:
        if not state or state.contents.status != 0:
            raise RuntimeError("potrace failed")
        parts = []
        p = state.contents.plist
        while p:
            curve = p.contents.curve
            n = curve.n
            last = curve.c[n - 1][2]
            seg = [f"M{_f(last.x)} {_f(last.y)}"]
            for i in range(n):
                c = curve.c[i]
                if curve.tag[i] == 2:  # POTRACE_CORNER
                    seg.append(f"L{_f(c[1].x)} {_f(c[1].y)}L{_f(c[2].x)} {_f(c[2].y)}")
                else:  # POTRACE_CURVETO
                    seg.append(f"C{_f(c[0].x)} {_f(c[0].y)} {_f(c[1].x)} {_f(c[1].y)} {_f(c[2].x)} {_f(c[2].y)}")
            parts.append("".join(seg) + "Z")
            p = p.contents.next
        return "".join(parts)
    finally:
        if state:
            lib.potrace_state_free(state)
        lib.potrace_param_free(param)
