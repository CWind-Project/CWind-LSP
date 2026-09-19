"""Analysis pipeline: run the CWind frontend and build a queryable snapshot."""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from cwind_frontend.ast_components.ast import (
    ConstDecl,
    EnumDecl,
    ExtraDecl,
    Field,
    FnDecl,
    Node,
    StructDecl,
    TraitDecl,
    Type,
    TypeDecl,
    UseDecl,
    Variant,
)
from cwind_frontend.lexer import lex_with_errors
from cwind_frontend.parser import parse_with_errors
from cwind_frontend.sa import run_sa_with_errors

from . import vfs
from .config import Settings
from .builtin_surface import BuiltinSurface
from .formatting import render_declaration, render_signature
from .formatting import render_type as render_type_node
from .index import (
    FileIndex,
    Occurrence,
    build_file_index,
    scan_macro_definitions,
)
from .legend import MOD
from .positions import Span, span_from_error
from .project import (
    ProjectContext,
    config_for,
    discover,
    is_dependency_path,
    libs_root,
)


@dataclass
class ParseConfig:
    target_os: Optional[str] = None
    target_arch: Optional[str] = None
    target_vendor: Optional[str] = None
    target_pointer_width: Optional[str] = None
    no_std: bool = False


def _config_str(data: dict, key: str, fallback: Optional[str]) -> Optional[str]:
    value = data.get(key)
    return str(value) if value is not None else fallback


@dataclass
class Diagnostic:
    file: str
    span: Span
    message: str
    severity: int = 1
    code: Optional[str] = None
    source: str = "cwind"
    stage: str = "sa"


@dataclass
class CompletionCandidate:
    label: str
    kind: int
    detail: str = ""
    insert_text: Optional[str] = None
    sort_text: str = ""


@dataclass
class Snapshot:
    entry: str
    project: ProjectContext
    files: dict[str, FileIndex] = field(default_factory=dict)
    diagnostics: dict[str, list[Diagnostic]] = field(default_factory=dict)
    node_file: dict[int, str] = field(default_factory=dict)
    node_by_id: dict[int, Node] = field(default_factory=dict)
    decl_by_id: dict[int, Occurrence] = field(default_factory=dict)
    occurrences_by_key: dict[tuple, list[Occurrence]] = field(default_factory=dict)
    occurrences_by_subject: dict[tuple, list[Occurrence]] = field(default_factory=dict)
    symbols_by_name: dict[str, list[Occurrence]] = field(default_factory=dict)
    binding_to_fn: dict[int, int] = field(default_factory=dict)
    trait_methods: dict[tuple[str, str], int] = field(default_factory=dict)
    alias_exports: dict[tuple[str, str], frozenset] = field(default_factory=dict)
    module_items: dict[tuple[str, ...], frozenset] = field(default_factory=dict)
    module_alias_map: dict[tuple[str, ...], tuple[str, ...]] = field(default_factory=dict)
    methods_by_owner: dict[str, list[FnDecl]] = field(default_factory=dict)
    fields_by_owner: dict[str, list[Field]] = field(default_factory=dict)
    variants_by_owner: dict[str, list[Variant]] = field(default_factory=dict)
    assoc_consts_by_owner: dict[str, list[ConstDecl]] = field(default_factory=dict)
    visible_by_file: dict[str, frozenset] = field(default_factory=dict)
    macro_defs: dict[tuple, Occurrence] = field(default_factory=dict)
    macro_defs_by_name: dict[str, list[Occurrence]] = field(default_factory=dict)
    builtin_surface: Optional["BuiltinSurface"] = None
    internal_error: Optional[str] = None
    created: float = field(default_factory=time.time)

    # -- key resolution ---------------------------------------------------

    def resolve_key(self, key: Optional[tuple]) -> Optional[tuple]:
        if key is None:
            return None
        if key[0] == "binding":
            fn_id = self.binding_to_fn.get(key[1])
            return ("node", fn_id) if fn_id is not None else key
        if key[0] in ("type", "symbol_name"):
            node_id = self._unique_declaration(key[1], type_only=key[0] == "type")
            return ("node", node_id) if node_id is not None else key
        if key[0] == "variant_name":
            for node in self.variants_by_owner.get(key[2], ()):
                if node.name != key[3]:
                    continue
                nid = getattr(node, "_typed_id", None)
                if isinstance(nid, int) and nid in self.decl_by_id:
                    return ("node", nid)
            return key
        if key[0] == "variant":
            nodes = self.variants_by_owner.get(key[2], ())
            if 0 <= key[3] < len(nodes):
                nid = getattr(nodes[key[3]], "_typed_id", None)
                if isinstance(nid, int) and nid in self.decl_by_id:
                    return ("node", nid)
            return key
        if key[0] == "macro_name":
            named = self.macro_defs_by_name.get(key[1], ())
            keys = {definition.key for definition in named}
            if len(keys) == 1:
                return next(iter(keys))
            return key
        if key[0] == "trait_method":
            bare = _bare_name(key[1])
            found = self.trait_methods.get((bare, key[2]))
            if found is None:
                for (trait, member), fn_id in self.trait_methods.items():
                    if trait == bare and member == key[2]:
                        found = fn_id
                        break
            return ("node", found) if found is not None else key
        return key

    def _unique_declaration(self, name: str, *, type_only: bool) -> Optional[int]:
        roles = {"struct", "enum", "interface", "type", "typeParameter"}
        candidates: set[int] = set()
        for occurrence in self.symbols_by_name.get(name, ()):
            if not occurrence.is_decl or occurrence.key is None:
                continue
            if occurrence.key[0] != "node":
                continue
            if type_only and occurrence.role not in roles:
                continue
            candidates.add(occurrence.key[1])
            if len(candidates) > 1:
                return None
        return next(iter(candidates)) if len(candidates) == 1 else None

    def occurrences_for(self, key: Optional[tuple]) -> list[Occurrence]:
        resolved = self.resolve_key(key)
        if resolved is None:
            return []
        return list(self.occurrences_by_key.get(resolved, ()))

    # -- navigation --------------------------------------------------------

    def find_definition(self, occurrence: Occurrence) -> list[Occurrence]:
        resolved = self.resolve_key(occurrence.key)
        if resolved is not None:
            if resolved[0] == "node":
                target = self.decl_by_id.get(resolved[1])
                if target is not None and target is not occurrence:
                    return [target]
            elif resolved[0] == "builtin":
                found = self._builtin_definition(str(resolved[1]), occurrence)
                if found:
                    return found
            elif resolved[0] == "type":
                found = self._type_definition(
                    str(resolved[1]),
                    preferred=occurrence.file,
                    module_hint=self._type_hint(occurrence),
                )
                if found:
                    return found
            elif resolved[0] == "module":
                found = self._module_definition(resolved[1])
                if found:
                    return found
            elif resolved[0] == "macro":
                definition = self.macro_defs.get(resolved)
                if definition is not None:
                    return [definition]
                named = self.macro_defs_by_name.get(occurrence.name, ())
                if named:
                    return list(named[:1])
            elif resolved[0] == "symbol_name":
                found = self._symbol_definition(occurrence, str(resolved[1]))
                if found:
                    return found
        if occurrence.is_decl:
            return [occurrence]
        if occurrence.role == "namespace":
            module = self._module_definition((occurrence.name,))
            if module:
                return module
        if occurrence.role == "method":
            return self._builtin_method_definition(occurrence.name)
        if occurrence.role in ("struct", "enum", "interface", "type"):
            return self._builtin_type_definition(occurrence.name)
        return []

    def _symbol_definition(self, occurrence: Occurrence, name: str) -> list[Occurrence]:
        qualifier: tuple = ()
        for subject in occurrence.subjects:
            if subject and subject[0] == "use_prefix":
                qualifier = tuple(subject[1])
                break
        if qualifier:
            module = self._module_definition(qualifier + (name,))
            if module:
                return module
            holder = self.module_file(qualifier)
            if holder is not None:
                named = [
                    occ
                    for occ in self.symbols_by_name.get(name, ())
                    if occ.is_decl and occ.file == holder
                ]
                if named:
                    return named[:1]
        found = self._type_definition(name, preferred=occurrence.file)
        if found:
            return found
        named_macro = self.macro_defs_by_name.get(name, ())
        if len({definition.key for definition in named_macro}) == 1:
            return [named_macro[0]]
        module = self._module_definition((name,))
        if module:
            return module
        return []

    def _builtin_definition(self, name: str, occurrence: Occurrence) -> list[Occurrence]:
        candidates = self.symbols_by_name.get(name, [])
        found = [occ for occ in candidates if occ.is_decl][:1]
        if found:
            return found
        if occurrence.role == "method":
            return self._builtin_method_definition(name)
        return self._builtin_type_definition(name)

    def _builtin_method_definition(self, name: str) -> list[Occurrence]:
        if self.builtin_surface is None:
            return []
        return list(self.builtin_surface.method_occurrences.get(name, ()))[:1]

    def _builtin_type_definition(self, name: str) -> list[Occurrence]:
        if self.builtin_surface is None:
            return []
        return list(self.builtin_surface.type_decls.get(name, ()))[:1]

    def _type_hint(self, occurrence: Occurrence) -> Optional[str]:
        node = occurrence.node
        if node is None:
            return None
        info = (getattr(node, "_typed_ann", None) or {}).get("type")
        if isinstance(info, dict):
            hint = info.get("def")
            return str(hint) if hint else None
        return None

    def _type_definition(
        self,
        name: str,
        *,
        preferred: Optional[str] = None,
        module_hint: Optional[str] = None,
    ) -> list[Occurrence]:
        candidates = [
            occ
            for occ in self.symbols_by_name.get(name, ())
            if occ.is_decl and occ.role in {"struct", "enum", "interface", "type", "typeParameter"}
        ]
        if not candidates:
            candidates = [
                occ for occ in self.symbols_by_name.get(name, ()) if occ.is_decl
            ]
        if candidates:
            if module_hint:
                file = self.module_file(str(module_hint).split("::"))
                if file is not None:
                    targeted = [occ for occ in candidates if occ.file == file]
                    if targeted:
                        return targeted[:1]
            if preferred:
                same_file = [occ for occ in candidates if occ.file == preferred]
                if same_file:
                    return same_file[:1]
            local = [
                occ
                for occ in candidates
                if (index := self.files.get(occ.file)) is None or not index.is_dependency
            ]
            if local:
                return local[:1]
            return candidates[:1]
        return self._builtin_type_definition(name)

    def _module_definition(self, module) -> list[Occurrence]:
        path = module if isinstance(module, tuple) else None
        if not path:
            return []
        file = self.module_file(path)
        if file is None:
            file = self._module_file_guess(path[-1])
        if file is None and path and path[0] == "std":
            file = self._std_root_file()
        if file is None:
            return []
        index = self.files.get(file)
        if index is not None:
            first = next(
                (occ for occ in index.declarations if occ.role == "namespace"), None
            )
            if first is not None:
                return [first]
        return [
            Occurrence(
                file=file,
                span=Span(0, 0, 0, 1),
                name=path[-1],
                role="namespace",
                modifiers=MOD["declaration"] | MOD["definition"],
                is_decl=True,
                decl_kind="mod",
            )
        ]

    def module_file(self, parts: Iterable[str]) -> Optional[str]:
        parts = tuple(parts)
        resolved = self.module_alias_map.get(parts, parts)
        file = self._module_files.get(resolved)
        if file is not None:
            return file
        for module_parts, candidate in self._module_files.items():
            if module_parts[-len(parts) :] == parts:
                return candidate
        return None

    def _std_root_file(self) -> Optional[str]:
        root = libs_root(self.project)
        if not root:
            return None
        for suffix in (".wind", ".wd", ".cwind", ".cwd"):
            candidate = Path(root) / f"mod{suffix}"
            if candidate.is_file():
                return vfs.normalize(candidate)
        return None

    def _module_file_guess(self, name: str) -> Optional[str]:
        """Locate a module file by name around the project when it never entered
        the compile surface (e.g. a module reached only through a macro import)."""
        roots: list[Path] = []
        entry = Path(self.entry)
        roots.extend([entry.parent, entry.parent.parent])
        if self.project.root:
            root = Path(self.project.root)
            roots.extend([root, root / "src"])
        suffixes = (".wind", ".wd", ".cwind", ".cwd")
        seen: set[str] = set()
        for root in roots:
            if not root.is_dir():
                continue
            for suffix in suffixes:
                candidates = [root / f"{name}{suffix}", root / name / f"mod{suffix}"]
                for candidate in candidates:
                    key = vfs.normalize(candidate)
                    if key in seen:
                        continue
                    seen.add(key)
                    if candidate.is_file():
                        return key
        return None

    # populated during build
    _module_files: dict[tuple[str, ...], str] = field(default_factory=dict)

    # -- hover -------------------------------------------------------------

    def hover_markdown(self, occurrence: Occurrence) -> Optional[str]:
        if occurrence.role == "macro" and occurrence.node is None:
            definitions = self.find_definition(occurrence)
            if not definitions:
                return f"*macro* `{occurrence.name}`"
            target = definitions[0]
            location = f"`{Path(target.file).name}:{target.span.line + 1}`"
            if occurrence.is_decl:
                return (
                    f"```cwind\n{occurrence.name}!(...)\n```\n\n"
                    f"*Declarative macro*"
                )
            return (
                f"```cwind\n{occurrence.name}!(...)\n```\n\n"
                f"*Macro*, defined at {location}"
            )
        node = occurrence.node
        parts: list[str] = []
        target = None
        if not occurrence.is_decl:
            definition = self.find_definition(occurrence)
            if definition:
                target = definition[0].node
        if target is None and occurrence.is_decl:
            target = node
        if isinstance(target, FnDecl):
            parts.append(f"```cwind\n{self.signature(target)}\n```")
        elif isinstance(target, (StructDecl, EnumDecl, TraitDecl, TypeDecl)):
            parts.append(f"```cwind\n{self.declaration_text(target)}\n```")
        if node is not None:
            info = (getattr(node, "_typed_ann", None) or {}).get("type")
            rendered = format_type_info(info)
            if rendered:
                parts.append(f"*{occurrence.role}*: `{rendered}`")
        if not parts:
            parts.append(f"`{occurrence.name}` ({occurrence.role})")
        return "\n\n".join(parts)

    def type_of_occurrence(self, occurrence: Occurrence) -> Optional[str]:
        node = occurrence.node
        if node is None:
            return None
        info = (getattr(node, "_typed_ann", None) or {}).get("type")
        return format_type_info(info)

    def signature(self, fn: FnDecl) -> str:
        return render_signature(fn)

    def declaration_text(self, node: Node) -> str:
        return render_declaration(node)

    def render_type(self, node: Optional[Type]) -> str:
        return render_type_node(node)

    # -- members -----------------------------------------------------------

    def members_of(self, type_info: Any) -> list[CompletionCandidate]:
        name = _type_base_name(type_info)
        if not name:
            return []
        candidates: list[CompletionCandidate] = []
        seen: set[str] = set()

        def add(label: str, kind: int, detail: str = "") -> None:
            if label in seen:
                return
            seen.add(label)
            candidates.append(CompletionCandidate(label=label, kind=kind, detail=detail))

        for field_node in self.fields_by_owner.get(name, ()):
            detail = self.render_type(field_node.type) if field_node.type is not None else ""
            add(field_node.name, 5, detail)  # CompletionItemKind.Field
        for const_node in self.assoc_consts_by_owner.get(name, ()):
            add(const_node.name, 21, self.render_type(const_node.type))
        for variant in self.variants_by_owner.get(name, ()):
            add(variant.name, 20, "")  # EnumMember
        for method in self.methods_by_owner.get(name, ()):
            add(method.name, 2, self.signature(method))  # Method
        if self.builtin_surface is not None:
            for label, detail in self.builtin_surface.method_candidates(name):
                add(label, 2, detail)
        return candidates

    def path_candidates_for_type(self, name: str) -> list[CompletionCandidate]:
        candidates: list[CompletionCandidate] = []
        for method in self.methods_by_owner.get(name, ()):
            candidates.append(
                CompletionCandidate(method.name, 3, self.signature(method))
            )
        for const_node in self.assoc_consts_by_owner.get(name, ()):
            candidates.append(
                CompletionCandidate(const_node.name, 21, self.render_type(const_node.type))
            )
        for variant in self.variants_by_owner.get(name, ()):
            candidates.append(CompletionCandidate(variant.name, 20, ""))
        if self.builtin_surface is not None:
            for label, detail in self.builtin_surface.method_candidates(name):
                candidates.append(CompletionCandidate(label, 2, detail))
        return candidates

    def module_candidates(self, parts: Iterable[str]) -> list[CompletionCandidate]:
        requested = tuple(parts)
        resolved = self.module_alias_map.get(requested, requested)
        names: set[str] = set()
        for module_parts, items in self.module_items.items():
            if (
                module_parts[: len(resolved)] == resolved
                and len(module_parts) == len(resolved) + 1
            ):
                names.add(module_parts[-1])
        if resolved in self.module_items:
            names |= set(self.module_items[resolved])
        if resolved == ("std", "builtins") and self.builtin_surface is not None:
            names |= set(self.builtin_surface.type_decls)
        candidates: list[CompletionCandidate] = []
        for name in sorted(names):
            role = self.decl_role_for_name(name)
            candidates.append(
                CompletionCandidate(name, _completion_kind_for_role(role), role)
            )
        return candidates

    def decl_role_for_name(self, name: str) -> str:
        if name in self.macro_defs_by_name:
            return "macro"
        for occ in self.symbols_by_name.get(name, ()):
            if occ.is_decl:
                return occ.role
        return "namespace"

    # -- references --------------------------------------------------------

    def find_references(
        self, occurrence: Occurrence, *, include_declaration: bool = True
    ) -> list[Occurrence]:
        if occurrence.key is None and not occurrence.subjects:
            return [occurrence] if include_declaration else []
        collected: dict[tuple, Occurrence] = {}
        queue: list[Occurrence] = [occurrence]
        while queue:
            current = queue.pop()
            key = (
                current.file,
                current.span.line,
                current.span.character,
                current.name,
            )
            if key in collected:
                continue
            collected[key] = current
            queue.extend(self._related(current))
        found = list(collected.values())
        if not include_declaration:
            found = [occ for occ in found if not occ.is_decl]
        found.sort(key=lambda occ: (occ.file, occ.span.line, occ.span.character))
        return found

    def _related(self, occurrence: Occurrence) -> list[Occurrence]:
        related: list[Occurrence] = []
        resolved = self.resolve_key(occurrence.key)
        if resolved is not None:
            related.extend(self.occurrences_by_key.get(resolved, ()))
        for subject in occurrence.subjects:
            related.extend(self.occurrences_by_subject.get(subject, ()))
        return related


def _bare_name(name: Any) -> str:
    return str(name).split("::")[-1]


def _bare_type(name: str) -> str:
    name = str(name)
    if name.startswith("std::builtins::"):
        name = name[len("std::builtins::") :]
    if "::" in name and not name.startswith(("*", "[", "fn")):
        name = name.split("::")[-1]
    return name


def _type_base_name(type_info: Any) -> Optional[str]:
    if not isinstance(type_info, dict):
        return None
    name = type_info.get("name")
    if not isinstance(name, str):
        return None
    if name.startswith(("*", "[", "fn")):
        return None
    return _bare_name(name)


def format_type_info(type_info: Any) -> str:
    if not isinstance(type_info, dict):
        return ""
    name = type_info.get("name")
    if not isinstance(name, str):
        return ""
    text = _bare_type(name)
    args = type_info.get("args")
    if isinstance(args, list) and args:
        rendered = [format_type_info(arg) for arg in args]
        rendered = [item for item in rendered if item]
        if rendered:
            text += "<" + ", ".join(rendered) + ">"
    if type_info.get("ref"):
        text = "&" + ("mut " if type_info.get("mut") else "") + text
    if type_info.get("alias"):
        text = f"{type_info['alias']}({text})"
    return text


def _completion_kind_for_role(role: str) -> int:
    return {
        "struct": 22,
        "enum": 13,
        "interface": 8,
        "type": 25,
        "function": 3,
        "method": 2,
        "variable": 6,
        "property": 10,
        "enumMember": 20,
        "namespace": 9,
        "typeParameter": 25,
    }.get(role, 6)


def _ensure_sa_source_patch() -> None:
    from cwind_frontend.sa.analyzer import _Analyzer

    if getattr(_Analyzer, "_cwind_lsp_patched", False):
        return
    original_error = _Analyzer._record_error
    original_warning = _Analyzer._record_warning

    def _record_error(self, message, line, column):
        before = len(self.errors)
        before_std = len(self.std_errors)
        original_error(self, message, line, column)
        if len(self.errors) > before:
            self.errors[-1].source = self.current_module
        elif len(self.std_errors) > before_std:
            self.std_errors[-1].source = self.current_module

    def _record_warning(self, message, line, column):
        before = len(self.warnings)
        original_warning(self, message, line, column)
        if len(self.warnings) > before:
            self.warnings[-1].source = self.current_module

    _Analyzer._record_error = _record_error
    _Analyzer._record_warning = _record_warning
    setattr(_Analyzer, "_cwind_lsp_patched", True)


_SA_PATCH_LOCK = threading.Lock()


class AnalysisEngine:
    """Runs parse + SA for an entry file and returns a :class:`Snapshot`."""

    def __init__(self, settings: Optional[Settings] = None, max_cache: int = 6):
        self.settings = settings or Settings()
        self.settings.apply_environment()
        self._cache: OrderedDict[tuple, Snapshot] = OrderedDict()
        self._max_cache = max_cache
        self._surfaces: dict[str, BuiltinSurface] = {}
        vfs.install()
        with _SA_PATCH_LOCK:
            _ensure_sa_source_patch()

    def builtin_surface(self, context: ProjectContext) -> BuiltinSurface:
        from .builtin_surface import load

        root = libs_root(context) or ""
        surface = self._surfaces.get(root)
        if surface is None:
            surface = load(root or None)
            self._surfaces[root] = surface
        return surface

    def analyze(self, entry: str, documents: Mapping[str, str]) -> Snapshot:
        entry = vfs.normalize(entry)
        documents = {vfs.normalize(k): v for k, v in documents.items()}
        key = fingerprint(entry, documents)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        snapshot = self._analyze(entry, documents)
        self._cache[key] = snapshot
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_cache:
            self._cache.popitem(last=False)
        return snapshot

    def _analyze(self, entry: str, documents: Mapping[str, str]) -> Snapshot:
        context = discover(entry, self.settings)
        entry_text = _read_source(context.entry, documents)
        if entry_text is None:
            message = f"cannot read {context.entry}"
            diagnostics = {context.entry: [_internal_diagnostic(context.entry, message)]}
            return Snapshot(
                entry=context.entry,
                project=context,
                diagnostics=diagnostics,
                internal_error=message,
            )
        lexed = lex_with_errors(entry_text)
        diagnostics: dict[str, list[Diagnostic]] = {}
        for error in lexed.errors:
            _append_diag(diagnostics, context.entry, error, 1, stage="lex")
        for warning in lexed.warnings:
            _append_lex_warning(diagnostics, context.entry, warning)
        config = self._parse_config(context)
        try:
            parsed = parse_with_errors(
                lexed.tokens,
                source_path=context.entry,
                target_os=config.target_os,
                target_arch=config.target_arch,
                target_vendor=config.target_vendor,
                target_pointer_width=config.target_pointer_width,
                package_lib=self._package_lib(context),
                no_std=config.no_std,
                flush_cache=True,
            )
        except Exception as exc:  # the server must survive frontend failures
            message = f"{type(exc).__name__}: {exc}"
            diagnostics.setdefault(context.entry, []).append(
                _internal_diagnostic(context.entry, message)
            )
            return Snapshot(
                entry=context.entry,
                project=context,
                diagnostics=diagnostics,
                internal_error=message,
            )
        for error in parsed.errors:
            _append_diag(diagnostics, error.source or context.entry, error, 1, stage="parse")
        program = parsed.program
        sa_result = None
        try:
            sa_result = run_sa_with_errors(program)
        except Exception as exc:
            return self._build_snapshot(
                context, program, None, diagnostics, internal_error=f"{type(exc).__name__}: {exc}",
                documents=documents,
            )
        for error in sa_result.errors:
            _append_diag(diagnostics, error.source or context.entry, error, 1)
        for warning in sa_result.warnings:
            _append_diag(
                diagnostics,
                getattr(warning, "source", None) or context.entry,
                warning,
                2,
            )
        return self._build_snapshot(
            context,
            program,
            sa_result.info,
            diagnostics,
            documents=documents,
        )

    def _parse_config(self, context: ProjectContext) -> "ParseConfig":
        data = config_for(context.entry)
        pointer = data.get("target_pointer_width")
        return ParseConfig(
            target_os=_config_str(data, "target_os", self.settings.target_os),
            target_arch=_config_str(data, "target_arch", self.settings.target_arch),
            target_vendor=_config_str(data, "target_vendor", self.settings.target_vendor),
            target_pointer_width=(
                str(pointer)
                if pointer is not None
                else self.settings.target_pointer_width
            ),
            no_std=bool(data.get("no_std", self.settings.no_std)),
        )

    def _package_lib(self, context: ProjectContext):
        if context.root is None:
            return None
        try:
            from cwind_frontend import breeze

            manifest = breeze.load_manifest(Path(context.root) / "Breeze.toml")
        except Exception:
            return None
        if getattr(manifest.entry, "is_lib", False):
            return None
        lib = manifest.lib_path()
        if not lib.is_file():
            return None
        if vfs.normalize(lib) == context.entry:
            return None
        return ([manifest.name], str(lib))

    def _build_snapshot(
        self,
        context: ProjectContext,
        program: Any,
        info: Any,
        diagnostics: dict[str, list[Diagnostic]],
        *,
        documents: Mapping[str, str],
        internal_error: Optional[str] = None,
    ) -> Snapshot:
        if internal_error and not any(
            diag.code == "internal" for diag in diagnostics.get(context.entry, ())
        ):
            diagnostics.setdefault(context.entry, []).append(
                _internal_diagnostic(context.entry, internal_error)
            )
        snapshot = Snapshot(
            entry=context.entry,
            project=context,
            diagnostics=diagnostics,
            internal_error=internal_error,
        )
        grouped = _group_items_by_file(program, context.entry)
        _merge_module_programs(grouped, program, context)
        for path, items in grouped.items():
            source = _read_source(path, documents)
            if source is None:
                continue
            lexed = lex_with_errors(source)
            dependency = is_dependency_path(path, context)
            index = build_file_index(
                path, source, lexed.tokens, items, is_dependency=dependency
            )
            snapshot.files[path] = index
            for nid, node in index.nodes.items():
                snapshot.node_by_id[nid] = node
                snapshot.node_file[nid] = path
        if info is not None:
            snapshot.binding_to_fn = {
                binding.id: binding.fn_id for binding in getattr(info, "bindings", ())
            }
        _collect_owners(snapshot)
        snapshot.trait_methods = _collect_trait_methods(snapshot)
        _index_macros(snapshot, program, context.entry)
        for index in snapshot.files.values():
            for occurrence in index.occurrences:
                if occurrence.is_decl:
                    nid = (
                        occurrence.node._typed_id  # type: ignore[union-attr]
                        if occurrence.node is not None
                        else None
                    )
                    if isinstance(nid, int):
                        snapshot.decl_by_id[nid] = occurrence
                    snapshot.symbols_by_name.setdefault(occurrence.name, []).append(
                        occurrence
                    )
        for index in snapshot.files.values():
            for occurrence in index.occurrences:
                resolved = snapshot.resolve_key(occurrence.key)
                if resolved is not None:
                    snapshot.occurrences_by_key.setdefault(resolved, []).append(
                        occurrence
                    )
                for subject in occurrence.subjects:
                    snapshot.occurrences_by_subject.setdefault(subject, []).append(
                        occurrence
                    )
        _collect_modules(snapshot, program)
        snapshot.builtin_surface = self.builtin_surface(context)
        for name, declarations in snapshot.builtin_surface.type_decls.items():
            snapshot.symbols_by_name.setdefault(name, []).extend(declarations)
        if program is not None:
            table = getattr(program, "_module_table", None) or {}
            for path, row in table.items():
                if not isinstance(row, dict):
                    continue
                visible = row.get("visible")
                if isinstance(visible, frozenset):
                    snapshot.visible_by_file[vfs.normalize(path)] = visible
        return snapshot


def fingerprint(entry: str, documents: Mapping[str, str]) -> tuple:
    digest = hashlib.blake2b(digest_size=16)
    for path in sorted(documents):
        digest.update(path.encode("utf-8", "surrogatepass"))
        digest.update(b"\0")
        digest.update(documents[path].encode("utf-8", "surrogatepass"))
        digest.update(b"\0")
    return (entry, digest.hexdigest())


def _read_source(path: str, documents: Mapping[str, str]) -> Optional[str]:
    text = documents.get(vfs.normalize(path))
    if text is not None:
        return text
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None


def _home_of(item: Node, entry: str) -> str:
    home = getattr(item, "source_module", None)
    if isinstance(home, str) and home:
        return vfs.normalize(home)
    return entry


def _group_items_by_file(program: Any, entry: str) -> dict[str, list[Node]]:
    grouped: dict[str, list[Node]] = {}
    if program is None:
        return grouped
    for item in getattr(program, "items", ()) or ():
        if not isinstance(item, Node):
            continue
        grouped.setdefault(_home_of(item, entry), []).append(item)
    return grouped


def _merge_module_programs(
    grouped: dict[str, list[Node]], program: Any, context: ProjectContext
) -> None:
    """Fold each loaded module file's full item list into the per-file view.

    The flat program only carries the *selected* import surface of a module
    (declarations); ``use`` statements and other non-exported items live in
    the per-file program.  Merging by node identity keeps one index per file
    while making import lines (and their rename targets) visible.  Dependency
    files that never reached the compile surface are skipped: the builtin
    surface already covers their declarations and full std indexing would
    only slow the common case down.
    """
    programs = getattr(program, "_module_file_programs", None) or {}
    for path, child in programs.items():
        key = vfs.normalize(path)
        if key not in grouped and is_dependency_path(key, context):
            continue
        items = grouped.setdefault(key, [])
        seen = {id(item) for item in items}
        for item in getattr(child, "items", ()) or ():
            if not isinstance(item, Node) or id(item) in seen:
                continue
            home = getattr(item, "source_module", None)
            if isinstance(home, str) and home and vfs.normalize(home) != key:
                continue
            seen.add(id(item))
            items.append(item)


def _append_diag(
    diagnostics: dict[str, list[Diagnostic]],
    path: str,
    error: Any,
    severity: int,
    *,
    stage: str = "sa",
) -> None:
    norm = vfs.normalize(path) if path else ""
    if not norm:
        return
    diagnostics.setdefault(norm, []).append(
        Diagnostic(
            file=norm,
            span=span_from_error(error),
            message=str(getattr(error, "message", error)),
            severity=severity,
            code=getattr(error, "category", None),
            stage=stage,
        )
    )


def _internal_diagnostic(path: str, message: str) -> Diagnostic:
    return Diagnostic(
        file=path,
        span=Span(0, 0, 0, 1),
        message=f"CWind server: {message}",
        severity=1,
        code="internal",
        stage="internal",
    )


def _append_lex_warning(diagnostics: dict[str, list[Diagnostic]], path: str, warning: Any) -> None:
    diag = Diagnostic(
        file=path,
        span=span_from_error(warning),
        message=str(getattr(warning, "message", warning)),
        severity=2,
        stage="lex",
    )
    diagnostics.setdefault(vfs.normalize(path), []).append(diag)


def _index_macros(snapshot: Snapshot, program: Any, entry: str) -> None:
    """Inject macro definitions (token scan + records) and call sites.

    Declarative definitions are stripped by the macro preprocessor and calls
    are replaced by their expansion, so neither appears in the AST.  The raw
    token stream carries the definition heads; ``program._macro_records``
    carries both definition anchors and call-site coordinates.
    """
    records = list(getattr(program, "_macro_records", ()) or ()) if program is not None else []
    defs_by_key = snapshot.macro_defs
    defs_by_name = snapshot.macro_defs_by_name

    def register(occurrence: Occurrence, line: int, column: int) -> None:
        key = ("macro", occurrence.file, line, column)
        if key in defs_by_key:
            return
        occurrence.key = key
        defs_by_key[key] = occurrence
        defs_by_name.setdefault(occurrence.name, []).append(occurrence)
        index = snapshot.files.get(occurrence.file)
        if index is not None:
            index.add_occurrence(occurrence)

    for index in list(snapshot.files.values()):
        for occurrence in scan_macro_definitions(index):
            register(occurrence, occurrence.span.line + 1, occurrence.span.character + 1)

    for record in records:
        if record.get("kind") != "definition":
            continue
        source = record.get("source")
        name = str(record.get("macro") or "")
        line = int(record.get("line") or 0)
        column = int(record.get("column") or 0)
        if not source or not name or line <= 0:
            continue
        span = Span(line - 1, column - 1, line - 1, column - 1 + len(name))
        register(
            Occurrence(
                file=vfs.normalize(source),
                span=span,
                name=name,
                role="macro",
                modifiers=MOD["declaration"] | MOD["definition"],
                is_decl=True,
                decl_kind="macro",
            ),
            line,
            column,
        )

    seen: set[tuple] = set()
    for record in records:
        kind = record.get("kind")
        if kind not in ("expansion", "unknown_macro"):
            continue
        name = str(record.get("macro") or "")
        source = record.get("source") or entry
        line = int(record.get("line") or 0)
        column = int(record.get("column") or 0)
        if not name or line <= 0:
            continue
        call_file = vfs.normalize(source)
        spot = (call_file, line, column)
        if spot in seen:
            continue
        seen.add(spot)
        end_line = int(record.get("end_line") or line)
        end_column = int(record.get("end_column") or (column + len(name)))
        span = Span(line - 1, column - 1, end_line - 1, max(end_column - 1, column))
        key: tuple = ("macro_name", name)
        if kind == "expansion":
            def_line = int(record.get("def_line") or 0)
            def_column = int(record.get("def_column") or 0)
            def_source = record.get("def_source")
            if def_source and def_line > 0:
                key = ("macro", vfs.normalize(str(def_source)), def_line, def_column)
            elif def_line > 0:
                for definition_key, definition in defs_by_key.items():
                    if (
                        definition.name == name
                        and definition_key[2] == def_line
                        and definition_key[3] == def_column
                    ):
                        key = definition_key
                        break
        occurrence = Occurrence(
            file=call_file,
            span=span,
            name=name,
            role="macro",
            key=key,
        )
        index = snapshot.files.get(call_file)
        if index is not None:
            index.add_occurrence(occurrence)


def _collect_trait_methods(snapshot: Snapshot) -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for index in snapshot.files.values():
        for node in index.nodes.values():
            if not isinstance(node, TraitDecl):
                continue
            trait = _bare_name(node.name)
            for method in node.methods:
                nid = getattr(method, "_typed_id", None)
                if isinstance(nid, int):
                    found.setdefault((trait, method.name), nid)
    return found


def _collect_owners(snapshot: Snapshot) -> None:
    from cwind_frontend.ast_components.ast import ExternBlock, ImplDecl

    for index in snapshot.files.values():
        for node in index.nodes.values():
            if isinstance(node, StructDecl):
                snapshot.fields_by_owner.setdefault(_bare_name(node.name), []).extend(
                    node.fields
                )
            elif isinstance(node, EnumDecl):
                snapshot.variants_by_owner.setdefault(_bare_name(node.name), []).extend(
                    node.variants
                )
            elif isinstance(node, ExtraDecl):
                owner = _bare_name(node.struct.name) if node.struct is not None else ""
                if owner:
                    snapshot.assoc_consts_by_owner.setdefault(owner, []).extend(node.consts)
                    snapshot.methods_by_owner.setdefault(owner, []).extend(node.methods)
            elif isinstance(node, TraitDecl):
                snapshot.methods_by_owner.setdefault(_bare_name(node.name), []).extend(
                    node.methods
                )
            elif isinstance(node, ImplDecl):
                owner = _bare_name(node.struct.name) if node.struct is not None else ""
                if owner:
                    snapshot.methods_by_owner.setdefault(owner, []).extend(node.methods)
            elif isinstance(node, ExternBlock):
                for fn in node.fns:
                    if fn.cwind_owner is None:
                        continue
                    owner = _bare_name(fn.cwind_owner.name)
                    snapshot.methods_by_owner.setdefault(owner, []).append(fn)


def _collect_modules(snapshot: Snapshot, program: Any) -> None:
    module_names: dict[tuple[str, ...], set[str]] = {}
    for path, index in snapshot.files.items():
        module_parts: Optional[tuple[str, ...]] = None
        for item in index.nodes.values():
            parts = getattr(item, "source_module_path", None)
            if isinstance(parts, list) and parts:
                module_parts = tuple(str(part) for part in parts)
                break
        if module_parts is not None:
            names = {
                occurrence.name
                for occurrence in index.declarations
                if occurrence.role != "namespace"
            }
            module_names.setdefault(module_parts, set()).update(names)
            snapshot._module_files.setdefault(module_parts, path)
        for item in index.nodes.values():
            if isinstance(item, UseDecl):
                alias = item.alias or (item.parts[-1] if item.parts else None)
                if not alias:
                    continue
                exported = getattr(item, "exported_names", None)
                if isinstance(exported, frozenset):
                    snapshot.alias_exports[(path, alias)] = exported
    snapshot.module_items = {
        parts: frozenset(names) for parts, names in module_names.items()
    }
    alias_map: dict[tuple[str, ...], tuple[str, ...]] = {}
    ordered = sorted(module_names, key=lambda key: ("std" in key[:1], key))
    for module_parts in ordered:
        alias_map.setdefault(module_parts, module_parts)
        for start in range(1, len(module_parts)):
            alias_map.setdefault(module_parts[start:], module_parts)
    snapshot.module_alias_map = alias_map
