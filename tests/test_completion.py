from pathlib import Path

import pytest

from cwind_lsp.analysis import AnalysisEngine
from cwind_lsp.completion import CompletionRequest, complete
from cwind_lsp.config import Settings
from cwind_lsp.positions import LineMap
from cwind_lsp.vfs import normalize


@pytest.fixture(scope="module")
def engine() -> AnalysisEngine:
    return AnalysisEngine(Settings())


def _complete_at(snapshot, path: Path, text: str, needle: str):
    position = text.index(needle) + len(needle)
    line, character = LineMap(text).position(position)
    return complete(
        CompletionRequest(
            snapshot=snapshot,
            path=str(path),
            line=line,
            character=character,
            text=text,
        )
    )


def test_member_completion_uses_inferred_type(engine: AnalysisEngine, repo_root: Path):
    path = repo_root / "example" / "05_trait.wind"
    snapshot = engine.analyze(str(path), {})
    source = snapshot.files[normalize(str(path))].source
    text = source.replace("Data1.to_json()", "Data1.")
    items = _complete_at(snapshot, path, text, "Data1.")
    labels = {item.label for item in items}
    assert {"to_json", "get", "set", "remove"} <= labels


def test_path_completion_for_builtins(engine: AnalysisEngine, repo_root: Path):
    path = repo_root / "example" / "05_trait.wind"
    snapshot = engine.analyze(str(path), {})
    source = snapshot.files[normalize(str(path))].source
    text = source.replace("Data1.to_json()", "builtins::")
    items = _complete_at(snapshot, path, text, "builtins::")
    labels = {item.label for item in items}
    assert "print" in labels or "println" in labels
    assert "String" in labels


def test_scope_completion_contains_locals(engine: AnalysisEngine, repo_root: Path):
    path = repo_root / "example" / "05_trait.wind"
    snapshot = engine.analyze(str(path), {})
    source = snapshot.files[normalize(str(path))].source
    text = source.replace("result += ", "res")
    items = _complete_at(snapshot, path, text, "res")
    assert any(item.label == "result" for item in items)


def test_type_position_prefers_types_over_macros(
    engine: AnalysisEngine, tmp_path: Path
):
    source = "fn main() {\n    let x: Stri\n}\n"
    path = tmp_path / "main.wind"
    path.write_text(source, encoding="utf-8")
    snapshot = engine.analyze(str(path), {})
    items = _complete_at(snapshot, path, source, "Stri")
    labels = [item.label for item in items]
    assert "String" in labels
    if "stringify" in labels:
        assert labels.index("String") < labels.index("stringify")


def test_cross_file_member_completion(engine: AnalysisEngine, demo_project: Path):
    entry = demo_project / "src" / "main.wind"
    snapshot = engine.analyze(str(entry), {})
    source = snapshot.files[normalize(str(entry))].source
    text = source.replace("h.value", "h.")
    items = _complete_at(snapshot, entry, text, "h.")
    labels = {item.label for item in items}
    assert "value" in labels
