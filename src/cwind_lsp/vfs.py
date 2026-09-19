"""In-process virtual file overlay for unsaved editor buffers.

The CWind frontend reads imported modules straight from disk (``Path.read_text``,
``Path.read_bytes``).  The LSP keeps an overlay of open, possibly dirty buffers
and patches those two entry points for the lifetime of the server process:
reads for overlaid paths return the in-memory text, everything else falls
through to the original implementation.

``Path.resolve`` is additionally memoised.  On Windows ``ntpath.realpath``
performs a ``_getfinalpathname`` syscall per path component, which dominates
frontend parse time; resolution results are stable for the lifetime of an
editor session.
"""

from __future__ import annotations

import os
import pathlib
import threading
from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator, Mapping

_OVERLAY: dict[str, str] = {}
_LOCK = threading.RLock()

_INSTALLED = False
_ORIG_READ_TEXT = pathlib.Path.read_text
_ORIG_READ_BYTES = pathlib.Path.read_bytes
_ORIG_RESOLVE = pathlib.Path.resolve


def normalize(path) -> str:
    """Return a canonical, case-folded absolute path usable as a dict key.

    Resolution goes through the same memoised ``Path.resolve`` the frontend
    uses, so short (8.3) Windows spellings and symlinked paths collapse onto
    the canonical spelling the parser stamps on ``source_module``.
    """
    try:
        candidate = pathlib.Path(path)
    except (TypeError, ValueError):
        return str(path)
    try:
        resolved = _cached_resolve(candidate, False)
    except OSError:
        resolved = pathlib.Path(os.path.abspath(os.fspath(path)))
    return os.path.normcase(os.fspath(resolved))


def replace_documents(mapping: Mapping[str, str]) -> None:
    """Atomically replace the overlay contents."""
    normalized = {normalize(path): text for path, text in mapping.items()}
    with _LOCK:
        _OVERLAY.clear()
        _OVERLAY.update(normalized)


def set_document(path, text: str) -> None:
    with _LOCK:
        _OVERLAY[normalize(path)] = text


def remove_document(path) -> None:
    with _LOCK:
        _OVERLAY.pop(normalize(path), None)


def get_document(path) -> str | None:
    with _LOCK:
        return _OVERLAY.get(normalize(path))


@contextmanager
def overlay(mapping: Mapping[str, str]) -> Iterator[None]:
    with _LOCK:
        saved = dict(_OVERLAY)
        _OVERLAY.clear()
        _OVERLAY.update({normalize(path): text for path, text in mapping.items()})
    try:
        yield
    finally:
        with _LOCK:
            _OVERLAY.clear()
            _OVERLAY.update(saved)


def _read_text(self: pathlib.Path, encoding=None, errors=None):
    with _LOCK:
        data = _OVERLAY.get(normalize(self))
    if data is not None:
        return data
    return _ORIG_READ_TEXT(self, encoding=encoding, errors=errors)


def _read_bytes(self: pathlib.Path):
    with _LOCK:
        data = _OVERLAY.get(normalize(self))
    if data is not None:
        return data.encode("utf-8")
    return _ORIG_READ_BYTES(self)


@lru_cache(maxsize=16384)
def _cached_resolve(path: pathlib.Path, strict: bool):
    return _ORIG_RESOLVE(path, strict=strict)


def _resolve(self: pathlib.Path, strict: bool = False):
    return _cached_resolve(self, strict)


def install(fast_resolve: bool | None = None) -> None:
    """Install the overlay (and optionally the resolve memo) once."""
    global _INSTALLED
    if _INSTALLED:
        return
    pathlib.Path.read_text = _read_text  # type: ignore[method-assign]
    pathlib.Path.read_bytes = _read_bytes  # type: ignore[method-assign]
    if fast_resolve is None:
        fast_resolve = os.environ.get("CWIND_LSP_NO_FAST_RESOLVE", "") not in {
            "1",
            "true",
            "yes",
        }
    if fast_resolve:
        pathlib.Path.resolve = _resolve  # type: ignore[method-assign]
    _INSTALLED = True
