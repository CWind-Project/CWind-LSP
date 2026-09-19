from pathlib import Path

import pytest

from cwind_lsp.analysis import AnalysisEngine
from cwind_lsp.config import Settings
from cwind_lsp.project import project_config
from cwind_lsp.vfs import normalize


@pytest.fixture(scope="module")
def engine() -> AnalysisEngine:
    return AnalysisEngine(Settings())


def test_project_config_reads_cwind_table(tmp_path: Path):
    (tmp_path / "cwind-lsp.toml").write_text(
        "[cwind]\ntarget_os = \"linux\"\nno_std = true\n", encoding="utf-8"
    )
    config = project_config(normalize(str(tmp_path)))
    assert config["target_os"] == "linux"
    assert config["no_std"] is True


def test_project_config_overrides_target(engine: AnalysisEngine, tmp_path: Path):
    (tmp_path / "cwind-lsp.toml").write_text(
        "target_os = \"bogus-os\"\n", encoding="utf-8"
    )
    source = tmp_path / "main.wind"
    source.write_text("fn main() {\n    builtins::print(1);\n}\n", encoding="utf-8")
    snapshot = engine.analyze(str(source), {})
    assert snapshot.internal_error is not None
    assert "bogus-os" in snapshot.internal_error
    diagnostics = snapshot.diagnostics.get(normalize(str(source)), [])
    assert any(diagnostic.stage == "internal" for diagnostic in diagnostics)
