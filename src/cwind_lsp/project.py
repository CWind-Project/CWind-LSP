"""Project/entry discovery and file classification."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import vfs
from .config import Settings

CONFIG_NAME = "cwind-lsp.toml"
_CONFIG_CACHE: dict[str, tuple[float, dict]] = {}


def project_config(root: Optional[str]) -> dict:
    """Load the optional per-project ``cwind-lsp.toml`` (cached by mtime).

    Both a top-level table and a ``[cwind]`` table are accepted; keys mirror
    the ``initializationOptions.cwind`` object.
    """
    if not root:
        return {}
    path = Path(root) / CONFIG_NAME
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return {}
    key = vfs.normalize(path)
    cached = _CONFIG_CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}
    table = data.get("cwind")
    if not isinstance(table, dict):
        table = data
    _CONFIG_CACHE[key] = (stamp, dict(table))
    return _CONFIG_CACHE[key][1]


@dataclass
class ProjectContext:
    file: str
    entry: str
    root: Optional[str] = None
    manifest_error: Optional[str] = None

    @property
    def is_project(self) -> bool:
        return self.root is not None


def _is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def install_root() -> Optional[str]:
    try:
        from cwind_frontend import home

        root = home.install_root()
    except Exception:
        return None
    return vfs.normalize(root) if root is not None else None


def dependency_roots() -> list[str]:
    """Roots whose files are considered read-only dependencies (std/pkgs)."""
    root = install_root()
    if root is None:
        return []
    return [
        os.path.join(root, "libs"),
        os.path.join(root, "pkgs"),
    ]


def is_dependency_path(path: str, context: Optional[ProjectContext] = None) -> bool:
    norm = vfs.normalize(path)
    if context is not None and context.root is not None:
        if _is_within(norm, os.path.join(context.root, "libs")):
            return True
        if not _is_within(norm, context.root):
            for root in dependency_roots():
                if _is_within(norm, root):
                    return True
            return False
    for root in dependency_roots():
        if _is_within(norm, root):
            return True
    return False


def is_editable(path: str, context: Optional[ProjectContext]) -> bool:
    return not is_dependency_path(path, context)


def _manifest_entry(manifest_path: Path) -> Optional[Path]:
    try:
        from cwind_frontend import breeze

        manifest = breeze.load_manifest(manifest_path)
    except Exception:
        return None
    for candidate in manifest.entry_candidates():
        if candidate.is_file():
            return candidate
    return None


def discover(path: str, settings: Optional[Settings] = None) -> ProjectContext:
    """Resolve the analysis entry (project root file) for *path*."""
    norm = vfs.normalize(path)
    if settings is not None:
        override = settings.entry_override(norm)
        if override:
            entry = vfs.normalize(override)
            root = None
            manifest = find_manifest(entry)
            if manifest is not None:
                root = vfs.normalize(manifest.parent)
            return ProjectContext(file=norm, entry=entry, root=root)

    manifest_path = find_manifest(norm)
    if manifest_path is not None:
        entry_path = _manifest_entry(manifest_path)
        if entry_path is not None:
            return ProjectContext(
                file=norm,
                entry=vfs.normalize(entry_path),
                root=vfs.normalize(manifest_path.parent),
            )
    return ProjectContext(file=norm, entry=norm)


def config_for(path: str) -> dict:
    """Nearest ``cwind-lsp.toml`` walking upward from *path*, if any."""
    current = Path(vfs.normalize(path))
    if current.is_file() or not current.exists():
        current = current.parent
    for candidate in (current, *current.parents):
        config = project_config(vfs.normalize(candidate))
        if config:
            return config
    return {}


def find_manifest(path: str) -> Optional[Path]:
    try:
        from cwind_frontend import breeze

        return breeze.find_manifest(path)
    except Exception:
        return None


def libs_root(context: Optional[ProjectContext] = None) -> Optional[str]:
    """The std tree used for a context: project ``libs/`` else install root."""
    if context is not None and context.root is not None:
        local = Path(context.root) / "libs"
        if local.is_dir():
            return vfs.normalize(local)
    root = install_root()
    if root is not None:
        candidate = Path(root) / "libs"
        if candidate.is_dir():
            return vfs.normalize(candidate)
    return None


def project_root_for(path: str) -> Optional[str]:
    """Nearest ancestor owning ``libs/`` or ``Breeze.toml`` (frontend rule)."""
    current = Path(vfs.normalize(path)).parent
    for candidate in (current, *current.parents):
        if (candidate / "libs").is_dir() or (candidate / "Breeze.toml").is_file():
            return vfs.normalize(candidate)
    return None
