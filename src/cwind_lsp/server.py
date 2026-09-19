"""pygls based language server wiring all CWind LSP features together."""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Optional, Protocol

from lsprotocol import types
from pygls.lsp.server import LanguageServer
from pygls.uris import from_fs_path, to_fs_path

from . import completion as completion_mod, semantic as semantic_mod, vfs
from ._version import __version__
from .analysis import AnalysisEngine, Diagnostic, Snapshot, fingerprint
from .completion import CompletionRequest
from .config import Settings
from .index import FileIndex, Occurrence
from .legend import LEGEND
from .positions import span_to_range
from .project import discover, is_dependency_path

logger = logging.getLogger("cwind_lsp")

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class _PositionParams(Protocol):
    text_document: types.TextDocumentIdentifier
    position: types.Position


class AnalysisManager:
    """Owns analysis snapshots and the debounced diagnostics worker.

    Requests for different projects may arrive concurrently (pygls runs
    handlers in a thread pool).  A global *analysis* lock serializes the
    frontend runs themselves -- its caches are process globals and not
    thread safe -- but requests coalesce per entry: the first caller for a
    stale entry performs the run, every other caller waits on the same event
    and then reads the fresh snapshot.  Requests whose entry is already
    cached never touch the lock at all.
    """

    def __init__(self, server: "CWindLanguageServer", settings: Settings):
        self.server = server
        self.settings = settings
        self.engine = AnalysisEngine(settings)
        self._analysis_lock = threading.Lock()
        self._snapshots: dict[str, Snapshot] = {}
        self._last_good: dict[str, Snapshot] = {}
        self._fingerprints: dict[str, tuple] = {}
        self._inflight: dict[str, threading.Event] = {}
        self._inflight_lock = threading.Lock()
        self._pending: dict[str, float] = {}
        self._pending_lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = False
        self._published: set[str] = set()
        self._send_lock = threading.Lock()
        self._worker = threading.Thread(
            target=self._loop, name="cwind-lsp-analysis", daemon=True
        )

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._worker.start()

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()

    # -- documents --------------------------------------------------------

    def documents(self) -> dict[str, str]:
        documents: dict[str, str] = {}
        for doc in self.server.workspace.text_documents.values():
            path = to_fs_path(doc.uri)
            if path is None:
                continue
            documents[vfs.normalize(path)] = doc.source
        return documents

    # -- snapshots ---------------------------------------------------------

    def cached(self, path: str) -> Optional[Snapshot]:
        context = discover(path, self.settings)
        return self._snapshots.get(context.entry)

    def last_good(self, path: str) -> Optional[Snapshot]:
        context = discover(path, self.settings)
        return self._last_good.get(context.entry)

    def require_snapshot(self, path: str) -> Snapshot:
        snapshot = self.snapshot_for(path)
        if snapshot is None:  # pragma: no cover - wait=None never gives up
            raise RuntimeError("analysis did not produce a snapshot")
        return snapshot

    def snapshot_for(
        self, path: str, *, wait: Optional[float] = None
    ) -> Optional[Snapshot]:
        context = discover(path, self.settings)
        deadline: Optional[float] = None
        while True:
            documents = self.documents()
            current = fingerprint(context.entry, documents)
            cached = self._snapshots.get(context.entry)
            if cached is not None and self._fingerprints.get(context.entry) == current:
                return cached
            owner = False
            with self._inflight_lock:
                event = self._inflight.get(context.entry)
                if event is None:
                    event = threading.Event()
                    self._inflight[context.entry] = event
                    owner = True
            if owner:
                try:
                    with self._analysis_lock:
                        documents = self.documents()
                        current = fingerprint(context.entry, documents)
                        cached = self._snapshots.get(context.entry)
                        if (
                            cached is not None
                            and self._fingerprints.get(context.entry) == current
                        ):
                            return cached
                        vfs.replace_documents(documents)
                        try:
                            snapshot = self.engine.analyze(context.entry, documents)
                        except Exception as exc:  # analysis must never wedge requests
                            logger.exception("analysis crashed for %s", context.entry)
                            snapshot = Snapshot(
                                entry=context.entry,
                                project=context,
                                internal_error=f"{type(exc).__name__}: {exc}",
                            )
                        self._snapshots[context.entry] = snapshot
                        self._fingerprints[context.entry] = current
                        if _is_clean(snapshot):
                            self._last_good[context.entry] = snapshot
                        return snapshot
                finally:
                    with self._inflight_lock:
                        self._inflight.pop(context.entry, None)
                    event.set()
            timeout = None
            if wait is not None:
                remaining = wait if deadline is None else max(deadline - time.monotonic(), 0.0)
                if deadline is None:
                    deadline = time.monotonic() + wait
                timeout = remaining
                if remaining <= 0:
                    return cached
            event.wait(timeout)
            if timeout is not None and not event.is_set() and cached is not None:
                self.schedule(path)
                return cached

    def analyze_documents(self, entry: str, documents: dict[str, str]) -> Snapshot:
        with self._analysis_lock:
            vfs.replace_documents(documents)
            return self.engine.analyze(entry, documents)

    def invalidate(self, path: str) -> None:
        context = discover(path, self.settings)
        self._snapshots.pop(context.entry, None)
        self._fingerprints.pop(context.entry, None)

    # -- debounced diagnostics ----------------------------------------------

    def schedule(self, path: str) -> None:
        context = discover(path, self.settings)
        with self._pending_lock:
            self._pending[context.entry] = time.monotonic()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stopping:
            self._wake.wait(timeout=0.5)
            self._wake.clear()
            if self._stopping:
                return
            self._drain_pending()

    def _drain_pending(self) -> None:
        debounce = max(self.settings.debounce_ms, 0) / 1000.0
        while not self._stopping:
            with self._pending_lock:
                items = dict(self._pending)
            if not items:
                return
            now = time.monotonic()
            due = [entry for entry, stamp in items.items() if now - stamp >= debounce]
            if not due:
                time.sleep(min(0.1, debounce + 0.01))
                continue
            with self._pending_lock:
                for entry in due:
                    self._pending.pop(entry, None)
            for entry in due:
                try:
                    snapshot = self.require_snapshot(entry)
                    self.publish(snapshot)
                except Exception:  # pragma: no cover - diagnostics never crash
                    logger.exception("analysis failed for %s", entry)

    # -- diagnostics ---------------------------------------------------------

    def publish(self, snapshot: Snapshot) -> None:
        if not self.settings.diagnostics:
            return
        open_docs = self.documents()
        targets = set(open_docs) | self._published
        for path in sorted(targets):
            uri = self.uri_for_path(path)
            if not uri:
                continue
            lines = self._lines_for(snapshot, path, open_docs)
            data = snapshot.diagnostics.get(path, [])
            limit = max(self.settings.max_problems, 1)
            diagnostics = [
                self._to_lsp(diag, lines) for diag in data[:limit]
            ]
            if len(data) > limit:
                diagnostics.append(
                    types.Diagnostic(
                        range=types.Range(
                            start=types.Position(0, 0), end=types.Position(0, 1)
                        ),
                        message=f"{len(data) - limit} more problems suppressed",
                        severity=types.DiagnosticSeverity.Information,
                        source="cwind",
                    )
                )
            self._notify_publish(uri, diagnostics)
        self._published = set(open_docs)

    def publish_empty(self, uri: str) -> None:
        self._notify_publish(uri, [])
        path = to_fs_path(uri)
        if path is not None:
            self._published.discard(vfs.normalize(path))

    def _lines_for(
        self, snapshot: Snapshot, path: str, documents: dict[str, str]
    ) -> list[str]:
        index = snapshot.files.get(path)
        if index is not None:
            return index.source.splitlines(True)
        return (documents.get(path) or "").splitlines(True)

    def _to_lsp(self, diagnostic: Diagnostic, lines: list[str]) -> types.Diagnostic:
        codec = self.server.workspace.position_codec
        return types.Diagnostic(
            range=span_to_range(diagnostic.span, lines, codec),
            message=diagnostic.message,
            severity=(
                types.DiagnosticSeverity.Error
                if diagnostic.severity == 1
                else types.DiagnosticSeverity.Warning
            ),
            source=diagnostic.source,
            code=diagnostic.code,
        )

    def uri_for_path(self, path: str) -> str:
        normalized = vfs.normalize(path)
        for doc in self._open_documents():
            candidate = to_fs_path(doc.uri)
            if candidate is not None and vfs.normalize(candidate) == normalized:
                return doc.uri
        return from_fs_path(path) or ""

    def _open_documents(self):
        return list(self.server.workspace.text_documents.values())

    def _notify_publish(self, uri: str, diagnostics: list[types.Diagnostic]) -> None:
        with self._send_lock:
            self.server.text_document_publish_diagnostics(
                types.PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics)
            )


def _degraded(snapshot: Snapshot, path: str) -> bool:
    if snapshot.internal_error:
        return True
    normalized = vfs.normalize(path)
    if normalized not in snapshot.files:
        return True
    return any(
        diagnostic.stage in ("lex", "parse", "internal")
        for diagnostic in snapshot.diagnostics.get(normalized, ())
    )


def _is_clean(snapshot: Snapshot) -> bool:
    if snapshot.internal_error:
        return False
    for path, diagnostics in snapshot.diagnostics.items():
        if not diagnostics:
            continue
        index = snapshot.files.get(path)
        if index is not None and index.is_dependency:
            continue
        return False
    return True


class CWindLanguageServer(LanguageServer):
    settings: Settings
    manager: AnalysisManager

    def __init__(self, name: str = "cwind-lsp", version: str = __version__):
        super().__init__(name, version)
        self.settings = Settings()
        self.manager = AnalysisManager(self, self.settings)

    def setup(self, settings: Optional[Settings] = None) -> None:
        if settings is not None:
            self.settings = settings
        try:
            from cwind_frontend import home

            home.reset_install_root_cache()
        except Exception:
            logger.exception("failed to reset frontend install root cache")
        vfs.install()
        self.manager.stop()
        self.manager = AnalysisManager(self, self.settings)
        self.manager.start()

    # -- helpers -----------------------------------------------------------

    def text_document(self, uri: str):
        return self.workspace.get_text_document(uri)

    def position_params(
        self, params: _PositionParams
    ) -> Optional[tuple[str, int, int]]:
        path = to_fs_path(params.text_document.uri)
        if path is None:
            return None
        document = self.text_document(params.text_document.uri)
        server_position = document.position_from_client_units(params.position)
        return path, server_position.line, server_position.character

    def snapshot_index_occurrence(
        self, path: str, line: int, character: int
    ) -> tuple[Snapshot, Optional[FileIndex], Optional[Occurrence]]:
        snapshot = self.manager.require_snapshot(path)
        index = snapshot.files.get(vfs.normalize(path))
        occurrence = index.occurrence_at(line, character) if index else None
        return snapshot, index, occurrence

    def source_lines(self, snapshot: Snapshot, path: str) -> list[str]:
        index = snapshot.files.get(vfs.normalize(path))
        if index is not None:
            return index.source.splitlines(True)
        uri = self.manager.uri_for_path(path)
        if uri:
            document = self.workspace.text_documents.get(uri)
            if document is not None:
                return document.source.splitlines(True)
        try:
            return open(path, encoding="utf-8").read().splitlines(True)
        except OSError:
            return []


def _location(server: CWindLanguageServer, snapshot: Snapshot, occurrence: Occurrence) -> types.Location:
    uri = server.manager.uri_for_path(occurrence.file)
    lines = server.source_lines(snapshot, occurrence.file)
    return types.Location(
        uri=uri,
        range=span_to_range(
            occurrence.span, lines, server.workspace.position_codec
        ),
    )


def register_features(server: CWindLanguageServer) -> None:
    @server.feature(types.INITIALIZE)
    def on_initialize(ls: CWindLanguageServer, params: types.InitializeParams):
        settings = Settings.from_options(params.initialization_options)
        ls.setup(settings)
        logger.info("cwind-lsp initialized (std_path=%s)", settings.std_path)
        ls.window_log_message(
            types.LogMessageParams(
                type=types.MessageType.Info,
                message=f"CWind language server {__version__} ready",
            )
        )

    @server.feature(types.SHUTDOWN)
    def on_shutdown(ls: CWindLanguageServer, *args):
        ls.manager.stop()

    @server.feature(types.TEXT_DOCUMENT_DID_OPEN)
    def on_did_open(ls: CWindLanguageServer, params: types.DidOpenTextDocumentParams):
        path = to_fs_path(params.text_document.uri)
        if path is not None:
            ls.manager.schedule(path)

    @server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
    def on_did_change(ls: CWindLanguageServer, params: types.DidChangeTextDocumentParams):
        path = to_fs_path(params.text_document.uri)
        if path is not None:
            ls.manager.schedule(path)

    @server.feature(types.TEXT_DOCUMENT_DID_CLOSE)
    def on_did_close(ls: CWindLanguageServer, params: types.DidCloseTextDocumentParams):
        path = to_fs_path(params.text_document.uri)
        if path is None:
            return
        vfs.remove_document(path)
        ls.manager.publish_empty(params.text_document.uri)
        ls.manager.invalidate(path)

    @server.feature(
        types.TEXT_DOCUMENT_COMPLETION,
        types.CompletionOptions(
            trigger_characters=[".", ":"],
            resolve_provider=False,
        ),
    )
    @server.thread()
    def on_completion(
        ls: CWindLanguageServer, params: types.CompletionParams
    ) -> Optional[types.CompletionList]:
        if not ls.settings.completion:
            return None
        located = ls.position_params(params)
        if located is None:
            return None
        path, line, character = located
        document = ls.text_document(params.text_document.uri)

        def probe(text: str, word_start: int) -> Optional[Snapshot]:
            context = discover(path, ls.settings)
            documents = ls.manager.documents()
            documents[vfs.normalize(path)] = (
                text[:word_start] + "__cwind_lsp_probe__" + text[word_start:]
            )
            return ls.manager.analyze_documents(context.entry, documents)

        snapshot = ls.manager.snapshot_for(path, wait=0.5)
        fallback = ls.manager.last_good(path)
        if snapshot is None or _degraded(snapshot, path):
            if fallback is not None:
                snapshot = fallback
        if snapshot is None:
            return None
        items = completion_mod.complete(
            CompletionRequest(
                snapshot=snapshot,
                path=path,
                line=line,
                character=character,
                text=document.source,
                probe=probe,
            )
        )
        return types.CompletionList(is_incomplete=False, items=items)

    @server.feature(types.TEXT_DOCUMENT_HOVER)
    @server.thread()
    def on_hover(
        ls: CWindLanguageServer, params: types.HoverParams
    ) -> Optional[types.Hover]:
        if not ls.settings.hover:
            return None
        located = ls.position_params(params)
        if located is None:
            return None
        path, line, character = located
        snapshot, index, occurrence = ls.snapshot_index_occurrence(path, line, character)
        if occurrence is None:
            return None
        markdown = snapshot.hover_markdown(occurrence)
        if not markdown:
            return None
        lines = ls.source_lines(snapshot, path)
        return types.Hover(
            contents=types.MarkupContent(
                kind=types.MarkupKind.Markdown, value=markdown
            ),
            range=span_to_range(
                occurrence.span, lines, ls.workspace.position_codec
            ),
        )

    @server.feature(types.TEXT_DOCUMENT_DEFINITION)
    @server.thread()
    def on_definition(
        ls: CWindLanguageServer, params: types.DefinitionParams
    ) -> Optional[list[types.Location]]:
        located = ls.position_params(params)
        if located is None:
            return None
        path, line, character = located
        snapshot, index, occurrence = ls.snapshot_index_occurrence(path, line, character)
        if occurrence is None:
            return None
        definitions = snapshot.find_definition(occurrence)
        if not definitions:
            return None
        return [_location(ls, snapshot, definition) for definition in definitions]

    @server.feature(types.TEXT_DOCUMENT_REFERENCES)
    @server.thread()
    def on_references(
        ls: CWindLanguageServer, params: types.ReferenceParams
    ) -> Optional[list[types.Location]]:
        located = ls.position_params(params)
        if located is None:
            return None
        path, line, character = located
        snapshot, index, occurrence = ls.snapshot_index_occurrence(path, line, character)
        if occurrence is None:
            return None
        include_declaration = bool(
            params.context and params.context.include_declaration
        )
        references = snapshot.find_references(
            occurrence, include_declaration=include_declaration
        )
        if not references:
            return None
        return [_location(ls, snapshot, reference) for reference in references]

    @server.feature(types.TEXT_DOCUMENT_PREPARE_RENAME)
    @server.thread()
    def on_prepare_rename(
        ls: CWindLanguageServer, params: types.PrepareRenameParams
    ) -> Optional[types.Range]:
        located = ls.position_params(params)
        if located is None:
            return None
        path, line, character = located
        snapshot, index, occurrence = ls.snapshot_index_occurrence(path, line, character)
        if occurrence is None or not _renamable(snapshot, occurrence):
            return None
        lines = ls.source_lines(snapshot, path)
        return span_to_range(occurrence.span, lines, ls.workspace.position_codec)

    @server.feature(types.TEXT_DOCUMENT_RENAME)
    @server.thread()
    def on_rename(
        ls: CWindLanguageServer, params: types.RenameParams
    ) -> Optional[types.WorkspaceEdit]:
        new_name = params.new_name
        if not _IDENTIFIER_RE.match(new_name):
            ls.window_show_message(
                types.ShowMessageParams(
                    type=types.MessageType.Warning,
                    message=f"'{new_name}' is not a valid CWind identifier",
                )
            )
            return None
        if _is_keyword(new_name):
            ls.window_show_message(
                types.ShowMessageParams(
                    type=types.MessageType.Warning,
                    message=f"'{new_name}' is a reserved keyword",
                )
            )
            return None
        located = ls.position_params(params)
        if located is None:
            return None
        path, line, character = located
        snapshot, index, occurrence = ls.snapshot_index_occurrence(path, line, character)
        if occurrence is None or not _renamable(snapshot, occurrence):
            ls.window_show_message(
                types.ShowMessageParams(
                    type=types.MessageType.Warning,
                    message="cannot rename this symbol",
                )
            )
            return None
        references = [
            reference
            for reference in snapshot.find_references(occurrence)
            if not _is_dependency(snapshot, reference)
        ]
        if not references:
            return None
        changes: dict[str, list[types.TextEdit]] = {}
        for reference in references:
            uri = ls.manager.uri_for_path(reference.file)
            if not uri:
                continue
            lines = ls.source_lines(snapshot, reference.file)
            changes.setdefault(uri, []).append(
                types.TextEdit(
                    range=span_to_range(
                        reference.span, lines, ls.workspace.position_codec
                    ),
                    new_text=new_name,
                )
            )
        if not changes:
            return None
        return types.WorkspaceEdit(changes=changes)

    @server.feature(
        types.TEXT_DOCUMENT_SEMANTIC_TOKENS_FULL,
        LEGEND,
    )
    @server.thread()
    def on_semantic_tokens_full(
        ls: CWindLanguageServer, params: types.SemanticTokensParams
    ) -> Optional[types.SemanticTokens]:
        if not ls.settings.semantic_tokens:
            return None
        path = to_fs_path(params.text_document.uri)
        if path is None:
            return None
        document = ls.text_document(params.text_document.uri)
        snapshot = ls.manager.require_snapshot(path)
        index = snapshot.files.get(vfs.normalize(path))
        return semantic_mod.semantic_tokens(
            path, document.source, ls.workspace.position_codec, index
        )


def _renamable(snapshot: Snapshot, occurrence: Occurrence) -> bool:
    if occurrence.key is None or occurrence.role in ("keyword", "builtin"):
        return False
    resolved = snapshot.resolve_key(occurrence.key)
    if resolved is None:
        return False
    if _is_dependency(snapshot, occurrence):
        return False
    if resolved[0] == "node":
        definition = snapshot.decl_by_id.get(resolved[1])
        if definition is not None and _is_dependency(snapshot, definition):
            return False
        return True
    if resolved[0] == "macro":
        definition = snapshot.macro_defs.get(resolved)
        return definition is not None and not _is_dependency(snapshot, definition)
    return False


def _is_dependency(snapshot: Snapshot, occurrence: Occurrence) -> bool:
    index = snapshot.files.get(occurrence.file)
    if index is not None:
        return index.is_dependency
    return is_dependency_path(occurrence.file, snapshot.project)


def _is_keyword(name: str) -> bool:
    try:
        from cwind_frontend.lexer import KEYWORDS

        return name in KEYWORDS
    except Exception:
        return False


def create_server(settings: Optional[Settings] = None) -> CWindLanguageServer:
    server = CWindLanguageServer()
    register_features(server)
    if settings is not None:
        server.setup(settings)
    return server
