"""Project configuration: where photos come from and where results go.

quick-editor looks for a `quick-editor.toml` in the current folder or any parent (or the one
given with --project). Without one it works in the current folder with simple defaults.
All paths are relative to the folder that holds the config file.

    [paths]
    inbox = "scans/inbox"            # photos to process
    sessions = "scans"               # per-photo progress (<photo>.studio.json)
    vectors = "assets/vector"        # <id>.svg and <id>-backing.svg
    originals = "assets/originals"   # <collection>/<id>.png (cleaned ink, full resolution)

    [catalog]
    assets = "data/assets.json"            # a JSON list of entries, or {"assets": [...]}
    collections = "data/collections.json"  # {"collections": [...]}
    categories = []                   # optional: a "Kind" field for each drawing
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = "quick-editor.toml"

DEFAULT_TOML = """# quick-editor project settings. Paths are relative to this file.
[paths]
inbox = "inbox"
sessions = "sessions"
vectors = "out/vector"
originals = "out/originals"

[catalog]
# One file holding {"collections": [...], "assets": [...]}; created on first save.
assets = "out/catalog.json"
collections = "out/catalog.json"
# categories = ["..."]  # optional: adds a "Kind" field to each drawing

[quality]
# A drawing smaller than this (long side, in pixels of the straightened photo) loses detail:
# fine lines merge and edges get blocky. quick-editor warns and records the size in the catalog.
min_pixels = 600
"""


@dataclass
class Project:
    root: Path
    inbox: Path
    sessions: Path
    vectors: Path
    originals: Path
    assets_json: Path
    collections_json: Path
    categories: list[str] = field(default_factory=list)
    min_pixels: int = 600
    config_file: Path | None = None

    def rel(self, p: Path) -> str:
        """Path as stored in the catalog: relative to the project root, with forward slashes."""
        return p.resolve().relative_to(self.root.resolve()).as_posix()


def find_config(start: Path) -> Path | None:
    for d in [start, *start.parents]:
        if (d / CONFIG_NAME).is_file():
            return d / CONFIG_NAME
    return None


def load(project: str | Path | None = None, cwd: Path | None = None) -> Project:
    cwd = Path(cwd or Path.cwd()).resolve()
    if project:
        p = Path(project).expanduser().resolve()
        config = p if p.is_file() else (p / CONFIG_NAME if (p / CONFIG_NAME).is_file() else None)
        root = p.parent if p.is_file() else p
    else:
        config = find_config(cwd)
        root = config.parent if config else cwd
    data = tomllib.loads(config.read_text("utf-8")) if config else tomllib.loads(DEFAULT_TOML)
    paths = data.get("paths", {})
    cat = data.get("catalog", {})
    defaults = tomllib.loads(DEFAULT_TOML)
    get = lambda section, key: (data.get(section, {}).get(key) or defaults[section][key])
    return Project(
        root=root,
        inbox=root / get("paths", "inbox"),
        sessions=root / get("paths", "sessions"),
        vectors=root / get("paths", "vectors"),
        originals=root / get("paths", "originals"),
        assets_json=root / get("catalog", "assets"),
        collections_json=root / get("catalog", "collections"),
        categories=list(cat.get("categories") or []),
        min_pixels=int(data.get("quality", {}).get("min_pixels") or defaults["quality"]["min_pixels"]),
        config_file=config,
    )


def init(folder: Path) -> Path:
    """Writes a starter quick-editor.toml (if there isn't one) and returns its path."""
    target = folder / CONFIG_NAME
    if not target.exists():
        target.write_text(DEFAULT_TOML, "utf-8")
    return target
