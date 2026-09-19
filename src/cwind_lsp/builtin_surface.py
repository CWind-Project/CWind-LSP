"""Syntax-only index of the std builtin declaration files.

SA prunes unreachable std declarations (todo-188 reachability pruning), so the
surviving program often lacks the ``extern "CWind"`` blocks declaring builtin
types and their methods.  Completion and go-to-definition still need that
surface; parsing the declaration files standalone (no project anchor, no
prelude) is cheap and side-effect free, so it runs once per libs fingerprint.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from cwind_frontend.ast_components.ast import (
    ExternBlock,
    ExtraDecl,
    FnDecl,
    ImplDecl,
    Node,
    TraitDecl,
)
from cwind_frontend.lexer import lex_with_errors
from cwind_frontend.parser import parse_with_errors

from .formatting import render_signature
from .index import FileIndex, Occurrence, build_file_index, walk_nodes
from .vfs import normalize


@dataclass
class BuiltinSurface:
    files: dict[str, FileIndex] = field(default_factory=dict)
    type_decls: dict[str, list[Occurrence]] = field(default_factory=dict)
    methods: dict[str, list[FnDecl]] = field(default_factory=dict)
    method_occurrences: dict[str, list[Occurrence]] = field(default_factory=dict)

    def method_candidates(self, owner: str) -> list[tuple[str, str]]:
        return [
            (method.name, render_signature(method))
            for method in self.methods.get(owner, ())
        ]


_CACHE: dict[tuple, BuiltinSurface] = {}
_LOCK = threading.Lock()


def load(libs_root: Optional[str]) -> BuiltinSurface:
    if not libs_root:
        return BuiltinSurface()
    root = Path(libs_root)
    paths: list[Path] = []
    for sub in ("builtins", "expansion", "traits"):
        directory = root / sub
        if not directory.is_dir():
            continue
        for pattern in ("*.wind", "*.wd"):
            paths.extend(sorted(directory.rglob(pattern)))
    if not paths:
        return BuiltinSurface()
    fingerprint_parts = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        fingerprint_parts.append((str(path), stat.st_size, stat.st_mtime_ns))
    fingerprint = (vfs_normalize_root(root), tuple(fingerprint_parts))
    with _LOCK:
        cached = _CACHE.get(fingerprint)
        if cached is not None:
            return cached
    surface = _parse_all(paths)
    with _LOCK:
        _CACHE.clear()
        _CACHE[fingerprint] = surface
    return surface


def vfs_normalize_root(root: Path) -> str:
    return normalize(root)


def _parse_all(paths: list[Path]) -> BuiltinSurface:
    surface = BuiltinSurface()
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        lexed = lex_with_errors(text)
        try:
            parsed = parse_with_errors(lexed.tokens, flush_cache=False)
        except Exception:
            continue
        items = [item for item in parsed.program.items if isinstance(item, Node)]
        index = build_file_index(
            normalize(path), text, lexed.tokens, items, is_dependency=True
        )
        surface.files[normalize(path)] = index
        for occurrence in index.declarations:
            if occurrence.role in ("struct", "enum", "interface", "type"):
                surface.type_decls.setdefault(occurrence.name, []).append(occurrence)
            if occurrence.role == "method":
                surface.method_occurrences.setdefault(occurrence.name, []).append(
                    occurrence
                )
        for node in items:
            for member in walk_nodes(node):
                if isinstance(member, ExternBlock):
                    for fn in member.fns:
                        if fn.cwind_owner is not None:
                            owner = _owner_name(fn.cwind_owner)
                            surface.methods.setdefault(owner, []).append(fn)
                elif isinstance(member, (ExtraDecl, ImplDecl)):
                    owner = _owner_name(member.struct)
                    surface.methods.setdefault(owner, []).extend(member.methods)
                elif isinstance(member, TraitDecl):
                    surface.methods.setdefault(member.name, []).extend(member.methods)
    for owner, methods in surface.methods.items():
        unique: dict[str, FnDecl] = {}
        for method in methods:
            unique.setdefault(method.name, method)
        surface.methods[owner] = list(unique.values())
    return surface


def _owner_name(type_node) -> str:
    name = getattr(type_node, "name", "") or ""
    return str(name).split("::")[-1]
