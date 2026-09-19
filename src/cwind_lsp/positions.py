"""Source position helpers.

The CWind frontend reports 1-based line/column pairs with an exclusive end
column; the LSP protocol uses 0-based lines and columns expressed in the
client's negotiated position encoding (usually UTF-16).  This module keeps an
internal, encoding independent representation (0-based code point columns) and
converts at the protocol boundary through pygls' ``PositionCodec``.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from lsprotocol import types

from pygls.workspace.position_codec import PositionCodec, ServerTextPosition


@dataclass(frozen=True)
class Span:
    """A half-open range in 0-based code point coordinates."""

    line: int
    character: int
    end_line: int
    end_character: int

    def contains(self, line: int, character: int) -> bool:
        if line < self.line or line > self.end_line:
            return False
        if line == self.line and character < self.character:
            return False
        if line == self.end_line and character >= self.end_character:
            return False
        return True

    def overlaps(self, line: int, character: int) -> bool:
        """True when the position touches the span (inclusive of the end)."""
        if line < self.line or line > self.end_line:
            return False
        if line == self.line and character < self.character:
            return False
        if line == self.end_line and character > self.end_character:
            return False
        return True

    def with_end(self, other: "Span") -> "Span":
        if (other.end_line, other.end_character) <= (self.end_line, self.end_character):
            return self
        return Span(self.line, self.character, other.end_line, other.end_character)


def span_from_token(token) -> Span:
    return Span(
        token.line - 1,
        token.column - 1,
        token.end_line - 1,
        max(token.end_column - 1, token.column - 1),
    )


def span_from_error(error) -> Span:
    start_line = max(getattr(error, "line", 1) - 1, 0)
    start_char = max(getattr(error, "column", 1) - 1, 0)
    end_line = max(getattr(error, "end_line", getattr(error, "line", 1)) - 1, 0)
    end_char = max(getattr(error, "end_column", getattr(error, "column", 1) + 1) - 1, 0)
    if (end_line, end_char) <= (start_line, start_char):
        end_line, end_char = start_line, start_char + 1
    return Span(start_line, start_char, end_line, end_char)


def span_to_range(span: Span, lines: Sequence[str], codec: PositionCodec) -> types.Range:
    return types.Range(
        start=codec.position_to_client_units(
            lines, ServerTextPosition(span.line, span.character)
        ),
        end=codec.position_to_client_units(
            lines, ServerTextPosition(span.end_line, span.end_character)
        ),
    )


class LineMap:
    """Offset <-> (line, code point column) conversion for one source text."""

    __slots__ = ("text", "starts")

    def __init__(self, text: str):
        self.text = text
        starts = [0]
        append = starts.append
        for index, char in enumerate(text):
            if char == "\n":
                append(index + 1)
        self.starts = starts

    @property
    def line_count(self) -> int:
        return len(self.starts)

    def line_text(self, line: int) -> str:
        if line < 0 or line >= len(self.starts):
            return ""
        start = self.starts[line]
        if line + 1 < len(self.starts):
            end = self.starts[line + 1]
            if end > start and self.text[end - 1] == "\n":
                end -= 1
            if end > start and self.text[end - 1] == "\r":
                end -= 1
            return self.text[start:end]
        return self.text[start:]

    def offset(self, line: int, character: int) -> int:
        if line < 0:
            return 0
        if line >= len(self.starts):
            return len(self.text)
        start = self.starts[line]
        end = self.starts[line + 1] if line + 1 < len(self.starts) else len(self.text)
        return min(start + max(character, 0), end)

    def position(self, offset: int) -> tuple[int, int]:
        offset = max(0, min(offset, len(self.text)))
        line = bisect.bisect_right(self.starts, offset) - 1
        return line, offset - self.starts[line]

    def span(self, line: int, character: int, end_line: int, end_character: int) -> Span:
        return Span(line, character, end_line, end_character)

    def span_offset(self, span: Span) -> tuple[int, int]:
        return self.offset(span.line, span.character), self.offset(
            span.end_line, span.end_character
        )


def token_span(token) -> Span:
    return span_from_token(token)


def chooses_line(tokens: Iterable, line: int, character: int) -> Optional[object]:
    """Return the first token whose half-open span contains the position."""
    for token in tokens:
        span = span_from_token(token)
        if span.contains(line, character):
            return token
    return None
