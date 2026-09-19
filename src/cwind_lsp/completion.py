"""Completion: scope names, member access and ``::`` paths."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from cwind_frontend import Token, TokenKind
from cwind_frontend.lexer import lex_with_errors
from lsprotocol import types

from .analysis import CompletionCandidate, Snapshot, _bare_name
from .index import FileIndex
from .positions import LineMap
from .vfs import normalize

Probe = Callable[[str, int], Optional[Snapshot]]

KEYWORDS = [
    "fn",
    "let",
    "mut",
    "struct",
    "enum",
    "impl",
    "extra",
    "trait",
    "const",
    "static",
    "pub",
    "use",
    "mod",
    "if",
    "elif",
    "else",
    "match",
    "for",
    "while",
    "loop",
    "in",
    "return",
    "break",
    "continue",
    "where",
    "type",
    "typedef",
    "group",
    "extern",
    "which",
    "self",
    "Self",
    "true",
    "false",
]

SNIPPETS: list[tuple[str, str]] = [
    ("fn", "fn ${1:name}(${2}) -> ${3:Int} {\n\t$0\n}"),
    ("let", "let ${1:name}: ${2:Int} = ${3:value};"),
    ("struct", "struct ${1:Name} {\n\t${2:field}: ${3:Int},\n}"),
    ("enum", "enum ${1:Name} {\n\t${2:Variant},\n}"),
    ("impl", "impl ${1:Trait} for ${2:Type} {\n\t$0\n}"),
    ("extra", "extra ${1:Type} {\n\t$0\n}"),
    ("match", "match (${1:value}) {\n\t${2:Pattern} => {\n\t\t$0\n\t}\n}"),
    ("for", "for ${1:item} in ${2:iterable} {\n\t$0\n}"),
    ("while", "while (${1:cond}) {\n\t$0\n}"),
    ("if", "if (${1:cond}) {\n\t$0\n}"),
    ("use", "use ${1:std::module};"),
]

_BUILTIN_TYPES = [
    "Int",
    "Int8",
    "Int16",
    "Int32",
    "Int64",
    "UInt",
    "UInt8",
    "UInt16",
    "UInt32",
    "UInt64",
    "Float",
    "Float64",
    "Bool",
    "Byte",
    "String",
    "Vector",
    "Map",
    "Set",
    "Tuple",
    "Option",
]


@dataclass
class CompletionRequest:
    snapshot: Snapshot
    path: str
    line: int
    character: int
    text: str
    probe: Optional[Probe] = None


def complete(request: CompletionRequest) -> list[types.CompletionItem]:
    snapshot = request.snapshot
    path = normalize(request.path)
    text = request.text
    line_map = LineMap(text)
    cursor = line_map.offset(request.line, request.character)
    lexed = lex_with_errors(text, emit_comments=True)
    significant = [
        token for token in lexed.tokens if token.kind != TokenKind.COMMENT
    ]
    index = snapshot.files.get(path)

    before, word_start = _context_tokens(significant, line_map, cursor)
    prefix = text[word_start:cursor]

    candidates: list[CompletionCandidate] = []
    if before is not None and significant[before].kind == TokenKind.DOT:
        candidates = _member_candidates(
            request, significant[before], word_start, line_map, text
        )
    elif before is not None and significant[before].kind == TokenKind.PATH:
        candidates = _path_candidates(request, significant, before, index)
    else:
        type_position = False
        if index is not None and index.source[:cursor] == text[:cursor]:
            type_position = _in_type_position(index, cursor)
        if not type_position:
            type_position = _token_type_position(significant, before)
        candidates = _scope_candidates(request, cursor, prefix, type_position)

    items: list[types.CompletionItem] = []
    seen: set[str] = set()
    lowered = prefix.lower()
    for candidate in candidates:
        if candidate.label in seen:
            continue
        if lowered and not candidate.label.lower().startswith(lowered):
            continue
        seen.add(candidate.label)
        items.append(
            types.CompletionItem(
                label=candidate.label,
                kind=types.CompletionItemKind(candidate.kind),
                detail=candidate.detail or None,
                insert_text=candidate.insert_text or candidate.label,
                sort_text=candidate.sort_text or candidate.label,
            )
        )
    items.sort(key=lambda item: item.sort_text or item.label)
    return items


def _context_tokens(
    tokens: list[Token], line_map: LineMap, cursor: int
) -> tuple[Optional[int], int]:
    if not tokens:
        return None, cursor
    starts = [_start_offset(token, line_map) for token in tokens]
    low, high = 0, len(tokens)
    while low < high:
        mid = (low + high) // 2
        if starts[mid] < cursor:
            low = mid + 1
        else:
            high = mid
    last = low - 1
    if last < 0:
        return None, cursor
    token = tokens[last]
    token_end = _end_offset(token, line_map)
    if starts[last] <= cursor <= token_end and token.kind == TokenKind.IDENTIFIER:
        return last - 1, starts[last]
    return last, cursor


def _start_offset(token: Token, line_map: LineMap) -> int:
    return line_map.offset(token.line - 1, token.column - 1)


def _end_offset(token: Token, line_map: LineMap) -> int:
    return line_map.offset(token.end_line - 1, token.end_column - 1)


def _member_candidates(
    request: CompletionRequest,
    dot: Token,
    word_start: int,
    line_map: LineMap,
    text: str,
) -> list[CompletionCandidate]:
    snapshot = request.snapshot
    receiver_end = _start_offset(dot, line_map)
    index = snapshot.files.get(normalize(request.path))
    type_info = (
        infer_receiver_type(index, receiver_end) if index is not None else None
    )
    if type_info is None and request.probe is not None:
        probed = request.probe(text, word_start)
        if probed is not None:
            probe_index = probed.files.get(normalize(request.path))
            if probe_index is not None and text[:receiver_end] == probe_index.source[:receiver_end]:
                type_info = infer_receiver_type(probe_index, receiver_end)
    if type_info is None:
        return []
    return snapshot.members_of(type_info)


def infer_receiver_type(index: FileIndex, dot_offset: int) -> Optional[dict]:
    best: Optional[dict] = None
    best_start = -1
    for delta in range(0, 4):
        for node in index.nodes_by_offset.get(dot_offset - delta, ()):
            token_span = index.node_token_span.get(id(node))
            if token_span is None:
                continue
            end = index.token_offset(token_span[1], end=True)
            gap = index.source[end:dot_offset]
            if gap.strip(")]}? \t\r\n") != "":
                continue
            info = (getattr(node, "_typed_ann", None) or {}).get("type")
            if not isinstance(info, dict):
                continue
            start = index.token_offset(token_span[0])
            if start > best_start:
                best = info
                best_start = start
        if best is not None:
            break
    return best


def _path_candidates(
    request: CompletionRequest,
    tokens: list[Token],
    path_token_index: int,
    index: Optional[FileIndex],
) -> list[CompletionCandidate]:
    snapshot = request.snapshot
    path = normalize(request.path)
    j = path_token_index - 1
    parts: list[str] = []
    while j >= 0:
        token = tokens[j]
        if token.kind == TokenKind.IDENTIFIER:
            parts.append(str(token.value))
            j -= 1
            if j >= 0 and tokens[j].kind == TokenKind.PATH:
                j -= 1
                continue
            break
        break
    parts.reverse()
    if not parts:
        return []
    exported = snapshot.alias_exports.get((path, parts[0]))
    if exported:
        candidates: list[CompletionCandidate] = []
        for name in sorted(exported):
            role = snapshot.decl_role_for_name(name)
            candidates.append(
                CompletionCandidate(
                    name,
                    _kind_for_role(role),
                    role,
                    sort_text="0" + name,
                )
            )
        return candidates
    if len(parts) >= 2 and parts[0] in ("std", "crate", "self", "super"):
        return snapshot.module_candidates(parts)
    typed = snapshot.path_candidates_for_type(parts[-1])
    if typed:
        return typed
    return snapshot.module_candidates(parts)


_TYPE_ROLES = {"struct", "enum", "interface", "type", "typeParameter"}
_VALUE_ROLES = {
    "function",
    "method",
    "variable",
    "property",
    "enumMember",
    "macro",
}


def _rank(label: str, prefix: str, bucket: int) -> str:
    case = 0 if prefix and label.startswith(prefix) else 1
    return f"{bucket}{case}{label}"


def _scope_candidates(
    request: CompletionRequest,
    cursor: int,
    prefix: str,
    type_position: bool,
) -> list[CompletionCandidate]:
    snapshot = request.snapshot
    path = normalize(request.path)
    candidates: list[CompletionCandidate] = []
    index = snapshot.files.get(path)
    if index is not None:
        index_cursor = min(cursor, len(index.source))
        offsets = [
            (index.offset_of(occ.span.line, occ.span.character), occ)
            for occ in index.declarations
            if occ.role in ("variable", "parameter")
        ]
        local: dict[str, tuple[int, CompletionCandidate]] = {}
        for offset, occ in offsets:
            if offset > index_cursor:
                continue
            candidate = CompletionCandidate(
                occ.name,
                _kind_for_role(occ.role),
                occ.decl_kind or occ.role,
                sort_text=_rank(occ.name, prefix, 0),
            )
            previous = local.get(occ.name)
            if previous is None or offset >= previous[0]:
                local[occ.name] = (offset, candidate)
        candidates.extend(item[1] for item in local.values())

    visible = snapshot.visible_by_file.get(path)
    names = visible if visible is not None else set(snapshot.symbols_by_name)
    for name in names:
        role = snapshot.decl_role_for_name(name)
        if name in ("self", "Self"):
            continue
        candidates.append(
            CompletionCandidate(
                name,
                _kind_for_role(role),
                role,
                sort_text=_rank(name, prefix, _bucket_for(role, type_position)),
            )
        )
    for name in _BUILTIN_TYPES:
        candidates.append(
            CompletionCandidate(
                name,
                int(types.CompletionItemKind.Struct),
                "type",
                sort_text=_rank(name, prefix, 0 if type_position else 4),
            )
        )
    for keyword in KEYWORDS:
        candidates.append(
            CompletionCandidate(
                keyword,
                int(types.CompletionItemKind.Keyword),
                "keyword",
                sort_text=_rank(keyword, prefix, 5),
            )
        )
    for label, snippet in SNIPPETS:
        candidates.append(
            CompletionCandidate(
                label,
                int(types.CompletionItemKind.Snippet),
                "snippet",
                insert_text=snippet,
                sort_text=_rank(label, prefix, 6),
            )
        )
    return candidates


def _bucket_for(role: str, type_position: bool) -> int:
    if role in _TYPE_ROLES:
        return 0 if type_position else 3
    if role == "namespace":
        return 2
    if role in _VALUE_ROLES:
        return 1 if not type_position else 5
    return 2


def _in_type_position(index: FileIndex, cursor: int) -> bool:
    from cwind_frontend.ast_components.ast import Type

    for node in index.nodes.values():
        if not isinstance(node, Type):
            continue
        token_span = index.node_token_span.get(id(node))
        if token_span is None:
            continue
        start = index.token_offset(token_span[0])
        end = index.token_offset(token_span[1], end=True)
        if start <= cursor <= end:
            return True
    return False


_TYPE_LEAD_KEYWORDS = frozenset(
    {
        TokenKind.IMPL,
        TokenKind.EXTRA,
        TokenKind.TRAIT,
        TokenKind.STRUCT,
        TokenKind.ENUM,
        TokenKind.TYPE,
        TokenKind.TYPEDEF,
        TokenKind.WHERE,
        TokenKind.AS,
        TokenKind.ARROW,
    }
)


def _token_type_position(tokens: list[Token], before: Optional[int]) -> bool:
    if before is None or before < 0:
        return False
    kind = tokens[before].kind
    if kind in _TYPE_LEAD_KEYWORDS:
        return True
    if kind == TokenKind.COLON:
        for index in range(before - 1, -1, -1):
            previous = tokens[index].kind
            if previous in (TokenKind.LET, TokenKind.FAT_ARROW, TokenKind.ARROW):
                return True
            if previous in (
                TokenKind.SEMICOLON,
                TokenKind.LBRACE,
                TokenKind.RBRACE,
                TokenKind.ASSIGN,
            ):
                return False
        return False
    depth = 0
    for index in range(before, -1, -1):
        current = tokens[index].kind
        if current == TokenKind.GT:
            depth += 1
        elif current == TokenKind.SHR:
            depth += 2
        elif current == TokenKind.LT:
            depth -= 1
            if depth < 0:
                return True
        elif current in (
            TokenKind.SEMICOLON,
            TokenKind.LBRACE,
            TokenKind.RBRACE,
            TokenKind.ASSIGN,
            TokenKind.FAT_ARROW,
            TokenKind.ARROW,
        ):
            return False
    return False


def _kind_for_role(role: str) -> int:
    return {
        "struct": int(types.CompletionItemKind.Struct),
        "enum": int(types.CompletionItemKind.Enum),
        "interface": int(types.CompletionItemKind.Interface),
        "type": int(types.CompletionItemKind.TypeParameter),
        "typeParameter": int(types.CompletionItemKind.TypeParameter),
        "function": int(types.CompletionItemKind.Function),
        "method": int(types.CompletionItemKind.Method),
        "macro": int(types.CompletionItemKind.Function),
        "variable": int(types.CompletionItemKind.Variable),
        "property": int(types.CompletionItemKind.Property),
        "enumMember": int(types.CompletionItemKind.EnumMember),
        "namespace": int(types.CompletionItemKind.Module),
    }.get(role, int(types.CompletionItemKind.Text))


def bare_name(name: str) -> str:
    return _bare_name(name)
