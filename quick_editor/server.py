"""quick-editor's local web server.

    quick-editor                    # in a project folder (finds quick-editor.toml)
    quick-editor --project ~/www/x  # any project
    quick-editor init               # write a starter quick-editor.toml here
"""
from __future__ import annotations

import argparse
import json
import threading
import webbrowser
from collections import OrderedDict
from pathlib import Path

import cv2
import numpy as np
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import config as C
from . import process as P
from . import trace as T
from .catalog import Catalog

STATIC = Path(__file__).parent / "static"
PREVIEW_SIDE = 1600
FULL_SIDE = 6000

project: C.Project
catalog: Catalog
_lock = threading.Lock()


def configure(proj: C.Project) -> None:
    """Points the server at a project (called by main(); tests call it directly)."""
    global project, catalog
    project = proj
    catalog = Catalog(proj.assets_json, proj.collections_json)
    _photos.clear()
    _cleaned.clear()


# ---------------------------------------------------------------- caching

class LRU(OrderedDict):
    def __init__(self, size: int):
        super().__init__()
        self.size = size

    def get_or(self, key, make):
        with _lock:
            if key in self:
                self.move_to_end(key)
                return self[key]
        value = make()
        with _lock:
            self[key] = value
            while len(self) > self.size:
                self.popitem(last=False)
        return value


_photos = LRU(4)
_cleaned = LRU(8)

CLEAN_FIELDS = ("invert", "lighting", "smooth", "sensitivity", "faint", "specks", "weight")


def photo_path(name: str) -> Path:
    p = (project.inbox / name).resolve()
    if p.parent != project.inbox.resolve() or not p.is_file():
        raise FileNotFoundError(name)
    return p


def photo(name: str, rotate: int = 0) -> np.ndarray:
    """The photo, rotated. Cached."""
    path = photo_path(name)
    key = (str(path), path.stat().st_mtime, rotate)
    return _photos.get_or(key, lambda: P.rotate(P.load_image(path), rotate))


def cleaned(name: str, quad, s: P.Settings, side: int, areas=None):
    """(flat, ink) for the framed page at a given size, with free-form areas applied. Cached."""
    key = (name, s.rotate, json.dumps(quad), json.dumps({k: getattr(s, k) for k in CLEAN_FIELDS}), side, json.dumps(areas or []))

    def make():
        page = P.warp(photo(name, s.rotate), quad, max_side=side)
        flat, ink = P.clean(page, s)
        return flat, P.apply_areas(ink, areas)

    return _cleaned.get_or(key, make)


def png(img: np.ndarray, headers: dict | None = None) -> Response:
    ok, buf = cv2.imencode(".png", img)
    return Response(buf.tobytes(), media_type="image/png", headers=headers or {})


async def body(request: Request) -> dict:
    return json.loads(await request.body() or b"{}")


def session_path(name: str) -> Path:
    return project.sessions / f"{Path(name).stem}.studio.json"


# ---------------------------------------------------------------- routes

async def index(_: Request):
    return FileResponse(STATIC / "index.html")


async def state(_: Request):
    project.inbox.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in project.inbox.iterdir() if p.suffix.lower() in P.IMAGE_EXTS)
    meta = catalog.meta()
    return JSONResponse({
        "project": project.root.name,
        "inbox": [{"name": p.name, "hasSession": session_path(p.name).exists()} for p in files],
        "inboxPath": str(project.inbox),
        "collections": [{"id": c["id"], "name": c["name"], "prefix": catalog.prefix(c)} for c in meta["collections"]],
        "categories": project.categories,
        "minPixels": project.min_pixels,
        "assets": [{"id": a["id"], "name": a["name"], "collection": a["collection"], "status": a["status"]}
                   for a in catalog.assets()],
        "potrace": T.available(),
        "defaults": P.Settings().to_dict(),
    })


async def photo_preview(request: Request):
    name = request.path_params["name"]
    rotate = int(request.query_params.get("rotate", 0))
    img = photo(name, rotate)
    small, scale = P.resize_max(img, PREVIEW_SIDE)
    h, w = img.shape[:2]
    ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return Response(buf.tobytes(), media_type="image/jpeg",
                    headers={"X-Width": str(w), "X-Height": str(h), "Cache-Control": "no-store"})


async def detect(request: Request):
    b = await body(request)
    img = photo(b["name"], int(b.get("rotate", 0)))
    quad, confidence = P.detect_page(img)
    return JSONResponse({"quad": quad, "confidence": confidence})


async def clean_preview(request: Request):
    b = await body(request)
    s = P.Settings.from_dict(b.get("settings"))
    flat, ink = cleaned(b["name"], b["quad"], s, PREVIEW_SIDE, b.get("areas"))
    if b.get("view") == "flat":
        out = flat
    else:
        out = np.where(ink, 0, 255).astype(np.uint8)
    h, w = out.shape
    return png(out, {"X-Width": str(w), "X-Height": str(h)})


async def pieces(request: Request):
    b = await body(request)
    s = P.Settings.from_dict(b.get("settings"))
    _, ink = cleaned(b["name"], b["quad"], s, PREVIEW_SIDE, b.get("areas"))
    boxes = P.find_pieces(ink, gap=float(b.get("gap", 0.004)), min_size=float(b.get("minSize", 0.02)))
    return JSONResponse({"boxes": [{"x": b.x, "y": b.y, "w": b.w, "h": b.h, "isolate": True} for b in boxes]})


def _piece(b: dict) -> tuple[np.ndarray, P.Settings]:
    s = P.Settings.from_dict(b.get("settings"))
    _, ink = cleaned(b["name"], b["quad"], s, FULL_SIDE, b.get("areas"))
    box = P.Box(**{k: b["box"][k] for k in ("x", "y", "w", "h")}, isolate=bool(b["box"].get("isolate", True)),
                polys=b["box"].get("polys") or None)
    return P.piece_mask(ink, box, gap=float(b.get("gap", 0.004))), s


async def trace_piece(request: Request):
    b = await body(request)
    mask, s = _piece(b)
    if mask.size == 0:
        return JSONResponse({"error": "No ink in this box"}, status_code=422)
    try:
        out = T.trace(mask, curves=s.curves)
        if b.get("backing"):
            out["backing"] = T.trace(P.backing_mask(mask), curves=s.curves)["svg"]
    except T.PotraceMissing as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    out["sourceWidth"], out["sourceHeight"] = int(mask.shape[1]), int(mask.shape[0])
    return JSONResponse(out)


async def get_session(request: Request):
    p = session_path(request.path_params["name"])
    return JSONResponse(json.loads(p.read_text("utf-8")) if p.exists() else None)


async def put_session(request: Request):
    b = await body(request)
    photo_path(b["photo"])  # validates the name
    project.sessions.mkdir(parents=True, exist_ok=True)
    session_path(b["photo"]).write_text(json.dumps(b, indent=2) + "\n", "utf-8")
    return JSONResponse({"ok": True})


async def new_collection(request: Request):
    b = await body(request)
    try:
        c = catalog.add_collection(b["name"], b.get("prefix") or None)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"id": c["id"], "name": c["name"], "prefix": catalog.prefix(c)})


async def save(request: Request):
    """Writes each approved piece: cleaned PNG original, vector SVG (+ optional backing),
    and a catalog entry (new draft, or an update of the asset it replaces)."""
    b = await body(request)
    results = []
    for piece in b["pieces"]:
        meta = piece.get("meta", {})
        try:
            mask, s = _piece({**b, "box": piece})
            if mask.size == 0:
                raise ValueError("no ink in this box")
            assets = catalog.assets()
            replaces = meta.get("replaces") or None
            if replaces:
                old = next(a for a in assets if a["id"] == replaces)
                aid, collection = old["id"], old["collection"]
            else:
                collection = meta.get("collection") or catalog.collections()[0]["id"]
                aid = meta.get("savedAs") or catalog.next_id(collection, assets)
            name = (meta.get("name") or "").strip() or f"Untitled {aid}"
            traced = T.trace(mask, curves=s.curves, title=name)
            vector = project.vectors / f"{aid}.svg"
            original = project.originals / collection / f"{aid}.png"
            vector.parent.mkdir(parents=True, exist_ok=True)
            original.parent.mkdir(parents=True, exist_ok=True)
            vector.write_text(traced["svg"], "utf-8")
            P.ink_png(mask).save(original, optimize=True)
            entry = {
                "id": aid,
                "name": name,
                "slug": catalog.unique_slug(name, assets, aid),
                "description": (meta.get("description") or "").strip(),
                "aliases": [],
                "tags": [t.strip().lower() for t in (meta.get("tags") or "").split(",") if t.strip()],
                "collection": collection,
                "sourcePath": project.rel(vector),
                "originalPath": project.rel(original),
                "format": "svg",
                "width": int(traced["svg"].split('viewBox="0 0 ')[1].split()[0]),
                "height": int(traced["svg"].split('viewBox="0 0 ')[1].split()[1].rstrip('"')),
                "viewBox": traced["svg"].split('viewBox="')[1].split('"')[0],
                "fileSize": vector.stat().st_size,
                "containsRaster": False,
                "license": "inherit",
                "version": "1.0.0",
                "status": "draft",
                "provenance": {"photo": project.rel(project.inbox / b["name"]), "session": project.rel(session_path(b["name"])),
                               "box": {k: round(piece[k], 5) for k in ("x", "y", "w", "h")},
                               "pixels": int(max(mask.shape)),
                               **({"outline": piece["polys"]} if piece.get("polys") else {})},
            }
            if project.categories:
                entry["category"] = meta.get("category") if meta.get("category") in project.categories else project.categories[0]
            if meta.get("backing"):
                backing = project.vectors / f"{aid}-backing.svg"
                backing.write_text(T.trace(P.backing_mask(mask), curves=s.curves, title=f"{name} (backing)")["svg"], "utf-8")
                entry["backingPath"] = project.rel(backing)
            saved = catalog.upsert(entry, keep_metadata=bool(replaces))
            results.append({"ok": True, "id": saved["id"], "name": saved["name"], "status": saved["status"],
                            "pixels": int(max(mask.shape)), "small": int(max(mask.shape)) < project.min_pixels,
                            "updated": bool(replaces or meta.get("savedAs")), "vector": project.rel(vector)})
        except Exception as e:  # report per piece, keep going
            results.append({"ok": False, "error": str(e), "name": meta.get("name", "")})
    return JSONResponse({"results": results})


async def not_found(_: Request, exc: Exception):
    return JSONResponse({"error": f"Not found: {exc}"}, status_code=404)


routes = [
    Route("/", index),
    Route("/api/state", state),
    Route("/api/photo/{name:path}", photo_preview),
    Route("/api/detect", detect, methods=["POST"]),
    Route("/api/clean", clean_preview, methods=["POST"]),
    Route("/api/pieces", pieces, methods=["POST"]),
    Route("/api/trace", trace_piece, methods=["POST"]),
    Route("/api/session/{name:path}", get_session),
    Route("/api/session", put_session, methods=["POST"]),
    Route("/api/collections", new_collection, methods=["POST"]),
    Route("/api/save", save, methods=["POST"]),
    Mount("/static", StaticFiles(directory=STATIC)),
]
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")


class LocalOnly:
    """Only this computer's browser, on the studio's own page, may use the API.

    The studio writes into the project, so a web page open in another tab must not be able to
    call it: requests must be addressed to localhost (stops DNS rebinding), and anything that
    changes something must come from the studio's own origin as JSON (a cross-site page can't
    send JSON to another origin without a CORS preflight, which this server never answers).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and (problem := self.refuse(scope)):
            status, text = problem
            await Response(text, status_code=status, media_type="text/plain")(scope, receive, send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    def refuse(scope) -> tuple[int, str] | None:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        host = headers.get("host", "").lower()
        name = host.split("]")[0] + "]" if host.startswith("[") else host.split(":")[0]
        if name not in LOCAL_HOSTS:
            return 403, "quick-editor only answers on localhost."
        if scope["method"] in ("GET", "HEAD"):
            return None
        origin = headers.get("origin")
        if origin is not None and origin.lower() != f"http://{host}":
            return 403, "Requests from other sites are refused."
        if not headers.get("content-type", "").startswith("application/json"):
            return 415, "Expected JSON."
        return None


app = LocalOnly(Starlette(routes=routes, exception_handlers={FileNotFoundError: not_found}))


def main():
    import uvicorn

    parser = argparse.ArgumentParser(prog="quick-editor", description="Photos of drawings in, clean vector assets out.")
    parser.add_argument("command", nargs="?", choices=["serve", "init", "refit"], default="serve",
                        help="serve (default), init (write a starter quick-editor.toml here), or refit "
                             "(re-extract drawings already in the catalog: finds them in the inbox photos, traces them)")
    parser.add_argument("ids", nargs="*", help="refit: only these asset ids (default: every published drawing that isn't traced yet)")
    parser.add_argument("--apply", action="store_true", help="refit: write the vectors and update the catalog (default: preview only)")
    parser.add_argument("--min-gain", type=float, default=1.25,
                        help="refit: use a photo only when it has at least this many times more pixels than the current image")
    parser.add_argument("--project", help="project folder or quick-editor.toml (default: found from the current folder)")
    parser.add_argument("--port", type=int, default=4400)
    parser.add_argument("--no-open", action="store_true", help="don't open the browser")
    args = parser.parse_args()

    if args.command == "init":
        path = C.init(Path(args.project or ".").resolve())
        print(f"Wrote {path}. Edit the paths, put photos in the inbox, then run quick-editor.")
        return

    if args.command == "refit":
        from . import refit
        proj = C.load(args.project)
        if not T.available():
            raise SystemExit("potrace not found. On macOS: brew install potrace")
        page = refit.run(proj, ids=args.ids or None, apply=args.apply, min_gain=args.min_gain)
        print(f"\nReview: {page}" + ("" if args.apply else "\nNothing was changed. Run again with --apply to write the vectors and update the catalog."))
        if not args.no_open:
            webbrowser.open(page.as_uri())
        return

    configure(C.load(args.project))
    project.inbox.mkdir(parents=True, exist_ok=True)
    url = f"http://localhost:{args.port}"
    where = project.config_file or f"{project.root} (no quick-editor.toml: using defaults)"
    print(f"\n  quick-editor → {url}\n  project: {where}\n  inbox:   {project.inbox}\n  potrace: {T.available() or 'NOT FOUND (brew install potrace)'}\n")
    if not args.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
