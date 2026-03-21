"""JSON-based tag store with filesystem reconciliation."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
from typing import Any

STORE_FILENAME = ".fstag.json"
_FINGERPRINT_BYTES = 8192


def _normalize(path: str) -> str:
    """Normalize path separators to forward slashes."""
    return path.replace("\\", "/")


def _fingerprint(filepath: Path) -> str:
    """Compute a fingerprint from file size and partial SHA-256."""
    try:
        stat = filepath.stat()
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            h.update(f.read(_FINGERPRINT_BYTES))
        return f"{stat.st_size}:{h.hexdigest()[:16]}"
    except OSError:
        return ""


class TagStore:
    """Manages the .fstag.json file and provides tag operations.

    The store uses POSIX-style relative paths (forward slashes) as keys,
    regardless of the host OS.  On load it reconciles with the actual
    filesystem: missing files are recovered by fingerprint matching,
    and new files are discovered.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._store_path = self.root / STORE_FILENAME
        self._tags: dict[str, dict[str, str]] = {}
        self._files: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self._batch_depth = 0
        self._load()
        self.reconcile()

    # ── persistence ──────────────────────────────────────────────

    def _load(self) -> None:
        if self._store_path.exists():
            try:
                with open(self._store_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                data = {}
        else:
            data = {}
        self._tags = data.get("tags", {})
        raw_files = data.get("files", {})
        self._files = {_normalize(k): v for k, v in raw_files.items()}

    def save(self) -> None:
        """Write to disk only if there are pending changes."""
        if not self._dirty:
            return
        data = {"tags": self._tags, "files": self._files}
        tmp = self._store_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self._store_path)
        self._dirty = False

    def _mark_dirty(self) -> None:
        self._dirty = True

    def _auto_save(self) -> None:
        """Save immediately unless inside a batch."""
        if self._batch_depth == 0:
            self.save()

    @contextlib.contextmanager
    def batch(self):
        """Defer save() until the outermost batch exits."""
        self._batch_depth += 1
        try:
            yield
        finally:
            self._batch_depth -= 1
            if self._batch_depth == 0:
                self.save()

    # ── reconciliation ───────────────────────────────────────────

    def _scan_disk(self) -> dict[str, Path]:
        """Return {normalized_relative_posix_path: absolute_path} for every file under root."""
        result: dict[str, Path] = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                abspath = Path(dirpath) / fn
                relpath = _normalize(abspath.relative_to(self.root).as_posix())
                result[relpath] = abspath
        return result

    def reconcile(self) -> None:
        """Match stored entries against the real filesystem.

        - Files still at their path: update fingerprint.
        - Files missing: try to find by fingerprint (rename/move detection).
        - New files on disk: add with empty tags.

        Only writes to disk if something actually changed.
        """
        disk_files = self._scan_disk()
        old_entries = self._files
        new_entries: dict[str, dict[str, Any]] = {}
        changed = False

        missing: list[tuple[str, dict[str, Any]]] = []

        for relpath, meta in old_entries.items():
            if relpath in disk_files:
                new_fp = _fingerprint(disk_files[relpath])
                if meta.get("fingerprint") != new_fp:
                    meta = {**meta, "fingerprint": new_fp}
                    changed = True
                new_entries[relpath] = meta
            else:
                missing.append((relpath, meta))

        # Build fingerprint index for unmatched disk files
        unmatched = {rp: ap for rp, ap in disk_files.items() if rp not in new_entries}
        fp_index: dict[str, str] = {}
        for rp, ap in unmatched.items():
            fp = _fingerprint(ap)
            if fp:
                fp_index[fp] = rp

        for old_rp, meta in missing:
            old_fp = meta.get("fingerprint", "")
            if old_fp and old_fp in fp_index:
                new_rp = fp_index.pop(old_fp)
                new_entries[new_rp] = {**meta, "fingerprint": old_fp}
                changed = True
            else:
                changed = True  # file removed

        # Add new files on disk
        for rp in disk_files:
            if rp not in new_entries:
                new_entries[rp] = {
                    "tags": [],
                    "fingerprint": _fingerprint(disk_files[rp]),
                }
                changed = True

        if changed or len(new_entries) != len(old_entries):
            self._files = new_entries
            self._mark_dirty()
            self.save()
        else:
            self._files = new_entries

    # ── tag operations ───────────────────────────────────────────

    def get_all_tags(self) -> dict[str, dict[str, str]]:
        """Return {tag_name: {"color": "#hex"}}."""
        return dict(self._tags)

    def create_tag(self, name: str, color: str = "#3b82f6") -> None:
        self._tags[name] = {"color": color}
        self._mark_dirty()
        self._auto_save()

    def rename_tag(self, old_name: str, new_name: str) -> None:
        if old_name not in self._tags:
            return
        self._tags[new_name] = self._tags.pop(old_name)
        for meta in self._files.values():
            tags = meta.get("tags", [])
            if old_name in tags:
                tags[tags.index(old_name)] = new_name
        self._mark_dirty()
        self._auto_save()

    def delete_tag(self, name: str) -> None:
        if name not in self._tags:
            return
        del self._tags[name]
        for meta in self._files.values():
            tags = meta.get("tags", [])
            if name in tags:
                tags.remove(name)
        self._mark_dirty()
        self._auto_save()

    def update_tag_color(self, name: str, color: str) -> None:
        if name in self._tags:
            self._tags[name]["color"] = color
            self._mark_dirty()
            self._auto_save()

    # ── file-tag operations ──────────────────────────────────────

    def get_files(self) -> dict[str, dict[str, Any]]:
        """Return {relpath: {"tags": [...], "fingerprint": ...}}."""
        return dict(self._files)

    def add_tag_to_file(self, relpath: str, tag_name: str) -> None:
        relpath = _normalize(relpath)
        if relpath in self._files:
            tags = self._files[relpath].setdefault("tags", [])
            if tag_name not in tags:
                tags.append(tag_name)
                self._mark_dirty()
                self._auto_save()

    def remove_tag_from_file(self, relpath: str, tag_name: str) -> None:
        relpath = _normalize(relpath)
        if relpath in self._files:
            tags = self._files[relpath].get("tags", [])
            if tag_name in tags:
                tags.remove(tag_name)
                self._mark_dirty()
                self._auto_save()

    def get_file_tags(self, relpath: str) -> list[str]:
        relpath = _normalize(relpath)
        return list(self._files.get(relpath, {}).get("tags", []))

    def refresh(self) -> None:
        """Re-scan disk and reconcile."""
        self.reconcile()
