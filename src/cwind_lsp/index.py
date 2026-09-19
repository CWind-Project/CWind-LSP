"""AST/token indexing: identifier occurrences, keys, spans.

The frontend AST only carries start positions; tokens carry exact half-open
spans.  This module walks the post-SA AST of one file and emits
:class:`Occurrence` records for declarations and references, resolving each
occurrence to a stable *key* (declaration node id, method binding id, trait
method, module, type name or builtin) so that definition/references/rename can
group them.  Node token spans are computed on demand and cached.
"""

from __future__ import annotations

import bisect
import dataclasses
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Optional

from cwind_frontend import Token
from cwind_frontend.ast_components.ast import (
    AssocTypeDecl,
    Attribute,
    BindPattern,
    Call,
    ConstDecl,
    Distribution,
    EnumDecl,
    EnumPattern,
    ExternBlock,
    ExternStatic,
    ExtraDecl,
    Field,
    FnDecl,
    GroupApply,
    GroupDecl,
    ImplDecl,
    LetStmt,
    ModDecl,
    Name,
    Node,
    Param,
    StructDecl,
    StructPatternField,
    TraitDecl,
    Type,
    TypeDecl,
    TypeParam,
    UseDecl,
    Variant,
)
from cwind_frontend.ast_components.token import TokenKind as TK

from .legend import MOD, role_priority
from .positions import LineMap, Span, span_from_token

try:  # pragma: no cover - import guard for frontend layout changes
    from cwind_frontend.sa.types import BUILTIN_TYPES as _BUILTIN_TYPES
except Exception:  # pragma: no cover
    _BUILTIN_TYPES = {}

_BUILTIN_NAMES = frozenset(str(name).split("::")[-1] for name in _BUILTIN_TYPES)
_BUILTIN_NAMES |= {
    "Int",
    "UInt",
    "Float",
    "String",
    "Bool",
    "Byte",
    "None",
    "Vector",
    "Map",
    "Set",
    "Tuple",
    "Iterator",
    "Fn",
}

_NODE_BINDING_KINDS = {
    "var",
    "const",
    "field",
    "fn",
    "extern_static",
    "variant",
    "assoc_const",
    "static_field",
}

_SCAN_SKIP = {TK.RPAREN, TK.RBRACKET, TK.RBRACE, TK.QUESTION, TK.NOT}

_STOP_DEFAULT = {TK.SEMICOLON, TK.LBRACE}

_METHOD_PARENTS = (ImplDecl, ExtraDecl, TraitDecl)

_DECL_ROLES: dict[type, tuple[str, str]] = {
    StructDecl: ("struct", "struct"),
    EnumDecl: ("enum", "enum"),
    TraitDecl: ("interface", "trait"),
    TypeDecl: ("type", "type"),
    ConstDecl: ("variable", "const"),
    GroupDecl: ("type", "group"),
    ModDecl: ("namespace", "mod"),
}

_IMPORT_ITEM_ROLES: dict[type, str] = {
    FnDecl: "function",
    StructDecl: "struct",
    EnumDecl: "enum",
    TraitDecl: "interface",
    TypeDecl: "type",
    ConstDecl: "variable",
}


def builtin_role(name: str) -> str:
    return "type" if name in _BUILTIN_NAMES else "function"


def key_from_ann(ann: Any, name: Optional[str] = None) -> Optional[tuple]:
    if not isinstance(ann, dict):
        return None
    enum_name = ann.get("enum")
    variant_index = ann.get("variant_index")
    if enum_name is not None and variant_index is not None:
        enum_def = str(ann.get("enum_def") or "")
        if name:
            return ("variant_name", enum_def, str(enum_name), str(name))
        return ("variant", enum_def, str(enum_name), int(variant_index))
    binding = ann.get("binding")
    if isinstance(binding, dict):
        kind = binding.get("kind")
        ref = binding.get("ref")
        if kind in _NODE_BINDING_KINDS and isinstance(ref, int):
            return ("node", ref)
        if kind == "method" and isinstance(ref, int):
            return ("binding", ref)
        if kind == "builtin":
            return ("builtin", str(binding.get("name") or ""))
        if kind in ("bound_method", "trait_fn"):
            trait = binding.get("trait")
            member = binding.get("member") or binding.get("name")
            if trait and member:
                return ("trait_method", str(trait), str(member))
    member = ann.get("member")
    if isinstance(member, dict):
        kind = member.get("kind")
        ref = member.get("ref")
        if kind == "field" and isinstance(ref, int):
            return ("node", ref)
        if kind == "method" and isinstance(ref, int):
            return ("binding", ref)
        if kind == "builtin":
            return ("builtin", str(member.get("name") or ""))
    call = ann.get("call")
    if isinstance(call, dict):
        kind = call.get("callee_kind")
        ref = call.get("callee_ref")
        if kind == "fn" and isinstance(ref, int):
            return ("node", ref)
        if kind == "method" and isinstance(ref, int):
            return ("binding", ref)
        if kind == "bound_method" and isinstance(ref, dict):
            trait = ref.get("trait")
            member = ref.get("member")
            if trait and member:
                return ("trait_method", str(trait), str(member))
        if kind == "trait_fn" and isinstance(ref, str):
            trait = None
            member_ann = ann.get("member")
            if isinstance(member_ann, dict):
                trait = member_ann.get("trait")
            return ("trait_method", str(trait or ""), ref)
    return None


def role_from_ann(ann: Any) -> Optional[str]:
    if not isinstance(ann, dict):
        return None
    binding = ann.get("binding")
    if isinstance(binding, dict):
        kind = binding.get("kind")
        if kind == "fn":
            return "function"
        if kind == "method":
            return "method"
        if kind == "var":
            return "variable"
        if kind == "field":
            return "property"
        if kind == "variant":
            return "enumMember"
        if kind in ("const", "assoc_const", "extern_static", "static_field"):
            return "variable"
        if kind == "builtin":
            return builtin_role(str(binding.get("name") or ""))
        if kind in ("bound_method", "trait_fn"):
            return "method"
    member = ann.get("member")
    if isinstance(member, dict):
        kind = member.get("kind")
        if kind == "method":
            return "method"
        if kind == "field":
            return "property"
        if kind == "tuple_elem":
            return "property"
        if kind == "builtin":
            return builtin_role(str(member.get("name") or ""))
    call = ann.get("call")
    if isinstance(call, dict) and call.get("callee_kind") == "indirect":
        return "variable"
    if ann.get("enum") is not None and ann.get("variant_index") is not None:
        return "enumMember"
    return None


def _method_subjects(fn: FnDecl, parent: Optional[Node]) -> tuple:
    if isinstance(parent, TraitDecl):
        return (("method_trait", str(parent.name).split("::")[-1], fn.name),)
    if isinstance(parent, ImplDecl):
        owner = str(parent.struct.name).split("::")[-1]
        trait = str(parent.trait.name).split("::")[-1]
        return (
            ("method_owner", owner, fn.name),
            ("method_trait", trait, fn.name),
        )
    if isinstance(parent, ExtraDecl):
        owner = str(parent.struct.name).split("::")[-1]
        return (("method_owner", owner, fn.name),)
    if isinstance(parent, ExternBlock) and fn.cwind_owner is not None:
        owner = str(fn.cwind_owner.name).split("::")[-1]
        return (("method_owner", owner, fn.name),)
    return ()


def iter_child_nodes(node: Node) -> Iterator[Node]:
    for f in dataclasses.fields(node):
        if f.name in ("line", "column"):
            continue
        yield from _iter_nodes(getattr(node, f.name, None))


def _iter_nodes(value: Any) -> Iterator[Node]:
    if isinstance(value, Node):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_nodes(item)


def walk_nodes(node: Node) -> Iterator[Node]:
    if not isinstance(node, Node):
        return
    yield node
    for child in iter_child_nodes(node):
        yield from walk_nodes(child)


@dataclass
class Occurrence:
    file: str
    span: Span
    name: str
    role: str
    modifiers: int = 0
    key: Optional[tuple] = None
    node: Optional[Node] = None
    is_decl: bool = False
    decl_kind: Optional[str] = None
    subjects: tuple = ()

    @property
    def start_key(self) -> tuple[int, int]:
        return (self.span.line, self.span.character)

    @property
    def definition(self) -> bool:
        return bool(self.modifiers & MOD["declaration"])


@dataclass
class FileIndex:
    path: str
    source: str
    tokens: list[Token]
    occurrences: list[Occurrence]
    by_start: dict[tuple[int, int], list[Occurrence]]
    declarations: list[Occurrence]
    nodes: dict[int, Node]
    node_token_span: dict[int, tuple[int, int]]
    nodes_by_offset: dict[int, list[Node]]
    line_map: LineMap
    is_dependency: bool = False

    def occurrence_at(self, line: int, character: int) -> Optional[Occurrence]:
        found = self.by_start.get((line, character))
        if found:
            return found[0]
        token = self.token_at(line, character)
        if token is None:
            return None
        found = self.by_start.get((token.line - 1, token.column - 1))
        return found[0] if found else None

    def token_at(self, line: int, character: int) -> Optional[Token]:
        best: Optional[Token] = None
        for token in self.tokens:
            span = span_from_token(token)
            if span.contains(line, character):
                if best is None or (span.line, span.character) >= (
                    best.line - 1,
                    best.column - 1,
                ):
                    best = token
        return best

    def occurrence_named(self, name: str) -> Optional[Occurrence]:
        for occ in self.occurrences:
            if occ.name == name:
                return occ
        return None

    def add_occurrence(self, occurrence: Occurrence) -> None:
        bucket = self.by_start.setdefault(occurrence.start_key, [])
        replaced: Optional[Occurrence] = None
        for position, existing in enumerate(bucket):
            if existing.role == occurrence.role:
                return
            if role_priority(occurrence.role) > role_priority(existing.role):
                replaced = existing
                bucket[position] = occurrence
                break
        else:
            bucket.append(occurrence)
        if replaced is not None:
            if replaced in self.occurrences:
                self.occurrences.remove(replaced)
            if replaced.is_decl and replaced in self.declarations:
                self.declarations.remove(replaced)
        self.occurrences.append(occurrence)
        if occurrence.is_decl:
            self.declarations.append(occurrence)

    def node_span(self, node: Node) -> Optional[Span]:
        token_span = self.node_token_span.get(id(node))
        if token_span is None:
            return None
        first = self.tokens[token_span[0]]
        last = self.tokens[token_span[1]]
        return Span(
            first.line - 1,
            first.column - 1,
            last.end_line - 1,
            max(last.end_column - 1, last.column - 1),
        )

    def nodes_ending_at(self, offset: int) -> list[Node]:
        return list(self.nodes_by_offset.get(offset, ()))

    def nodes_starting_at(self, offset: int) -> list[Node]:
        return list(self.nodes_by_offset.get(offset, ()))

    def innermost_node(self, line: int, character: int) -> Optional[Node]:
        best: Optional[Node] = None
        for node in self.nodes_by_offset.values():
            for candidate in node:
                span = self.node_span(candidate)
                if span is None:
                    continue
                if span.contains(line, character):
                    if best is None:
                        best = candidate
                    else:
                        best_span = self.node_span(best)
                        if best_span is not None and (
                            (span.line, span.character) > (best_span.line, best_span.character)
                        ):
                            best = candidate
        return best

    def offset_of(self, line: int, character: int) -> int:
        return self.line_map.offset(line, character)

    def token_offset(self, token_index: int, *, end: bool = False) -> int:
        token = self.tokens[token_index]
        if end:
            return self.line_map.offset(token.end_line - 1, token.end_column - 1)
        return self.line_map.offset(token.line - 1, token.column - 1)


class Indexer:
    def __init__(
        self,
        path: str,
        source: str,
        tokens: list[Token],
        *,
        is_dependency: bool = False,
    ):
        self.path = path
        self.source = source
        self.tokens = tokens
        self.is_dependency = is_dependency
        self.line_map = LineMap(source)
        self._starts = {(t.line - 1, t.column - 1): i for i, t in enumerate(tokens)}
        self._start_keys = sorted(self._starts)
        self.occurrences: list[Occurrence] = []
        self._by_start: dict[tuple[int, int], list[Occurrence]] = {}
        self.nodes: dict[int, Node] = {}
        self._node_by_pyid: dict[int, Node] = {}
        self._end_cache: dict[int, Optional[int]] = {}
        self._token_spans: dict[int, tuple[int, int]] = {}
        self._generic_stack: list[set[str]] = []

    # -- public ---------------------------------------------------------

    def run(self, items: Iterable[Node]) -> FileIndex:
        for item in items:
            self.visit(item, None)
        declarations = [occ for occ in self.occurrences if occ.is_decl]
        nodes_by_offset: dict[int, list[Node]] = {}
        for pyid, (first, last) in self._token_spans.items():
            node = self._node_by_pyid.get(pyid)
            if node is None:
                continue
            start = self._offset_for_token(first)
            end = self._offset_for_token(last, end=True)
            nodes_by_offset.setdefault(start, []).append(node)
            nodes_by_offset.setdefault(end, []).append(node)
        return FileIndex(
            path=self.path,
            source=self.source,
            tokens=self.tokens,
            occurrences=self.occurrences,
            by_start=self._by_start,
            declarations=declarations,
            nodes=self.nodes,
            node_token_span=self._token_spans,
            nodes_by_offset=nodes_by_offset,
            line_map=self.line_map,
            is_dependency=self.is_dependency,
        )

    # -- traversal ------------------------------------------------------

    def visit(self, node: Any, parent: Optional[Node]) -> None:
        if not isinstance(node, Node):
            return
        self._node_by_pyid[id(node)] = node
        nid = getattr(node, "_typed_id", None)
        if isinstance(nid, int) and nid not in self.nodes:
            self.nodes[nid] = node

        if isinstance(node, Name):
            self._name(node, parent)
        elif isinstance(node, Attribute):
            self._attribute(node, parent)
        elif isinstance(node, Type):
            self._type(node)
        elif isinstance(node, FnDecl):
            self._fn(node, parent)
        elif isinstance(node, (StructDecl, EnumDecl, TraitDecl, TypeDecl, ConstDecl, GroupDecl, ModDecl)):
            self._simple_decl(node)
        elif isinstance(node, Field):
            self._field(node)
        elif isinstance(node, Variant):
            self._variant(node)
        elif isinstance(node, Param):
            self._param(node)
        elif isinstance(node, LetStmt):
            self._let(node)
        elif isinstance(node, BindPattern):
            self._bind(node)
        elif isinstance(node, TypeParam):
            self._type_param(node)
        elif isinstance(node, AssocTypeDecl):
            self._assoc_type(node)
        elif isinstance(node, EnumPattern):
            self._enum_pattern(node)
        elif isinstance(node, StructPatternField):
            self._struct_pattern_field(node)
        elif isinstance(node, UseDecl):
            self._use(node)
        elif isinstance(node, ExternStatic):
            self._extern_static(node)
        elif isinstance(node, GroupApply):
            self._group_apply(node)

        generics = self._generic_names_of(node)
        pushed = False
        if generics:
            self._generic_stack.append(generics)
            pushed = True
        for child in iter_child_nodes(node):
            self.visit(child, node)
        if pushed:
            self._generic_stack.pop()
        if self._token_spans.get(id(node)) is None:
            self.end_of(node)

    # -- occurrence helpers ---------------------------------------------

    def _add(
        self,
        index: int,
        role: str,
        *,
        key: Optional[tuple] = None,
        node: Optional[Node] = None,
        modifiers: int = 0,
        is_decl: bool = False,
        decl_kind: Optional[str] = None,
        subjects: tuple = (),
    ) -> None:
        if index < 0 or index >= len(self.tokens):
            return
        token = self.tokens[index]
        if token.kind != TK.IDENTIFIER:
            return
        span = span_from_token(token)
        start_key = (span.line, span.character)
        existing = self._by_start.get(start_key)
        if existing:
            current = existing[0]
            if current.role == role:
                if key is not None and current.key is None:
                    current.key = key
                current.modifiers |= modifiers
                current.is_decl = current.is_decl or is_decl
                current.decl_kind = current.decl_kind or decl_kind
                if subjects:
                    merged = list(current.subjects)
                    for subject in subjects:
                        if subject not in merged:
                            merged.append(subject)
                    current.subjects = tuple(merged)
                return
            if role_priority(role) <= role_priority(current.role):
                return
            existing.clear()
        occurrence = Occurrence(
            file=self.path,
            span=span,
            name=str(token.value),
            role=role,
            modifiers=modifiers,
            key=key,
            node=node,
            is_decl=is_decl,
            decl_kind=decl_kind,
            subjects=tuple(subjects),
        )
        self._by_start.setdefault(start_key, []).append(occurrence)
        self.occurrences.append(occurrence)

    def _add_node_decl(
        self,
        index: Optional[int],
        role: str,
        node: Node,
        decl_kind: str,
        *,
        modifiers: int = 0,
        subjects: tuple = (),
    ) -> None:
        if index is None:
            return
        nid = getattr(node, "_typed_id", None)
        key = (
            ("node", nid)
            if isinstance(nid, int)
            else ("name", decl_kind, getattr(node, "name", ""))
        )
        self._add(
            index,
            role,
            key=key,
            node=node,
            modifiers=modifiers | MOD["declaration"] | MOD["definition"],
            is_decl=True,
            decl_kind=decl_kind,
            subjects=subjects,
        )

    def _generic_names(self) -> set[str]:
        names: set[str] = set()
        for frame in self._generic_stack:
            names |= frame
        return names

    def _generic_names_of(self, node: Node) -> set[str]:
        params = getattr(node, "type_params", None)
        if params is None and isinstance(
            node, (StructDecl, EnumDecl, TraitDecl, TypeDecl, ImplDecl, ExtraDecl)
        ):
            params = node.params
        names: set[str] = set()
        if params:
            for param in params:
                if isinstance(param, TypeParam) and isinstance(param.name, str):
                    names.add(param.name)
        return names

    # -- node handlers ---------------------------------------------------

    def _call_ann(self, node: Node, parent: Optional[Node]) -> dict:
        ann = dict(getattr(node, "_typed_ann", None) or {})
        if isinstance(parent, Call) and parent.callee is node:
            parent_ann = getattr(parent, "_typed_ann", None) or {}
            if parent_ann:
                merged = dict(parent_ann)
                merged.update(ann)
                return merged
        return ann

    def _name(self, node: Name, parent: Optional[Node] = None) -> None:
        start = self.start_index(node)
        if start is None:
            return
        parts = self.consume_path(start)
        if not parts:
            return
        ann = self._call_ann(node, parent)
        role = role_from_ann(ann) or "variable"
        last_name = str(self.tokens[parts[-1]].value)
        key = key_from_ann(ann, last_name)
        module = ann.get("module")
        module_path = module.get("path") if isinstance(module, dict) else None
        count = len(parts)
        prefix = 0
        if isinstance(module_path, list) and module_path:
            prefix = min(len(module_path), count - 1)
        if role == "enumMember" and count >= 2:
            prefix = max(prefix, count - 2)
        for idx in range(prefix):
            self._add(
                parts[idx],
                "namespace",
                key=("module", tuple(module_path)) if module_path else None,
                node=node,
            )
        for idx in range(prefix, count - 1):
            self._add(
                parts[idx],
                "type",
                key=("type", str(self.tokens[parts[idx]].value), None),
                node=node,
            )
        self._add(parts[-1], role, key=key, node=node)

    def _attribute(self, node: Attribute, parent: Optional[Node] = None) -> None:
        member = self.end_of(node.obj)
        if member is None:
            return
        i = member + 1
        dot = None
        limit = min(len(self.tokens), member + 12)
        while i < limit:
            kind = self.tokens[i].kind
            if kind == TK.DOT:
                dot = i
                break
            if kind in _SCAN_SKIP:
                i += 1
                continue
            break
        if dot is None:
            return
        j = dot + 1
        if j >= len(self.tokens) or self.tokens[j].kind != TK.IDENTIFIER:
            return
        ann = self._call_ann(node, parent)
        role = role_from_ann(ann) or "property"
        self._add(j, role, key=key_from_ann(ann, str(self.tokens[j].value)), node=node)

    def _type(self, node: Type) -> None:
        start = self.start_index(node)
        if start is None:
            return
        end = self.consume_type(start)
        generic_names = self._generic_names()
        for i in range(start, end + 1):
            token = self.tokens[i]
            if token.kind != TK.IDENTIFIER or token.value in ("mut", "const", "fn"):
                continue
            if token.value in generic_names:
                self._add(i, "typeParameter", node=node)
            else:
                self._add(i, "type", key=("type", str(token.value), None), node=node)
            break

    def _fn(self, node: FnDecl, parent: Optional[Node]) -> None:
        index = self._fn_name_index(node)
        if index is None:
            return
        is_method = isinstance(parent, _METHOD_PARENTS) or (
            isinstance(parent, ExternBlock) and node.cwind_owner is not None
        )
        role = "method" if is_method else "function"
        subjects = _method_subjects(node, parent)
        self._add_node_decl(index, role, node, "fn", subjects=subjects)

    def _simple_decl(self, node: Node) -> None:
        role, kind = _DECL_ROLES[type(node)]
        index = self._decl_name_index(node)
        if index is None:
            return
        modifiers = MOD["readonly"] if kind == "const" else 0
        if kind == "mod":
            modifiers |= MOD["static"]
        self._add_node_decl(index, role, node, kind, modifiers=modifiers)

    def _field(self, node: Field) -> None:
        index = self._first_ident(self.start_index(node) or 0, {TK.COLON})
        modifiers = MOD["static"] if node.static else 0
        self._add_node_decl(index, "property", node, "field", modifiers=modifiers)

    def _variant(self, node: Variant) -> None:
        self._add_node_decl(self.start_index(node), "enumMember", node, "variant")

    def _param(self, node: Param) -> None:
        index = self._first_ident(
            self.start_index(node) or 0, {TK.COLON, TK.COMMA, TK.RPAREN}
        )
        self._add_node_decl(index, "parameter", node, "param")

    def _let(self, node: LetStmt) -> None:
        if node.pattern is not None:
            return
        self._add_node_decl(self._let_name_index(node), "variable", node, "let")

    def _bind(self, node: BindPattern) -> None:
        self._add_node_decl(self.start_index(node), "variable", node, "pattern")

    def _type_param(self, node: TypeParam) -> None:
        self._add_node_decl(self.start_index(node), "typeParameter", node, "type_param")

    def _assoc_type(self, node: AssocTypeDecl) -> None:
        self._add_node_decl(self.start_index(node), "type", node, "assoc_type")

    def _enum_pattern(self, node: EnumPattern) -> None:
        start = self.start_index(node)
        if start is None:
            return
        parts = self.consume_path(start)
        if not parts:
            return
        ann = getattr(node, "_typed_ann", None) or {}
        last_name = str(self.tokens[parts[-1]].value)
        key = key_from_ann(ann, last_name)
        for index in parts[:-1]:
            self._add(
                index,
                "type",
                key=("type", str(self.tokens[index].value), None),
                node=node,
            )
        self._add(parts[-1], "enumMember", key=key, node=node)

    def _struct_pattern_field(self, node: StructPatternField) -> None:
        index = self.start_index(node)
        if index is None:
            return
        if node.pattern is None:
            self._add_node_decl(index, "variable", node, "pattern")
        else:
            ann = getattr(node, "_typed_ann", None) or {}
            self._add(index, "property", key=key_from_ann(ann), node=node)

    def _use(self, node: UseDecl) -> None:
        start = self.start_index(node)
        if start is None or self.tokens[start].kind != TK.USE:
            return
        roles: dict[str, str] = {}
        for item in getattr(node, "loaded_items", ()) or ():
            name = getattr(item, "name", None)
            if not isinstance(name, str):
                continue
            roles[name] = _IMPORT_ITEM_ROLES.get(type(item), "namespace")
        end = self._use_end(start)
        i = start
        prefix: list[str] = []
        while i <= end and i < len(self.tokens):
            token = self.tokens[i]
            if token.kind == TK.IDENTIFIER:
                if i > start and self.tokens[i - 1].kind == TK.AS:
                    self._add(
                        i,
                        "namespace",
                        node=node,
                        is_decl=True,
                        decl_kind="alias",
                        modifiers=MOD["declaration"] | MOD["definition"],
                    )
                    prefix = []
                else:
                    role = roles.get(str(token.value))
                    if role is None:
                        role = "namespace" if str(token.value).islower() else "type"
                    following = self.tokens[i + 1].kind if i + 1 < len(self.tokens) else None
                    if following in (TK.PATH, TK.LBRACE):
                        prefix.append(str(token.value))
                        self._add(i, "namespace", key=("module", tuple(prefix)), node=node)
                    else:
                        qualifier = tuple(prefix)
                        prefix = []
                        self._add(
                            i,
                            role,
                            key=("symbol_name", str(token.value)),
                            node=node,
                            subjects=(
                                (("use_prefix", qualifier),) if qualifier else ()
                            ),
                        )
            i += 1

    def _extern_static(self, node: ExternStatic) -> None:
        index = self._first_ident(self.start_index(node) or 0, {TK.COLON})
        self._add_node_decl(
            index, "variable", node, "extern_static", modifiers=MOD["readonly"]
        )

    def _group_apply(self, node: GroupApply) -> None:
        start = self.start_index(node)
        if start is None:
            return
        first = self._first_ident(start, set())
        if first is None:
            return
        self._add(first, "type", node=node)
        second = self._first_ident(first + 1, {TK.COMMA, TK.LBRACE, TK.SEMICOLON})
        if second is not None:
            self._add(second, "type", node=node)

    # -- name token helpers ----------------------------------------------

    def _decl_name_index(self, node: Node) -> Optional[int]:
        start = self.start_index(node)
        if start is None:
            return None
        if isinstance(node, ModDecl):
            i = start
            while i < len(self.tokens) and self.tokens[i].kind in (TK.PUB, TK.STATIC):
                i += 1
            if i < len(self.tokens) and self.tokens[i].kind == TK.MOD:
                i += 1
            return self._first_ident(i, _STOP_DEFAULT)
        i = start
        while i < len(self.tokens) and self.tokens[i].kind in (TK.PUB, TK.STATIC):
            if (
                self.tokens[i].kind == TK.PUB
                and i + 1 < len(self.tokens)
                and self.tokens[i + 1].kind == TK.LPAREN
            ):
                i = self._match(i + 1, TK.LPAREN, TK.RPAREN) + 1
                continue
            i += 1
        return self._first_ident(i, _STOP_DEFAULT | {TK.COLON, TK.ASSIGN, TK.EQ})

    def _fn_name_index(self, node: FnDecl) -> Optional[int]:
        start = self.start_index(node)
        if start is None:
            return None
        i = start
        while i < len(self.tokens) and self.tokens[i].kind != TK.FN:
            i += 1
            if i - start > 4:
                break
        if i >= len(self.tokens) or self.tokens[i].kind != TK.FN:
            i = start
        name: Optional[int] = None
        depth = 0
        j = i + 1
        while j < len(self.tokens):
            kind = self.tokens[j].kind
            if kind == TK.LT:
                depth += 1
            elif kind == TK.GT:
                depth = max(depth - 1, 0)
            elif kind == TK.SHR:
                depth = max(depth - 2, 0)
            elif kind == TK.IDENTIFIER and depth == 0:
                if j > 0 and self.tokens[j - 1].kind == TK.PATH:
                    name = j
                elif name is None:
                    name = j
            elif kind == TK.LPAREN and depth == 0:
                return name
            elif kind in (TK.SEMICOLON, TK.LBRACE):
                return name
            j += 1
        return name

    def _let_name_index(self, node: LetStmt) -> Optional[int]:
        start = self.start_index(node)
        if start is None:
            return None
        i = start + 1
        if i < len(self.tokens) and self.tokens[i].kind == TK.MUT:
            i += 1
        if i < len(self.tokens) and self.tokens[i].kind == TK.IDENTIFIER:
            return i
        return None

    def _first_ident(self, start: int, stop_kinds: set) -> Optional[int]:
        i = max(start, 0)
        while i < len(self.tokens):
            kind = self.tokens[i].kind
            if kind == TK.IDENTIFIER:
                return i
            if kind in stop_kinds:
                return None
            i += 1
        return None

    # -- token scanning ---------------------------------------------------

    def start_index(self, node: Node) -> Optional[int]:
        key = (node.line - 1, node.column - 1)
        exact = self._starts.get(key)
        if exact is not None:
            return exact
        slot = bisect.bisect_left(self._start_keys, key)
        if slot >= len(self._start_keys):
            return len(self.tokens) - 1 if self.tokens else None
        return self._starts[self._start_keys[slot]]

    def consume_path(self, index: int) -> list[int]:
        if index >= len(self.tokens) or self.tokens[index].kind != TK.IDENTIFIER:
            return []
        parts = [index]
        i = index + 1
        while (
            i + 1 < len(self.tokens)
            and self.tokens[i].kind == TK.PATH
            and self.tokens[i + 1].kind == TK.IDENTIFIER
        ):
            parts.append(i + 1)
            i += 2
        return parts

    def consume_type(self, index: int) -> int:
        if index >= len(self.tokens):
            return len(self.tokens) - 1
        kind = self.tokens[index].kind
        if kind in (TK.AMP, TK.STAR, TK.STAR_CONST, TK.STAR_MUT):
            i = index + 1
            if i < len(self.tokens) and self.tokens[i].kind == TK.MUT:
                i += 1
            return self.consume_type(i)
        if kind == TK.LBRACKET:
            return self._match(index, TK.LBRACKET, TK.RBRACKET)
        if kind == TK.IDENTIFIER and self.tokens[index].value == "fn":
            i = index + 1
            if i < len(self.tokens) and self.tokens[i].kind == TK.LPAREN:
                i = self._match(i, TK.LPAREN, TK.RPAREN) + 1
            if i < len(self.tokens) and self.tokens[i].kind == TK.ARROW:
                return self.consume_type(i + 1)
            return min(i, len(self.tokens) - 1)
        if kind == TK.IDENTIFIER:
            parts = self.consume_path(index)
            last = parts[-1]
            if last + 1 < len(self.tokens) and self.tokens[last + 1].kind == TK.LT:
                close = self._match_angle(last + 1)
                if close is not None:
                    return close
            return last
        return index

    def _match(self, index: int, open_kind, close_kind) -> int:
        pairs = {
            TK.LPAREN: TK.RPAREN,
            TK.LBRACKET: TK.RBRACKET,
            TK.LBRACE: TK.RBRACE,
        }
        depth = 0
        i = index
        while i < len(self.tokens):
            kind = self.tokens[i].kind
            if kind == open_kind:
                depth += 1
            elif kind == close_kind:
                depth -= 1
                if depth == 0:
                    return i
            elif kind in pairs and kind != open_kind:
                i = self._match(i, kind, pairs[kind])
            i += 1
        return len(self.tokens) - 1

    def _match_angle(self, index: int) -> Optional[int]:
        depth = 0
        i = index
        while i < len(self.tokens):
            kind = self.tokens[i].kind
            if kind == TK.LT:
                depth += 1
            elif kind == TK.GT:
                depth -= 1
                if depth == 0:
                    return i
            elif kind == TK.SHR:
                depth -= 2
                if depth <= 0:
                    return i
            elif kind in (TK.LPAREN, TK.LBRACKET, TK.LBRACE):
                pairs = {
                    TK.LPAREN: TK.RPAREN,
                    TK.LBRACKET: TK.RBRACKET,
                    TK.LBRACE: TK.RBRACE,
                }
                i = self._match(i, kind, pairs[kind])
            i += 1
        return None

    def _use_end(self, start: int) -> int:
        depth = 0
        last = start
        i = start
        while i < len(self.tokens):
            kind = self.tokens[i].kind
            if kind == TK.LBRACE:
                depth += 1
            elif kind == TK.RBRACE:
                depth = max(depth - 1, 0)
            elif kind == TK.SEMICOLON and depth == 0:
                return last
            last = i
            i += 1
        return last

    # -- spans ------------------------------------------------------------

    def end_of(self, node: Optional[Node]) -> Optional[int]:
        if node is None:
            return None
        pyid = id(node)
        if pyid in self._end_cache:
            return self._end_cache[pyid]
        end = self._compute_end(node)
        self._end_cache[pyid] = end
        start = self.start_index(node)
        if start is not None and end is not None:
            self._token_spans[pyid] = (start, max(end, start))
        return end

    def _compute_end(self, node: Node) -> Optional[int]:
        start = self.start_index(node)
        if start is None:
            return None
        if isinstance(node, Name):
            parts = self.consume_path(start)
            return parts[-1] if parts else start
        if isinstance(node, Type):
            return self.consume_type(start)
        if isinstance(node, EnumPattern):
            parts = self.consume_path(start)
            ends: list[Optional[int]] = [parts[-1] if parts else start]
            ends.extend(self.end_of(elem) for elem in node.elems)
            ends.extend(self.end_of(field) for field in node.named_fields or ())
            return self._max_end(ends, fallback=start)
        if isinstance(node, Attribute):
            obj_end = self.end_of(node.obj)
            if obj_end is None:
                return start
            i = obj_end + 1
            while i < len(self.tokens) and self.tokens[i].kind in _SCAN_SKIP:
                i += 1
            if i < len(self.tokens) and self.tokens[i].kind == TK.DOT:
                j = i + 1
                if j < len(self.tokens) and self.tokens[j].kind == TK.IDENTIFIER:
                    return j
            return obj_end
        if isinstance(node, UseDecl):
            return self._use_end(start)
        if isinstance(node, LetStmt):
            ends = [
                self._let_name_index(node) if node.pattern is None else None,
                self.end_of(node.type),
                self.end_of(node.value),
            ]
            return self._max_end(ends, fallback=start)
        if isinstance(node, Param):
            ends = [
                self._first_ident(start, {TK.COLON, TK.COMMA, TK.RPAREN}),
                self.end_of(node.type),
            ]
            return self._max_end(ends, fallback=start)
        if isinstance(node, Field):
            ends = [
                self._first_ident(start, {TK.COLON}),
                self.end_of(node.type),
                self.end_of(node.initializer),
                self.end_of(node.validation),
            ]
            return self._max_end(ends, fallback=start)
        if isinstance(node, Variant):
            ends = [start]
            ends.extend(self.end_of(child) for child in node.fields)
            return self._max_end(ends, fallback=start)
        if isinstance(node, FnDecl):
            ends = [self._fn_name_index(node)]
            ends.extend(self.end_of(child) for child in node.type_params)
            ends.extend(self.end_of(child) for child in node.params)
            ends.append(self.end_of(node.return_type))
            ends.append(self.end_of(node.body))
            return self._max_end(ends, fallback=start)
        if isinstance(node, TypeParam):
            ends = [start, self.end_of(node.default)]
            if node.bound is not None:
                ends.append(self.end_of(node.bound))
            return self._max_end(ends, fallback=start)
        if isinstance(node, StructPatternField):
            return self._max_end([start, self.end_of(node.pattern)], fallback=start)
        if isinstance(node, ExternStatic):
            return self._max_end(
                [self._first_ident(start, {TK.COLON}), self.end_of(node.type)],
                fallback=start,
            )
        if isinstance(node, AssocTypeDecl):
            return self._max_end([start, self.end_of(node.bound)], fallback=start)
        if isinstance(node, Distribution):
            return self._max_end(
                [self.end_of(child) for child in iter_child_nodes(node)],
                fallback=start,
            )
        if isinstance(node, GroupApply):
            first = self._first_ident(start, set())
            second = self._first_ident(
                (first if first is not None else start) + 1,
                {TK.COMMA, TK.LBRACE, TK.SEMICOLON},
            )
            return self._max_end([first, second], fallback=start)
        if isinstance(
            node, (StructDecl, EnumDecl, TraitDecl, TypeDecl, ConstDecl, GroupDecl, ModDecl)
        ):
            ends = [self._decl_name_index(node)]
            ends.extend(self.end_of(child) for child in iter_child_nodes(node))
            return self._max_end(ends, fallback=start)
        ends = [self.end_of(child) for child in iter_child_nodes(node)]
        return self._max_end(ends, fallback=start)

    def _max_end(self, ends: Iterable[Optional[int]], fallback: int) -> int:
        best: Optional[int] = None
        for end in ends:
            if end is None:
                continue
            if best is None or end > best:
                best = end
        return best if best is not None else fallback

    def _offset_for_token(self, index: int, *, end: bool = False) -> int:
        token = self.tokens[index]
        if end:
            return self.line_map.offset(token.end_line - 1, token.end_column - 1)
        return self.line_map.offset(token.line - 1, token.column - 1)


def build_file_index(
    path: str,
    source: str,
    tokens: list[Token],
    items: Iterable[Node],
    *,
    is_dependency: bool = False,
) -> FileIndex:
    return Indexer(path, source, tokens, is_dependency=is_dependency).run(items)


def scan_macro_definitions(index: FileIndex) -> list[Occurrence]:
    """Find ``macro_rules! NAME`` definition heads in a raw token stream.

    Declarative definitions are stripped before parsing, so the AST never
    carries them; the raw tokens still do.
    """
    found: list[Occurrence] = []
    tokens = index.tokens
    for i, token in enumerate(tokens):
        if token.kind != TK.IDENTIFIER or str(token.value) != "macro_rules":
            continue
        if i + 2 >= len(tokens) or tokens[i + 1].kind != TK.NOT:
            continue
        name_token = tokens[i + 2]
        if name_token.kind != TK.IDENTIFIER:
            continue
        found.append(
            Occurrence(
                file=index.path,
                span=span_from_token(name_token),
                name=str(name_token.value),
                role="macro",
                modifiers=MOD["declaration"] | MOD["definition"],
                is_decl=True,
                decl_kind="macro",
            )
        )
    return found
