"""Churn, operations and e2e scenarios on small fixtures with hand-counted values."""

import tempfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from code_vitals.areas import AreaRuleset, ruleset
from code_vitals.gitrepo import Clone
from code_vitals.measures import CommitContext, Value
from code_vitals.measures.churn import FilesChanged, LinesAdded, LinesDeleted
from code_vitals.measures.functionality import E2eScenarios, Operations
from code_vitals.repos import REPOS

from .gitfixture import make_repo

RULES = ruleset(1, ("app/**", "app"))


def _by(values: Sequence[Value]) -> dict[tuple[str | None, str], float]:
    return {(v.area, v.facet): v.value for v in values}


def _context(
    repo_name: str, clone: Clone, sha: str, parent: str | None, rules: AreaRuleset = RULES
) -> CommitContext:
    tmp = Path(tempfile.mkdtemp())
    tree = clone.extract_tree(sha, tmp)
    return CommitContext(repo_name, sha, tmp, tree.files, rules, parent=parent, clone=clone)


def test_churn_by_area_with_root_commit_binary_rename_and_excluded_files(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    root = repo.commit("root", {"app/a.py": "1\n2\n3\n", "notes.md": "n\n", "Cargo.lock": "x\n"})
    (repo.path / "app" / "logo.png").write_bytes(b"\x89PNG\x00\x01")
    repo.git("add", "app/logo.png")
    repo.git("mv", "app/a.py", "app/b.py")
    second = repo.commit("second", {"app/b.py": "1\n2\n3\n4\n", "Cargo.lock": "y\n"})
    clone = Clone(repo.path)

    first = _context("fixture", clone, root, None)
    assert _by(LinesAdded().compute(first)) == {("app", ""): 3.0, ("tooling", ""): 1.0}
    assert _by(FilesChanged().compute(first)) == {("app", ""): 1.0, ("tooling", ""): 1.0}

    then = _context("fixture", clone, second, root)
    # No rename detection: a.py deleted (3), b.py added (4); the binary file
    # changes but has no lines; the lock file is excluded.
    assert _by(LinesAdded().compute(then)) == {("app", ""): 4.0}
    assert _by(LinesDeleted().compute(then)) == {("app", ""): 3.0}
    assert _by(FilesChanged().compute(then)) == {("app", ""): 3.0}


def _tree_context(tmp_path: Path, repo: str, files: dict[str, str]) -> CommitContext:
    for name, content in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(content)
    return CommitContext(repo, "0" * 40, tmp_path, len(files), REPOS[repo].areas)


def test_bored_operations_skip_test_copies_and_comments(tmp_path: Path) -> None:
    context = _tree_context(
        tmp_path,
        "bored",
        {
            "backend/src/app.rs": (
                'Router::new()\n    .route("/api/cards", get(list))\n'
                '    .route(\n        "/api/cards/{id}",\n        put(update),\n    )\n'
            ),
            "backend/src/tests/outbound_http.rs": '.route("/x", get(x))\n',
            "mcp/src/server.rs": (
                '    #[tool(description = "a")]\n// #[tool(...)] in prose\n'
                '    #[tool(name = "b")]\n'
            ),
        },
    )
    assert _by(Operations().compute(context)) == {
        ("backend", "http_route"): 2.0,
        ("shared", "mcp_tool"): 2.0,
    }


def test_sovereign_grpc_methods_are_distinct_across_protocol_versions(tmp_path: Path) -> None:
    v3 = (
        "service Configuration {\n  rpc Get(GetRequest) returns (Value);\n"
        "  rpc Put(P) returns (V);\n}\n"
    )
    v4 = v3 + "service Audit {\n  rpc QueryAuditTrail(Q) returns (R);\n}\n"
    context = _tree_context(
        tmp_path,
        "sovereign-config",
        {
            "proto/sovereign/config/v3/service.proto": v3,
            "proto/sovereign/config/v4/service.proto": v4,
            "test-consumers/x/vtest/service.proto": "service T {\n  rpc Ping(P) returns (P);\n}\n",
            "crates/sovereign-config-mcp/src/tools.rs": (
                '        "status" => Ok(Status),\n        "get" => Ok(Get),\n'
            ),
            "crates/sovereign-config-mcp/src/server.rs": '            "ping" => out.send(x),\n',
        },
    )
    assert _by(Operations().compute(context)) == {
        ("libraries", "grpc_method"): 3.0,  # Get, Put, QueryAuditTrail — not the test proto
        ("libraries", "mcp_tool"): 2.0,  # tools.rs only; server.rs "ping" is protocol
    }


def test_v_note_scenarios_per_suite(tmp_path: Path) -> None:
    context = _tree_context(
        tmp_path,
        "v-note",
        {
            "e2e/tests/pages.spec.ts": (
                "test.describe('pages', () => {\n  test('creates', async () => {});\n"
                "  test.skip('later', async () => {});\n  test.skip(isMobile, 'reason');\n"
                "  expect(/re/.test(x));\n});\n"
            ),
            "e2e/live/smoke.spec.ts": "test(`live`, async () => {});\n",
            "android/app/src/androidTest/java/A.kt": "@Test\nfun a() {}\n  @Test fun b() {}\n",
            "crates/server/tests/api.rs": "#[test]\nfn a() {}\n#[tokio::test]\nasync fn b() {}\n",
        },
    )
    assert _by(E2eScenarios().compute(context)) == {
        ("tests", "web-e2e"): 2.0,  # not the conditional skip, not /re/.test
        ("tests", "live-smoke"): 1.0,
        ("tests", "android-instrumented"): 2.0,
        ("tests", "rust-integration"): 2.0,
    }


def test_a_rule_change_changes_the_definition(monkeypatch: pytest.MonkeyPatch) -> None:
    # The definition carries a digest of the rules, so the collector refuses a
    # changed rule under an existing version.
    from code_vitals.measures import functionality

    before = functionality._digest(functionality.OPERATIONS)
    changed = {**functionality.OPERATIONS, "v-note": ()}
    assert functionality._digest(changed) != before
    assert before in functionality.Operations.definition
