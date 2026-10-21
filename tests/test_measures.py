"""The size and complexity measures on a small hand-written tree, with values
worked out by hand (tokei and lizard as pinned)."""

from pathlib import Path

import pytest

from code_vitals import analysis
from code_vitals.areas import ruleset
from code_vitals.measures import CommitContext, FunctionRow, Value, registered
from code_vitals.measures.complexity import (
    ComplexityTotal,
    FunctionHotspots,
    Functions,
    FunctionsOver10,
    MaxFunctionComplexity,
    Nloc,
)
from code_vitals.measures.size import LinesOfCode, SourceFiles

APP_RS = """\
// A comment line.
fn simple() -> u32 {
    1
}

fn branchy(x: u32, y: u32) -> u32 {
    if x > 1 && y > 1 {
        return 2;
    }
    match x {
        0 => 0,
        1 => 1,
        _ => 3,
    }
}
"""

TEST_KT = """\
class T {
    fun check(a: Int): Int {
        return if (a > 0) a else -a
    }
}
"""

RULES = ruleset(1, ("app/**", "app"))


@pytest.fixture
def context(tmp_path: Path) -> CommitContext:
    files = {
        "app/src/main.rs": APP_RS,
        "app/tests/check.kt": TEST_KT,
        "notes.md": "# Notes\n\nSome text.\n",
        "Cargo.lock": "# generated\n[[package]]\nname = 'x'\n",
    }
    for name, content in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(content)
    return CommitContext("fixture", "0" * 40, tmp_path, len(files), RULES)


def _by(values: list[Value]) -> dict[tuple[str | None, str], float]:
    return {(v.area, v.facet): v.value for v in values}


def test_lines_of_code_by_area_and_language(context: CommitContext) -> None:
    assert _by(list(LinesOfCode().compute(context))) == {
        ("app", "Rust"): 13.0,  # 15 lines - 1 comment - 1 blank
        ("tests", "Kotlin"): 5.0,
        ("tooling", "Markdown"): 0.0,  # tokei counts Markdown prose as comments
    }


def test_source_files_by_area_and_language(context: CommitContext) -> None:
    assert _by(list(SourceFiles().compute(context))) == {
        ("app", "Rust"): 1.0,
        ("tests", "Kotlin"): 1.0,
        ("tooling", "Markdown"): 1.0,
    }  # Cargo.lock is excluded


def test_lizard_measures_by_area(context: CommitContext) -> None:
    assert _by(list(Functions().compute(context))) == {("app", ""): 2.0, ("tests", ""): 1.0}
    # simple: 1; branchy: 1 + if + && + 2 match arms (lizard counts `_` as default) = 5
    assert _by(list(ComplexityTotal().compute(context))) == {("app", ""): 6.0, ("tests", ""): 2.0}
    assert _by(list(MaxFunctionComplexity().compute(context))) == {
        ("app", ""): 5.0,
        ("tests", ""): 2.0,
    }
    assert _by(list(FunctionsOver10().compute(context))) == {("app", ""): 0.0, ("tests", ""): 0.0}
    nloc = _by(list(Nloc().compute(context)))
    assert nloc[("app", "")] == 13.0 and nloc[("tests", "")] == 5.0


def test_hotspot_rows(context: CommitContext) -> None:
    rows = sorted(FunctionHotspots().compute(context), key=lambda r: (r.file, r.start_line))
    assert [(r.area, r.file, r.function, r.start_line, r.ccn, r.params) for r in rows] == [
        ("app", "app/src/main.rs", "simple", 2, 1, 0),
        ("app", "app/src/main.rs", "branchy", 6, 5, 2),
        ("tests", "app/tests/check.kt", "check", 2, 2, 1),
    ]
    assert all(isinstance(r, FunctionRow) for r in rows)


def test_tokei_and_lizard_run_once_per_commit(
    context: CommitContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"tokei": 0, "lizard": 0}
    real_tokei, real_lizard = analysis.run_tokei, analysis.run_lizard

    def counting_tokei(tree: Path) -> list[analysis.TokeiFile]:
        calls["tokei"] += 1
        return real_tokei(tree)

    def counting_lizard(tree: Path, paths: list[str]) -> list[analysis.LizardFile]:
        calls["lizard"] += 1
        return real_lizard(tree, paths)

    monkeypatch.setattr(analysis, "run_tokei", counting_tokei)
    monkeypatch.setattr(analysis, "run_lizard", counting_lizard)
    for measure in registered():
        list(measure.compute(context))
    assert calls == {"tokei": 1, "lizard": 1}


def test_a_different_tokei_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(analysis, "_checked_tokei", [])
    monkeypatch.setattr(analysis, "tokei_version", lambda: "16.0.0")
    with pytest.raises(analysis.AnalysisError, match=r"defined for 15\.0\.0"):
        analysis.run_tokei(tmp_path)


def test_tool_based_definitions_name_the_pinned_tool_versions() -> None:
    # Bumping a pin changes these definitions, which the collector refuses
    # under an existing (name, version): the bump must come with a new version.
    root = Path(__file__).resolve().parent.parent
    tokei_pin = next(
        line.split("=", 1)[1]
        for line in (root / "tool-versions.env").read_text().splitlines()
        if line.startswith("TOKEI_VERSION=")
    )
    assert tokei_pin == analysis.TOKEI_VERSION
    assert f'"lizard=={analysis.LIZARD_VERSION}"' in (root / "pyproject.toml").read_text()
    for measure in registered():
        if measure.name in {"lines_of_code", "source_files"}:
            assert f"tokei {tokei_pin}" in measure.definition
        if measure.name in {"nloc", "functions", "complexity_total", "functions_over_10",
                            "max_function_complexity", "function_complexity"}:  # fmt: skip
            assert f"lizard {analysis.LIZARD_VERSION}" in measure.definition


def test_embedded_code_counts_in_its_host_files_language(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("# Doc\n\nText.\n\n```rust\nfn a() {}\nfn b() {}\n```\n")
    (tmp_path / "page.html").write_text(
        "<html>\n<body>\n<script>\nvar a = 1;\nvar b = 2;\n</script>\n</body>\n</html>\n"
    )
    context = CommitContext("fixture", "0" * 40, tmp_path, 2, RULES)
    assert _by(list(LinesOfCode().compute(context))) == {
        ("tooling", "Markdown"): 2.0,  # the two Rust lines in its code block
        ("tooling", "HTML"): 8.0,  # 6 of markup + 2 of embedded JavaScript
    }
