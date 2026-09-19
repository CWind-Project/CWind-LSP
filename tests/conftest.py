import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_DIR = REPO_ROOT / "example"

os.environ.setdefault("CWIND_HOME", str(REPO_ROOT))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def hello_world() -> Path:
    return EXAMPLE_DIR / "00_hello_world.wind"


@pytest.fixture()
def demo_project(tmp_path: Path) -> Path:
    files = {
        "Breeze.toml": (
            "[package]\n"
            'name = "demo"\n'
            'version = "0.1.0"\n'
            "\n"
            "[entry]\n"
            'source = "src"\n'
            'module = "lib.wd"\n'
        ),
        "src/lib.wd": (
            "pub mod common;\n"
            "pub use common::Helper;\n"
            "pub use common::helper_make;\n"
        ),
        "src/common.wind": (
            "pub struct Helper {\n"
            "    pub value: Int,\n"
            "}\n"
            "\n"
            "pub fn helper_make(v: Int) -> Helper {\n"
            "    return Helper { value: v };\n"
            "}\n"
        ),
        "src/main.wind": (
            "fn main() {\n"
            "    let h: Helper = helper_make(1);\n"
            "    builtins::print(h.value);\n"
            "}\n"
        ),
    }
    for relative, text in files.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def rich_project(tmp_path: Path) -> Path:
    files = {
        "Breeze.toml": (
            "[package]\n"
            'name = "rich"\n'
            'version = "0.1.0"\n'
            "\n"
            "[entry]\n"
            'source = "src"\n'
            'module = "lib.wd"\n'
        ),
        "src/lib.wd": "pub mod common;\n",
        "src/common.wind": (
            "#[macro_export]\n"
            "macro_rules! make_it {\n"
            "    ($v:expr) => {\n"
            "        $v + 1\n"
            "    };\n"
            "}\n"
            "\n"
            "pub struct Helper {\n"
            "    pub value: Int,\n"
            "}\n"
            "\n"
            "pub enum Kind {\n"
            "    A,\n"
            "    B(Int),\n"
            "}\n"
            "\n"
            "extra Helper {\n"
            "    fn double(self) -> Int {\n"
            "        return self.value * 2;\n"
            "    }\n"
            "}\n"
        ),
        "src/main.wind": (
            "use std::option;\n"
            "use std::option::Option;\n"
            "use common::make_it;\n"
            "use common::Helper;\n"
            "use common::Kind;\n"
            "\n"
            "fn main() {\n"
            "    let a: Int = make_it!(1);\n"
            "    let h: Helper = Helper { value: a };\n"
            "    let d: Int = h.double();\n"
            "    let k: Kind = Kind::A;\n"
            "    let o: Option<Int> = Option::Some(d);\n"
            "    match (k) {\n"
            "        Kind::A => {}\n"
            "        Kind::B(v) => { builtins::print(v); }\n"
            "    }\n"
            "    println!(\"x\");\n"
            "}\n"
        ),
    }
    for relative, text in files.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return tmp_path
