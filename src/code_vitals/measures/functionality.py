"""Functionality: the operations a repo exposes and the e2e scenarios that test
it, counted with per-repo text patterns.

Pattern extractors are fragile by nature, so the rules are data here and part
of each measure's definition (as a digest): changing any rule changes the
definition, which the collector refuses under a known version — a rule change
always comes with a version bump. Rules were derived from a survey of the
three repos' full histories (card #496).
"""

import hashlib
import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from code_vitals.areas import TESTS
from code_vitals.measures import CommitContext, Kind, Value


@dataclass(frozen=True)
class OperationRule:
    kind: str  # the facet: http_route, grpc_method, mcp_tool
    glob: str  # files to search, relative to the repo root
    pattern: str  # one match per operation (multiline, dotall off)
    # gRPC: count distinct `Service.Method` across files (v3/v4/v5 of one API
    # declare the same methods, all served at once).
    distinct_rpc: bool = False


@dataclass(frozen=True)
class SuiteRule:
    suite: str  # the facet
    glob: str
    pattern: str  # one match per scenario


# An axum route registration with a literal path; the path may be on the next line.
HTTP_ROUTE = r"\.route\(\s*\""
GRPC_RPC = r"^\s*rpc\s+(\w+)\s*\("
# A Playwright scenario: test(...) or test.only/skip/fixme/fail/slow(...) whose
# first argument is a title — not test.skip(condition) or /re/.test(x).
PLAYWRIGHT_TEST = r"^\s*test(\.(only|skip|fixme|fail|slow))?\(\s*['\"`]"
KOTLIN_TEST = r"^\s*@Test\b"
RUST_TEST = r"^\s*#\[(tokio::|sqlx::)?test\b"

OPERATIONS: dict[str, tuple[OperationRule, ...]] = {
    "v-note": (OperationRule("http_route", "crates/server/**/*.rs", HTTP_ROUTE),),
    "bored": (
        OperationRule("http_route", "backend/**/*.rs", HTTP_ROUTE),
        # rmcp tool macros, anchored so comments that mention them don't count.
        OperationRule("mcp_tool", "mcp/**/*.rs", r"^\s*#\[tool\("),
    ),
    "sovereign-config": (
        OperationRule("http_route", "crates/sovereign-config-server/**/*.rs", HTTP_ROUTE),
        OperationRule(
            "http_route", "crates/sovereign-config-woodpecker-broker/**/*.rs", HTTP_ROUTE
        ),
        OperationRule("grpc_method", "proto/**/*.proto", GRPC_RPC, distinct_rpc=True),
        # The hand-rolled MCP server's `parse_call` match arms in tools.rs, one
        # per tool, since the crate appeared (2026-07-23). Not server.rs, whose
        # arms are MCP protocol methods (initialize, ping), not tools.
        OperationRule(
            "mcp_tool", "crates/sovereign-config-mcp/src/tools.rs", r"^\s*\"([a-z_]+)\"\s*=>"
        ),
    ),
}

SUITES: dict[str, tuple[SuiteRule, ...]] = {
    "v-note": (
        SuiteRule("web-e2e", "e2e/tests/**/*.spec.ts", PLAYWRIGHT_TEST),
        SuiteRule("live-smoke", "e2e/live/**/*.spec.ts", PLAYWRIGHT_TEST),
        SuiteRule("android-instrumented", "android/app/src/androidTest/**/*.kt", KOTLIN_TEST),
        SuiteRule("rust-integration", "crates/*/tests/**/*.rs", RUST_TEST),
    ),
    "bored": (SuiteRule("web-e2e", "e2e/tests/**/*.spec.ts", PLAYWRIGHT_TEST),),
    "sovereign-config": (
        SuiteRule("browser", "browser-tests/tests/**/*.spec.js", PLAYWRIGHT_TEST),
        SuiteRule("rust-integration", "crates/*/tests/**/*.rs", RUST_TEST),
        SuiteRule("rust-integration", "test-consumers/*/tests/**/*.rs", RUST_TEST),
    ),
}


def _digest(rules: object) -> str:
    def plain(value: object) -> object:
        if isinstance(value, dict):
            return {k: plain(v) for k, v in value.items()}
        if isinstance(value, tuple | list):
            return [plain(v) for v in value]
        if hasattr(value, "__dataclass_fields__"):
            return {k: plain(getattr(value, k)) for k in value.__dataclass_fields__}
        return value

    text = json.dumps(plain(rules), sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


_SERVICE = re.compile(r"^\s*service\s+(\w+)\s*\{", re.MULTILINE)


def _rpcs(text: str, pattern: str) -> list[str]:
    """`Service.Method` for every rpc `pattern` (group 1 = method) matches."""
    found = []
    service = ""
    for line in text.splitlines():
        if m := _SERVICE.match(line):
            service = m[1]
        elif m := re.match(pattern, line):
            found.append(f"{service}.{m[1]}")
    return found


class Operations:
    name = "operations"
    version = 1
    unit = "operations"
    kind = Kind.SNAPSHOT
    recomputable = True
    definition = (
        "Operations a repo exposes — HTTP routes, gRPC methods (distinct Service.Method), MCP "
        "tools (facet = kind) — by per-repo regex over files outside the tests area, by area; "
        f"rules digest {_digest(OPERATIONS)}."
    )

    def compute(self, context: CommitContext) -> Sequence[Value]:
        counts: Counter[tuple[str, str]] = Counter()
        for rule in OPERATIONS.get(context.repo, ()):
            seen: set[tuple[str, str]] = set()  # (area, Service.Method)
            for path, text in context.text_files(rule.glob):
                area = context.area_of(path)
                if area is None or area == TESTS:
                    continue
                if rule.distinct_rpc:
                    for rpc in _rpcs(text, rule.pattern):
                        if (area, rpc) not in seen:
                            seen.add((area, rpc))
                            counts[(area, rule.kind)] += 1
                else:
                    counts[(area, rule.kind)] += len(re.findall(rule.pattern, text, re.MULTILINE))
        return [
            Value(float(n), facet=kind, area=area) for (area, kind), n in sorted(counts.items())
        ]


class E2eScenarios:
    name = "e2e_scenarios"
    version = 1
    unit = "scenarios"
    kind = Kind.SNAPSHOT
    recomputable = True
    definition = (
        "Scenarios in each repo's end-to-end and integration suites (facet = suite), by per-repo "
        "regex: Playwright test(...) titles, Kotlin @Test, Rust #[test]/#[tokio::test]/"
        f"#[sqlx::test]; by area; rules digest {_digest(SUITES)}."
    )

    def compute(self, context: CommitContext) -> Sequence[Value]:
        counts: Counter[tuple[str, str]] = Counter()
        for rule in SUITES.get(context.repo, ()):
            for path, text in context.text_files(rule.glob):
                area = context.area_of(path)
                assert area is not None
                counts[(area, rule.suite)] += len(re.findall(rule.pattern, text, re.MULTILINE))
        return [
            Value(float(n), facet=suite, area=area) for (area, suite), n in sorted(counts.items())
        ]
