"""Token based semantic highlighting with SA refinement."""

from __future__ import annotations

from typing import Optional

from cwind_frontend import Token, TokenKind
from cwind_frontend.lexer import lex_with_errors
from lsprotocol import types
from pygls.workspace.position_codec import PositionCodec, ServerTextPosition

from .index import FileIndex
from .legend import MOD, TYPE_INDEX

_KEYWORD_KINDS = frozenset(
    kind
    for kind in TokenKind
    if kind.name.isupper()
    and kind
    not in (
        TokenKind.IDENTIFIER,
        TokenKind.INTEGER,
        TokenKind.FLOAT,
        TokenKind.STRING,
        TokenKind.COMMENT,
    )
)

def classify_token(token: Token, index: Optional[FileIndex]) -> Optional[tuple[str, int]]:
    kind = token.kind
    if kind == TokenKind.COMMENT:
        return ("comment", 0)
    if kind == TokenKind.STRING:
        return ("string", 0)
    if kind in (TokenKind.INTEGER, TokenKind.FLOAT):
        return ("number", 0)
    if kind == TokenKind.IDENTIFIER:
        if token.value in ("true", "false"):
            return ("keyword", 0)
        if index is not None:
            found = index.by_start.get((token.line - 1, token.column - 1))
            if found:
                occurrence = found[0]
                modifiers = occurrence.modifiers
                if index.is_dependency:
                    modifiers |= MOD["defaultLibrary"]
                return (occurrence.role, modifiers)
        if token.value in ("self", "Self"):
            return ("variable", 0)
        return None
    if kind in _KEYWORD_KINDS:
        return ("keyword", 0)
    return None


def semantic_tokens(
    path: str,
    source: str,
    codec: PositionCodec,
    index: Optional[FileIndex] = None,
) -> types.SemanticTokens:
    lexed = lex_with_errors(source, emit_comments=True)
    lines = source.splitlines(True)
    data: list[int] = []
    previous_line = 0
    previous_char = 0
    for token in lexed.tokens:
        classified = classify_token(token, index)
        if classified is None:
            continue
        role, modifiers = classified
        if role not in TYPE_INDEX:
            continue
        line = token.line - 1
        char = token.column - 1
        raw = token.raw
        if token.end_line != token.line:
            line_text = _line_text(lines, line)
            raw = line_text[char:]
        if not raw:
            continue
        start = codec.position_to_client_units(
            lines, ServerTextPosition(line, char)
        )
        length = codec.client_num_units(raw)
        if length <= 0:
            continue
        delta_line = start.line - previous_line
        delta_char = start.character - previous_char if delta_line == 0 else start.character
        data.extend(
            [delta_line, delta_char, length, TYPE_INDEX[role], modifiers]
        )
        previous_line = start.line
        previous_char = start.character
    return types.SemanticTokens(data=data)


def _line_text(lines: list[str], line: int) -> str:
    if line < 0 or line >= len(lines):
        return ""
    return lines[line].rstrip("\r\n")
