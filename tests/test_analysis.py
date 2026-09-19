from pathlib import Path

import pytest

from cwind_lsp.analysis import AnalysisEngine
from cwind_lsp.config import Settings
from cwind_lsp.formatting import render_signature
from cwind_lsp.semantic import semantic_tokens
from cwind_lsp.vfs import normalize


@pytest.fixture(scope="module")
def engine() -> AnalysisEngine:
    return AnalysisEngine(Settings())


def test_hello_world_analysis(engine: AnalysisEngine, hello_world: Path):
    snapshot = engine.analyze(str(hello_world), {})
    assert snapshot.internal_error is None
    assert snapshot.diagnostics == {}
    index = snapshot.files[normalize(str(hello_world))]
    main = index.occurrence_named("main")
    assert main is not None and main.is_decl
    print_occ = index.occurrence_named("print")
    assert print_occ is not None
    definitions = snapshot.find_definition(print_occ)
    assert definitions
    assert "builtins" in definitions[0].file


def test_hover_and_semantics(engine: AnalysisEngine, hello_world: Path):
    snapshot = engine.analyze(str(hello_world), {})
    index = snapshot.files[normalize(str(hello_world))]
    main = index.occurrence_named("main")
    assert main is not None
    hover = snapshot.hover_markdown(main)
    assert hover is not None and "fn main" in hover
    tokens = semantic_tokens(
        str(hello_world),
        index.source,
        _codec(),
        index,
    )
    assert len(tokens.data) > 0
    assert len(tokens.data) % 5 == 0


def test_diagnostics_are_published_for_genuine_errors(engine: AnalysisEngine, tmp_path: Path):
    bad = tmp_path / "bad.wind"
    bad.write_text('fn main() {\n    let x: Int = "hello";\n}\n', encoding="utf-8")
    snapshot = engine.analyze(str(bad), {})
    diagnostics = snapshot.diagnostics.get(normalize(str(bad)), [])
    assert diagnostics, snapshot.internal_error
    assert all(diag.severity == 1 for diag in diagnostics)


def test_method_references_across_impls(engine: AnalysisEngine, repo_root: Path):
    path = repo_root / "example" / "05_trait.wind"
    snapshot = engine.analyze(str(path), {})
    index = snapshot.files[normalize(str(path))]
    use = index.occurrence_at(24, 26)
    assert use is not None and use.role == "method"
    references = snapshot.find_references(use)
    lines = {reference.span.line for reference in references}
    assert 1 in lines  # trait declaration
    assert 6 in lines  # impl declaration
    assert 24 in lines


def test_cross_file_definition(engine: AnalysisEngine, demo_project: Path):
    entry = demo_project / "src" / "main.wind"
    snapshot = engine.analyze(str(entry), {})
    assert snapshot.internal_error is None, snapshot.diagnostics
    main_index = snapshot.files[normalize(str(entry))]
    assert main_index is not None
    call = main_index.occurrence_named("helper_make")
    assert call is not None, [o.name for o in main_index.occurrences][:40]
    definitions = snapshot.find_definition(call)
    assert definitions
    assert definitions[0].file.endswith("common.wind")


def test_cross_file_rename_references(engine: AnalysisEngine, demo_project: Path):
    entry = demo_project / "src" / "main.wind"
    snapshot = engine.analyze(str(entry), {})
    main_index = snapshot.files[normalize(str(entry))]
    call = main_index.occurrence_named("helper_make")
    assert call is not None
    names = {Path(reference.file).name for reference in snapshot.find_references(call)}
    assert "main.wind" in names
    assert "common.wind" in names
    assert "lib.wd" in names


def test_type_reference_resolves_to_declaration(engine: AnalysisEngine, demo_project: Path):
    entry = demo_project / "src" / "main.wind"
    snapshot = engine.analyze(str(entry), {})
    main_index = snapshot.files[normalize(str(entry))]
    helper = main_index.occurrence_named("Helper")
    assert helper is not None
    references = snapshot.find_references(helper)
    assert any(reference.is_decl for reference in references)
    assert any(Path(reference.file).name == "common.wind" for reference in references)


def test_navigation_modules_macros_types_variants(
    engine: AnalysisEngine, rich_project: Path
):
    entry = rich_project / "src" / "main.wind"
    snapshot = engine.analyze(str(entry), {})
    assert snapshot.internal_error is None
    index = snapshot.files[normalize(str(entry))]
    expectations = {
        "std": "mod.wind",
        "option": "option.wind",
        "Option": "option.wind",
        "Some": "option.wind",
        "make_it": "common.wind",
        "Helper": "common.wind",
        "Kind": "common.wind",
        "A": "common.wind",
        "double": "common.wind",
        "println": "print.wind",
    }
    for name, expected in expectations.items():
        occurrence = index.occurrence_named(name)
        assert occurrence is not None, name
        definitions = snapshot.find_definition(occurrence)
        assert definitions, name
        assert Path(definitions[0].file).name == expected, (name, definitions)


def test_macro_hover_reports_definition(engine: AnalysisEngine, rich_project: Path):
    entry = rich_project / "src" / "main.wind"
    snapshot = engine.analyze(str(entry), {})
    index = snapshot.files[normalize(str(entry))]
    macro = index.occurrence_named("make_it")
    assert macro is not None
    hover = snapshot.hover_markdown(macro)
    assert hover is not None and "make_it" in hover


def test_variadic_builtin_signature(engine: AnalysisEngine, hello_world: Path):
    snapshot = engine.analyze(str(hello_world), {})
    surface = snapshot.builtin_surface
    assert surface is not None
    format_method = next(
        method for method in surface.methods.get("String", ()) if method.name == "format"
    )
    assert (
        render_signature(format_method)
        == 'extern "CWind" fn String::format(&self, ...) -> String'
    )


def test_signature_rendering(engine: AnalysisEngine, hello_world: Path):
    snapshot = engine.analyze(str(hello_world), {})
    index = snapshot.files[normalize(str(hello_world))]
    from cwind_frontend.ast_components.ast import FnDecl

    main = index.occurrence_named("main")
    assert main is not None and isinstance(main.node, FnDecl)
    assert render_signature(main.node) == "fn main() -> None"


def _codec():
    from pygls.workspace.position_codec import PositionCodec

    return PositionCodec()
