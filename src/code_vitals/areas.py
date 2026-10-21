"""Area rulesets: how a repo's paths are split into areas.

A ruleset is versioned like a measure: its rules decide which values land in
which area, so changing them changes measured values and needs a new version
(the collector refuses a known version whose rules changed). Paths use
`/` separators relative to the repo root; patterns are globs matched with
PurePosixPath.full_match (`**` spans directories). Excluded paths are not
measured at all; every other path lands in the first area whose pattern
matches, and the last rule of every ruleset catches everything else.
"""

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

TESTS = "tests"
TOOLING = "tooling"


@dataclass(frozen=True)
class AreaRuleset:
    version: int
    exclude: tuple[str, ...]
    rules: tuple[tuple[str, str], ...]  # (glob, area), first match wins

    def __post_init__(self) -> None:
        if not self.rules or self.rules[-1] != ("**", TOOLING):
            raise ValueError("a ruleset must end with the ('**', 'tooling') catch-all")

    @property
    def areas(self) -> tuple[str, ...]:
        """Area names in rule order, each once."""
        return tuple(dict.fromkeys(area for _, area in self.rules))

    def area_of(self, path: str) -> str | None:
        """The path's area, or None if it is excluded from measurement."""
        p = PurePosixPath(path)
        if any(p.full_match(pattern) for pattern in self.exclude):
            return None
        for pattern, area in self.rules:
            if p.full_match(pattern):
                return area
        raise AssertionError("unreachable: the catch-all matches every path")

    def as_json(self) -> dict[str, Any]:
        return {"exclude": list(self.exclude), "rules": [list(rule) for rule in self.rules]}


# Never measured, in any repo: build output, dependencies, generated bundles,
# lock files, binaries, keys, IDE settings and one-off dumps.
SHARED_EXCLUDE = (
    "**/target/**",
    "**/node_modules/**",
    "**/dist/**",
    "**/build/**",
    "**/.gradle/**",
    "web-dist/**",  # sovereign-config's committed web bundle (2026-07 to 2026-10)
    "**/*.lock",
    "**/package-lock.json",
    "**/pnpm-lock.yaml",
    "**/gradle/wrapper/**",
    "**/*.jar",
    "**/*.keystore",
    "**/*.pem",
    "**/*.der",
    "**/*-baseline.xml",  # detekt and Android lint baselines
    "**/screenshots/**",
    ".idea/**",
    ".run/**",
    "bored-cards-backup-*.md",
)

# Tests, whatever the repo: e2e and browser suites, test crates and dirs, test
# modules and files. Listed before the product areas so that a crate's tests
# count as tests, not as the crate's area.
TEST_RULES = (
    ("e2e/**", TESTS),
    ("browser-tests/**", TESTS),
    ("test-consumers/**", TESTS),
    ("test-contracts/**", TESTS),
    ("contracts/fixtures/**", TESTS),
    ("**/tests/**", TESTS),
    ("**/test/**", TESTS),
    ("**/androidTest/**", TESTS),
    ("**/testdata/**", TESTS),
    ("**/tests.rs", TESTS),
    ("**/*_test.rs", TESTS),
    ("**/*_tests.rs", TESTS),
    ("**/test_support.rs", TESTS),
    ("**/*.spec.*", TESTS),
    ("**/*.test.*", TESTS),
)

CATCH_ALL = ("**", TOOLING)


def ruleset(version: int, *areas: tuple[str, str]) -> AreaRuleset:
    """Shared excludes, then tests, then the repo's own areas, then tooling."""
    return AreaRuleset(version, SHARED_EXCLUDE, (*TEST_RULES, *areas, CATCH_ALL))


# For repos without rules of their own (and tests): tests vs everything else.
GENERIC = ruleset(1)
