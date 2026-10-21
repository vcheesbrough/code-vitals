"""Catch a repo up: record every trunk commit not yet measured, for every
registered measure version.

The one entrypoint behind both the deploy-time backfill and the measured
repos' push workflows. Safe to run concurrently and to interrupt:

- one advisory lock per repo for the whole run, so an overlapping run waits
  and then finds nothing left to do;
- each commit's values and its done-markers (`commit_measure`) are written in
  one transaction with ON CONFLICT DO NOTHING, so a killed run leaves only
  whole commits and the next run finishes the rest;
- a commit that fails to measure does not stop the run: later commits are
  still measured, the run is recorded as a failure (so `freshness` shows it),
  and the failed commit is retried by the next run.

A run row exists from the moment the repo is known, so every failure after
that — a bad clone, a changed measure definition — is recorded too.
"""

import logging
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import psycopg
from opentelemetry import trace

from code_vitals import obs
from code_vitals.build_info import BuildInfo
from code_vitals.gitrepo import Clone
from code_vitals.measures import CommitContext, Measure, Value, registered
from code_vitals.repos import RepoConfig, iteration_of, pr_number_of, version_from

log = logging.getLogger("code_vitals.collect")
Conn = psycopg.Connection[tuple[object, ...]]


class MeasureDefinitionConflict(RuntimeError):
    """A known (name, version) now has a different unit, kind or recomputability:
    the definition changed without a version bump."""


@dataclass(frozen=True)
class RunResult:
    run_id: int
    outcome: obs.Outcome
    backlog: int
    commits_measured: int
    commits_failed: int


def collect(
    conninfo: str,
    repo: RepoConfig,
    clone_path: Path,
    trigger: obs.Trigger,
    build: BuildInfo,
    *,
    measures: Sequence[Measure] | None = None,
    pipeline_number: str | None = None,
) -> RunResult:
    """Catch the repo up. Returns the run's result; raises only for failures
    that stop the whole run (after recording them on the run row)."""
    measures = registered() if measures is None else list(measures)
    started = time.monotonic()
    outcome = obs.Outcome.FAILURE
    attributes = {obs.REPO: repo.name, obs.TRIGGER: str(trigger)}
    if pipeline_number:
        attributes[obs.PIPELINE_NUMBER] = pipeline_number
    with (
        obs.tracer.start_as_current_span("collect", attributes=attributes) as span,
        psycopg.connect(conninfo, autocommit=True) as conn,
    ):
        try:
            repo_id = _ensure_repo(conn, repo)
            run_id = _start_run(conn, repo_id, trigger, build, span)
            try:
                clone = Clone(clone_path)
                head = clone.resolve_trunk(repo.trunk)
                span.set_attribute(obs.HEAD_SHA, head)
                measure_ids = _sync_measures(conn, measures)
                with _RepoLock(conn, repo.name):
                    result = _catch_up(
                        conn, clone, repo, repo_id, run_id, head, measures, measure_ids, trigger
                    )
            except Exception:
                log.exception("collect failed for %s", repo.name)
                _finish_run(conn, run_id, obs.Outcome.FAILURE, 0)
                raise
            outcome = result.outcome
            span.set_attribute(obs.COMMITS_MEASURED_ATTRIBUTE, result.commits_measured)
            if result.commits_failed:
                span.set_status(trace.StatusCode.ERROR, f"{result.commits_failed} commit(s) failed")
            return result
        finally:
            labels = {obs.OUTCOME: str(outcome), obs.TRIGGER: str(trigger)}
            obs.RUNS.add(1, labels)
            obs.RUN_DURATION.record(time.monotonic() - started, labels)


def _ensure_repo(conn: Conn, repo: RepoConfig) -> int:
    with obs.db_span("INSERT", "upsert repo"):
        row = conn.execute(
            "INSERT INTO repo (name, trunk) VALUES (%s, %s)"
            " ON CONFLICT (name) DO UPDATE SET trunk = EXCLUDED.trunk RETURNING id",
            (repo.name, repo.trunk),
        ).fetchone()
    assert row is not None
    return int(str(row[0]))


def _start_run(
    conn: Conn, repo_id: int, trigger: obs.Trigger, build: BuildInfo, span: trace.Span
) -> int:
    context = span.get_span_context()
    # With telemetry off there is no trace to name; record that, not zeros.
    trace_id = format(context.trace_id, "032x") if context.is_valid else None
    with obs.db_span("INSERT", "start run"):
        row = conn.execute(
            "INSERT INTO run (repo_id, trigger, collector_version, collector_revision, trace_id)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (repo_id, str(trigger), build.version, build.revision, trace_id),
        ).fetchone()
    assert row is not None
    return int(str(row[0]))


def _sync_measures(conn: Conn, measures: Sequence[Measure]) -> dict[tuple[str, int], int]:
    ids = {}
    for m in measures:
        with obs.db_span("INSERT", "register measure"):
            conn.execute(
                "INSERT INTO measure (name, version, unit, kind, definition, recomputable)"
                " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (name, version) DO NOTHING",
                (m.name, m.version, m.unit, str(m.kind), m.definition, m.recomputable),
            )
            row = conn.execute(
                "SELECT id, unit, kind::text, recomputable FROM measure"
                " WHERE name = %s AND version = %s",
                (m.name, m.version),
            ).fetchone()
        assert row is not None
        if (row[1], row[2], row[3]) != (m.unit, str(m.kind), m.recomputable):
            raise MeasureDefinitionConflict(
                f"measure {m.name} v{m.version} is recorded as unit={row[1]} kind={row[2]}"
                f" recomputable={row[3]}; a changed definition needs a new version"
            )
        ids[(m.name, m.version)] = int(str(row[0]))
    return ids


def lock_key(repo: str) -> str:
    """The advisory lock key (hashed by Postgres' hashtext) for a repo's runs."""
    return f"code-vitals:collect:{repo}"


class _RepoLock:
    """A session advisory lock per repo, held for the whole run."""

    def __init__(self, conn: Conn, repo: str) -> None:
        self.conn = conn
        self.key = lock_key(repo)

    def __enter__(self) -> None:
        waited_from = time.monotonic()
        with obs.db_span("SELECT", "acquire repo lock"):
            self.conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (self.key,))
        obs.LOCK_WAIT.record(time.monotonic() - waited_from)

    def __exit__(self, *exc: object) -> None:
        with obs.db_span("SELECT", "release repo lock"):
            self.conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (self.key,))


def _catch_up(
    conn: Conn,
    clone: Clone,
    repo: RepoConfig,
    repo_id: int,
    run_id: int,
    head: str,
    measures: Sequence[Measure],
    measure_ids: dict[tuple[str, int], int],
    trigger: obs.Trigger,
) -> RunResult:
    span = trace.get_current_span()
    history = clone.first_parent_history(head)
    with obs.db_span("SELECT", "done markers"):
        done = {
            (str(sha), int(str(measure_id)))
            for sha, measure_id in conn.execute(
                "SELECT c.sha, cm.measure_id FROM commit_measure cm"
                " JOIN commit c ON c.id = cm.commit_id WHERE c.repo_id = %s",
                (repo_id,),
            )
        }
    work = []
    for sha in history:
        missing = [m for m in measures if (sha, measure_ids[(m.name, m.version)]) not in done]
        if missing:
            work.append((sha, missing))
    backlog = len(work)
    span.set_attribute(obs.BACKLOG_COMMITS, backlog)
    obs.BACKLOG.set(backlog, {obs.TRIGGER: str(trigger)})
    log.info(
        "catching up %s: %d of %d trunk commits to measure",
        repo.name,
        backlog,
        len(history),
        extra={obs.REPO: repo.name, obs.HEAD_SHA: head},
    )
    # Every run, not only for new commits: a release tag is pushed after the
    # deploy, i.e. after the push-time run already measured its commit.
    tags = clone.tags_by_commit()
    measured = failed = 0
    for sha, missing in work:
        try:
            _measure_commit(conn, clone, repo, repo_id, sha, missing, measure_ids, tags)
        except Exception:
            failed += 1
            log.exception(
                "measuring %s at %s failed; the next run retries it",
                repo.name,
                sha,
                extra={obs.COMMIT_SHA: sha},
            )
            continue
        measured += 1
        obs.COMMITS_MEASURED.add(1, {obs.TRIGGER: str(trigger)})
    _refresh_tags(conn, repo_id, history, tags)
    outcome = obs.Outcome.FAILURE if failed else obs.Outcome.SUCCESS
    _finish_run(conn, run_id, outcome, measured)
    return RunResult(run_id, outcome, backlog, measured, failed)


def _measure_commit(
    conn: Conn,
    clone: Clone,
    repo: RepoConfig,
    repo_id: int,
    sha: str,
    missing: Sequence[Measure],
    measure_ids: dict[tuple[str, int], int],
    tags: dict[str, list[str]],
) -> None:
    with obs.tracer.start_as_current_span("commit", attributes={obs.COMMIT_SHA: sha}):
        info = clone.commit_info(sha)
        iteration = _iteration_at(clone, repo, sha)
        values: list[tuple[int, Value]] = []
        with tempfile.TemporaryDirectory(prefix="code-vitals-") as tmp:
            tree = clone.extract_tree(sha, Path(tmp))
            if tree.skipped:
                log.info("skipped %d tree entries at %s", tree.skipped, sha)
            context = CommitContext(repo.name, sha, tree.path, tree.files)
            for m in missing:
                started = time.monotonic()
                with obs.tracer.start_as_current_span(
                    "measure",
                    attributes={obs.MEASURE: m.name, "code_vitals.measure.version": m.version},
                ):
                    produced = list(m.compute(context))
                obs.MEASURE_DURATION.record(time.monotonic() - started, {obs.MEASURE: m.name})
                for value in produced:
                    if value.area is not None:
                        raise NotImplementedError("area-split values need area rulesets (#495)")
                    values.append((measure_ids[(m.name, m.version)], value))

        with obs.db_span("INSERT", "record commit"), conn.transaction():
            row = conn.execute(
                "INSERT INTO commit (repo_id, sha, committed_at, author, subject, pr_number,"
                " iteration, tags) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (repo_id, sha) DO UPDATE SET tags = EXCLUDED.tags RETURNING id",
                (
                    repo_id,
                    info.sha,
                    info.committed_at,
                    info.author,
                    info.subject,
                    pr_number_of(info.subject),
                    iteration,
                    tags.get(sha, []),
                ),
            ).fetchone()
            assert row is not None
            commit_id = int(str(row[0]))
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO measurement (commit_id, measure_id, area_id, facet, value)"
                    " VALUES (%s, %s, NULL, %s, %s)"
                    " ON CONFLICT ON CONSTRAINT measurement_key DO NOTHING",
                    [(commit_id, measure_id, v.facet, v.value) for measure_id, v in values],
                )
                cur.executemany(
                    "INSERT INTO commit_measure (commit_id, measure_id) VALUES (%s, %s)"
                    " ON CONFLICT DO NOTHING",
                    [(commit_id, measure_ids[(m.name, m.version)]) for m in missing],
                )


def _refresh_tags(
    conn: Conn, repo_id: int, history: Sequence[str], tags: dict[str, list[str]]
) -> None:
    """Bring every recorded commit's tags in line with the clone's: tags are
    added (and removed) after a commit is measured."""
    on_trunk = set(history)
    tagged = {sha: names for sha, names in tags.items() if sha in on_trunk}
    with obs.db_span("UPDATE", "refresh tags"), conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "UPDATE commit SET tags = %s WHERE repo_id = %s AND sha = %s"
            " AND tags IS DISTINCT FROM %s",
            [(names, repo_id, sha, names) for sha, names in tagged.items()],
        )
        cur.execute(
            "UPDATE commit SET tags = '{}' WHERE repo_id = %s AND tags <> '{}'"
            " AND NOT (sha = ANY(%s))",
            (repo_id, list(tagged)),
        )


def _iteration_at(clone: Clone, repo: RepoConfig, sha: str) -> int | None:
    for source in repo.version_sources:
        text = clone.read_file(sha, source.path)
        if text is None:
            continue
        iteration = iteration_of(version_from(source.kind, text))
        if iteration is not None:
            return iteration
    return None


def _finish_run(conn: Conn, run_id: int, outcome: obs.Outcome, measured: int) -> None:
    with obs.db_span("UPDATE", "finish run"):
        conn.execute(
            "UPDATE run SET outcome = %s, finished_at = now(), commits_measured = %s WHERE id = %s",
            (str(outcome), measured, run_id),
        )
