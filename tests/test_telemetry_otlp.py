"""The observability contract, asserted on what a collector would receive.

Each test runs the real CLI in a subprocess (telemetry providers are global per
process) against a migrated database, a fixture repository and either the
fake OTLP receiver, a black-holed endpoint, or no configuration at all.
"""

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from pathlib import Path

import psycopg
import pytest

from code_vitals.db.migrate import migrate
from code_vitals.measures import registered
from code_vitals.obs import METRIC_ATTRIBUTE_KEYS

from .conftest import TEST_PASSWORDS, role_url
from .gitfixture import FixtureRepo, three_commit_history
from .otlp_receiver import OtlpReceiver, attrs

RUN = (
    "import sys; from code_vitals import cli;"
    " from code_vitals.repos import RepoConfig, VersionSource, VersionFile;"
    " sys.exit(cli.main(sys.argv[1:], repos={'fixture': RepoConfig('fixture', 'main',"
    " (VersionSource('pyproject.toml', VersionFile.PYPROJECT),))}))"
)


@pytest.fixture
def writer(fresh_db: str) -> Iterator[str]:
    migrate(fresh_db, TEST_PASSWORDS)
    yield role_url(fresh_db, "code_vitals_writer")


@pytest.fixture
def repo(tmp_path: Path) -> FixtureRepo:
    return three_commit_history(tmp_path)


def run_collect(
    writer: str, repo: FixtureRepo, otel: Mapping[str, str], timeout: float = 60
) -> tuple[subprocess.CompletedProcess[str], float]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    env.update({"CODE_VITALS_DATABASE_URL": writer, "CI_PIPELINE_NUMBER": "42", **otel})
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", RUN, "collect", "--repo", "fixture", "--clone", str(repo.path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result, time.monotonic() - started


def otel_env(endpoint: str) -> dict[str, str]:
    return {
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment.name=test",
    }


def test_all_three_signals_arrive_with_identity_and_correlation(
    writer: str, repo: FixtureRepo
) -> None:
    with OtlpReceiver() as receiver:
        result, _ = run_collect(writer, repo, otel_env(receiver.endpoint))
    assert result.returncode == 0, result.stderr
    got = receiver.received

    # Resource attributes, identical on every signal.
    resources = (
        [r for r, _ in got.spans()] + [r for r, _ in got.log_records()]
        + [r for r, _, _ in got.metric_points()]
    )  # fmt: skip
    assert resources
    for resource in resources:
        assert resource["service.name"] == "code-vitals-collector"
        assert resource["service.instance.id"] == "fixture"
        assert resource["deployment.environment.name"] == "test"
        assert resource["telemetry_source"] == "otlp"
        assert resource["service.version"]

    # Spans: one root per run, a child per commit and measure, git and db calls.
    spans = [span for _, span in got.spans()]
    names = [s.name for s in spans]
    (root,) = [s for s in spans if s.name == "collect"]
    assert attrs(root.attributes)["code_vitals.ci.pipeline.number"] == "42"
    assert attrs(root.attributes)["code_vitals.commits.measured"] == 3
    measures = len(registered())
    assert names.count("commit") == 3 and names.count("measure") == 3 * measures
    assert "git cat-file" in names and "INSERT record commit" in names
    db = next(s for s in spans if s.name == "INSERT record commit")
    db_attributes = attrs(db.attributes)
    assert db_attributes["db.system.name"] == "postgresql"
    assert "INSERT INTO" not in str(db_attributes)  # no SQL text on spans
    assert {s.trace_id for s in spans} == {root.trace_id}

    # Logs carry the trace and span they were written in.
    records = [record for _, record in got.log_records()]
    catching_up = next(r for r in records if "catching up" in r.body.string_value)
    assert catching_up.trace_id == root.trace_id
    assert catching_up.span_id == root.span_id

    # The run row names the trace.
    with psycopg.connect(writer) as conn:
        (trace_id,) = conn.execute("SELECT trace_id FROM run").fetchone() or ("",)
    assert trace_id == root.trace_id.hex()


def test_metrics_carry_only_bounded_attribute_keys(writer: str, repo: FixtureRepo) -> None:
    with OtlpReceiver() as receiver:
        result, _ = run_collect(writer, repo, otel_env(receiver.endpoint))
    assert result.returncode == 0, result.stderr

    points = list(receiver.received.metric_points())
    names = {name for _, name, _ in points}
    assert {
        "code_vitals_collector.runs",
        "code_vitals_collector.run.duration",
        "code_vitals_collector.commits.measured",
        "code_vitals_collector.measure.duration",
        "code_vitals_collector.backlog",
        "code_vitals_collector.lock.wait",
        "code_vitals_collector.build.info",
    } <= names
    for _, name, point_attributes in points:
        unexpected = set(point_attributes) - METRIC_ATTRIBUTE_KEYS
        assert not unexpected, f"{name} carries unbounded attribute keys {unexpected}"
    build = next(a for _, n, a in points if n == "code_vitals_collector.build.info")
    assert set(build) == {"version", "revision"}


def test_a_black_holed_collector_never_holds_the_run(writer: str, repo: FixtureRepo) -> None:
    # Accepts connections and never answers: every export waits its timeout.
    hole = socket.socket()
    hole.bind(("127.0.0.1", 0))
    hole.listen(64)
    host, port = hole.getsockname()
    try:
        result, elapsed = run_collect(writer, repo, otel_env(f"http://{host}:{port}"))
    finally:
        hole.close()

    assert result.returncode == 0, result.stderr
    assert "3 commit(s) measured" in result.stdout
    # The work takes ~1-2 s; the shutdown budget is 5 s.
    assert elapsed < 15, f"took {elapsed:.1f}s"


def test_no_otel_variables_runs_without_telemetry(writer: str, repo: FixtureRepo) -> None:
    result, _ = run_collect(writer, repo, {})
    assert result.returncode == 0, result.stderr
    assert "telemetry off (no OTEL_* variables set)" in result.stdout
    assert "3 commit(s) measured" in result.stdout


def test_sdk_disabled_runs_without_telemetry(writer: str, repo: FixtureRepo) -> None:
    with OtlpReceiver() as receiver:
        env = {**otel_env(receiver.endpoint), "OTEL_SDK_DISABLED": "true"}
        result, _ = run_collect(writer, repo, env)
    assert result.returncode == 0, result.stderr
    assert "telemetry off (OTEL_SDK_DISABLED=true)" in result.stdout
    received = receiver.received
    assert not received.traces and not received.metrics and not received.logs


def test_a_half_configured_set_fails_startup(writer: str, repo: FixtureRepo) -> None:
    result, _ = run_collect(writer, repo, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://x:4318"})
    assert result.returncode == 2
    assert "telemetry misconfigured" in result.stderr
    with psycopg.connect(writer) as conn:
        assert conn.execute("SELECT count(*) FROM run").fetchone() == (0,)


def test_failures_reach_the_ci_log_with_their_traceback(writer: str, tmp_path: Path) -> None:
    not_a_clone = FixtureRepo(tmp_path)  # an empty directory
    result, _ = run_collect(writer, not_a_clone, {})
    assert result.returncode == 1
    assert "code-vitals: ERROR code_vitals.collect: collect failed for fixture" in result.stderr
    assert "Traceback" in result.stderr
