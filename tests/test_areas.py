import pytest

from code_vitals.areas import GENERIC, TESTS, TOOLING, AreaRuleset, ruleset
from code_vitals.repos import REPOS


def test_a_ruleset_must_end_with_the_catch_all() -> None:
    with pytest.raises(ValueError, match="catch-all"):
        AreaRuleset(1, (), (("src/**", "app"),))


def test_first_match_wins_and_excludes_come_first() -> None:
    rules = ruleset(1, ("app/**", "app"))
    assert rules.area_of("app/main.rs") == "app"
    assert rules.area_of("app/tests/it.rs") == TESTS  # tests are listed before areas
    assert rules.area_of("app/target/debug/x") is None  # excluded before anything
    assert rules.area_of("README.md") == TOOLING
    assert rules.areas == (TESTS, "app", TOOLING)


@pytest.mark.parametrize(
    "path",
    ["Cargo.lock", "frontend/package-lock.json", "web-dist/app.js", "node_modules/x/y.js",
     "android/gradle/wrapper/gradle-wrapper.jar", "browser-tests/screenshots/a.png"],
)  # fmt: skip
def test_generated_and_vendored_files_are_never_measured(path: str) -> None:
    assert GENERIC.area_of(path) is None


@pytest.mark.parametrize(
    "path",
    ["e2e/tests/a.spec.ts", "crates/x/tests/it.rs", "crates/x/src/tests.rs",
     "crates/x/src/query_tests.rs", "crates/x/src/test_support.rs",
     "android/app/src/test/T.kt", "android/app/src/androidTest/I.kt",
     "test-consumers/c/src/lib.rs", "frontend/src/a.test.ts"],
)  # fmt: skip
def test_test_code_is_tests_in_every_repo(path: str) -> None:
    for repo in REPOS.values():
        assert repo.areas.area_of(path) == TESTS, (repo.name, path)


@pytest.mark.parametrize(
    ("repo", "path", "area"),
    [
        ("v-note", "crates/server/src/lib.rs", "server"),
        ("v-note", "crates/protocol/src/lib.rs", "server"),
        ("v-note", "frontend/src/app.rs", "web"),
        ("v-note", "android/app/src/main/A.kt", "android"),
        ("v-note", "scripts/deploy.sh", "tooling"),
        ("bored", "backend/src/app.rs", "backend"),
        ("bored", "backend/src/tests/outbound_http.rs", "tests"),
        ("bored", "frontend/src/components/card.rs", "web"),
        ("bored", "shared/src/lib.rs", "shared"),
        ("bored", "mcp/src/server.rs", "shared"),
        ("bored", "agent/src/bored.rs", "tooling"),  # the removed POC crate
        ("sovereign-config", "crates/sovereign-config-server/src/main.rs", "server"),
        ("sovereign-config", "crates/sovereign-config-woodpecker-broker/src/handler.rs", "server"),
        ("sovereign-config", "crates/sovereign-config-web/src/lib.rs", "web"),
        ("sovereign-config", "crates/sovereign-config-core/src/lib.rs", "libraries"),
        ("sovereign-config", "proto/sovereign/config/v5/service.proto", "libraries"),
    ],
)
def test_each_repos_areas(repo: str, path: str, area: str) -> None:
    assert REPOS[repo].areas.area_of(path) == area


def test_rules_round_trip_as_json() -> None:
    rules = REPOS["bored"].areas.as_json()
    assert rules["rules"][-1] == ["**", "tooling"]
    assert "**/*.lock" in rules["exclude"]
