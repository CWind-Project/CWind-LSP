"""Semantic token legend shared by the index and the token encoder."""

from __future__ import annotations

from lsprotocol import types

TOKEN_TYPES: list[str] = [
    "namespace",
    "type",
    "class",
    "enum",
    "interface",
    "struct",
    "typeParameter",
    "parameter",
    "variable",
    "property",
    "enumMember",
    "event",
    "function",
    "method",
    "macro",
    "keyword",
    "modifier",
    "comment",
    "string",
    "number",
    "regexp",
    "operator",
    "decorator",
]

TOKEN_MODIFIERS: list[str] = [
    "declaration",
    "definition",
    "readonly",
    "static",
    "deprecated",
    "abstract",
    "async",
    "modification",
    "documentation",
    "defaultLibrary",
]

MOD = {name: 1 << index for index, name in enumerate(TOKEN_MODIFIERS)}

TYPE_INDEX = {name: index for index, name in enumerate(TOKEN_TYPES)}

LEGEND = types.SemanticTokensLegend(
    token_types=TOKEN_TYPES,
    token_modifiers=TOKEN_MODIFIERS,
)

_ROLE_PRIORITY = {
    "namespace": 0,
    "variable": 1,
    "typeParameter": 2,
    "type": 3,
    "parameter": 4,
    "property": 4,
    "enumMember": 5,
    "function": 6,
    "method": 6,
    "struct": 7,
    "enum": 7,
    "interface": 7,
    "macro": 9,
}


def role_priority(role: str) -> int:
    return _ROLE_PRIORITY.get(role, 1)
