from cwind_lsp.positions import LineMap, Span


def test_line_map_roundtrip():
    text = "abc\ndef\r\nghi"
    line_map = LineMap(text)
    assert line_map.line_count == 3
    assert line_map.offset(0, 0) == 0
    assert line_map.offset(1, 0) == 4
    assert line_map.offset(2, 0) == 9
    assert line_map.position(0) == (0, 0)
    assert line_map.position(4) == (1, 0)
    assert line_map.position(9) == (2, 0)
    assert line_map.line_text(1) == "def"


def test_span_contains():
    span = Span(1, 2, 1, 5)
    assert span.contains(1, 2)
    assert span.contains(1, 4)
    assert not span.contains(1, 5)
    assert not span.contains(0, 4)
