"""The catalog: a JSON record of every saved piece and the collections they belong to.

Two layouts work:
  - separate files: assets file is a JSON list, collections file is {"collections": [...]}
  - one combined file: {"collections": [...], "assets": [...]} (both settings point to it)
Missing files are created on first write.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path


class Catalog:
    def __init__(self, assets_path: Path, collections_path: Path):
        self.assets_path = assets_path
        self.collections_path = collections_path

    # ------------------------------------------------------------ files
    @staticmethod
    def _read(path: Path):
        return json.loads(path.read_text("utf-8")) if path.exists() else None

    def _write(self, path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")
        tmp.replace(path)

    # ------------------------------------------------------------ read
    def assets(self) -> list[dict]:
        data = self._read(self.assets_path)
        if data is None:
            return []
        return data if isinstance(data, list) else data.get("assets", [])

    def meta(self) -> dict:
        data = self._read(self.collections_path) or {}
        if not data.get("collections"):
            data["collections"] = [_collection("Drawings", "drawings", "d")]
        return data

    def collections(self) -> list[dict]:
        return self.meta()["collections"]

    def _save_assets(self, assets: list[dict]) -> None:
        data = self._read(self.assets_path)
        if isinstance(data, dict) or (data is None and self.assets_path == self.collections_path):
            data = data or self.meta()
            data["assets"] = assets
            self._write(self.assets_path, data)
        else:
            self._write(self.assets_path, assets)

    def _save_meta(self, meta: dict) -> None:
        if self.collections_path == self.assets_path:
            data = self._read(self.assets_path) or {}
            data.update({k: v for k, v in meta.items() if k != "assets"})
            data.setdefault("assets", [])
            self._write(self.collections_path, data)
        else:
            self._write(self.collections_path, meta)

    # ------------------------------------------------------------ write
    def add_collection(self, name: str, prefix: str | None = None) -> dict:
        meta = self.meta()
        cid = slugify(name)
        if not cid:
            raise ValueError("The collection needs a name")
        if any(c["id"] == cid for c in meta["collections"]):
            raise ValueError(f'A collection called "{name}" already exists')
        prefix = slugify(prefix or "".join(w[0] for w in name.split()) or cid)
        if any(self.prefix(c) == prefix for c in meta["collections"]):
            raise ValueError(f'The id prefix "{prefix}" is taken; pick another')
        c = _collection(name, cid, prefix)
        # a new collection starts with the same license and attribution as the existing ones
        if meta["collections"]:
            first = meta["collections"][0]
            c.update({k: first[k] for k in ("license", "licenseUrl", "attribution") if first.get(k)})
        meta["collections"].append(c)
        self._save_meta(meta)
        return c

    @staticmethod
    def prefix(collection: dict) -> str:
        return collection.get("idPrefix") or collection["id"]

    def next_id(self, collection_id: str, assets: list[dict] | None = None) -> str:
        assets = assets if assets is not None else self.assets()
        col = next(c for c in self.collections() if c["id"] == collection_id)
        p = self.prefix(col)
        nums = [int(m.group(1)) for a in assets if (m := re.fullmatch(rf"{re.escape(p)}-(\d+)", a["id"]))]
        return f"{p}-{(max(nums) + 1 if nums else 1):02d}"

    @staticmethod
    def unique_slug(name: str, assets: list[dict], own_id: str | None = None) -> str:
        base = slugify(name) or "untitled"
        taken = {a.get("slug") for a in assets if a["id"] != own_id}
        slug, n = base, 2
        while slug in taken:
            slug, n = f"{base}-{n}", n + 1
        return slug

    def upsert(self, entry: dict, keep_metadata: bool = True) -> dict:
        """Adds a new entry, or updates an existing one. With keep_metadata (replacing a
        curated asset), only the files and sizes change; its name, tags, status etc. stay."""
        assets = self.assets()
        for i, a in enumerate(assets):
            if a["id"] == entry["id"]:
                fields = ("id", "name", "slug", "description", "aliases", "tags", "category",
                          "collection", "license", "status", "related", "duplicateOf") if keep_metadata else ("id", "status")
                keep = {k: a[k] for k in fields if k in a}
                merged = {**a, **entry, **keep}
                merged["version"] = bump(a.get("version", "1.0.0"))
                if a.get("sourcePath") and a["sourcePath"] != entry.get("sourcePath"):
                    merged.setdefault("previousSources", []).append(a["sourcePath"])
                assets[i] = merged
                self._save_assets(assets)
                return merged
        assets.append(entry)
        self._save_assets(assets)
        return entry


def _collection(name: str, cid: str, prefix: str) -> dict:
    return {"id": cid, "name": name.strip(), "description": "", "license": "UNDECIDED",
            "attribution": "", "version": "0.1.0", "idPrefix": prefix}


def bump(version: str) -> str:
    major, minor, _ = (int(x) for x in version.split("."))
    return f"{major}.{minor + 1}.0"


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
