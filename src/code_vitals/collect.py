"""Catch a repo up: record every trunk commit not yet measured, for every
registered measure version.

The one entrypoint behind both the deploy-time backfill and the measured
repos' push workflows. Safe to run concurrently and to interrupt:

- one advisory lock per repo for the whole run, so an overlapping run waits
  and then finds nothing left to do;
- each commit's values and its done-markers (`commit_measure`) are written in
  one transaction with ON CONFLICT DO NOTHING, so a killed run leaves only
  whole commits and the next run finishes the rest.
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


class MeasureDefinitionConflict(RuntimeError):
    """A known (name, version) now has a different unit, kind or recomputability:
    the definition changed without a version bump."""


@dataclass(frozen=True)
class RunResult:
    run_id: int
    outcome: obs.Outcome
    backlog: int
    commits_measured: int


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
            clone = Clone(clone_path)
            head = clone.resolve_trunk(repo.trunk)
            span.set_attribute(obs.HEAD_SHA, head)
            repo_id = _ensure_repo(conn, repo)
            measure_ids = _sync_measures(conn, measures)
            with _repo_lock(conn, repo.name):
                result = _catch_up(
                    conn, clone, repo, repo_id, head, measures, measure_ids, trigger, build, span
                )
            outcome = result.outcome
            return result
        finally:
            obs.RUNS.add(1, {obs.OUTCOME: str(outcome), obs.TRIGGER: str(trigger)})
            obs.RUN_DURATION.record(
                time.monotonic() - started, {obs.OUTCOME: str(outcome), obs.TRIGGER: str(trigger)}
            )


def _ensure_repo(conn: psycopg.Connection[tuple[object, ...]], repo: RepoConfig) -> int:
    with obs.db_span("INSERT", "upsert repo"):
        row = conn.execute(
            "INSERT INTO repo (name, trunk) VALUES (%s, %s)"
            " ON CONFLICT (name) DO UPDATE SET trunk = EXCLUDED.trunk RETURNING id",
            (repo.name, repo.trunk),
        ).fetchone()
    assert row is not None
    return int(str(row[0]))


def _sync_measures(
    conn: psycopg.Connection[tuple[object, ...]], measures: Sequence[Measure]
) -> dict[tuple[str, int], int]:
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


class _repo_lock:
    """A session advisory lock per repo, held for the whole run."""

    def __init__(self, conn: psycopg.Connection[tuple[object, ...]], repo: str) -> None:
        self.conn = conn
        self.key = f"code-vitals:collect:{repo}"

    def __enter__(self) -> None:
        waited_from = time.monotonic()
        with obs.db_span("SELECT", "acquire repo lock"):
            self.conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (self.key,))
        obs.LOCK_WAIT.record(time.monotonic() - waited_from)

    def __exit__(self, *exc: object) -> None:
        with obs.db_span("SELECT", "release repo lock"):
            self.conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (self.key,))


def _catch_up(
    conn: psycopg.Connection[tuple[object, ...]],
    clone: Clone,
    repo: RepoConfig,
    repo_id: int,
    head: str,
    measures: Sequence[Measure],
    measure_ids: dict[tuple[str, int], int],
    trigger: obs.Trigger,
    build: BuildInfo,
    span: trace.Span,
) -> RunResult:
    trace_id = format(span.get_span_context().trace_id, "032x")
    with obs.db_span("INSERT", "start run"):
        row = conn.execute(
            "INSERT INTO run (repo_id, trigger, collector_version, collector_revision, trace_id)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (repo_id, str(trigger), build.version, build.revision, trace_id),
        ).fetchone()
    assert row is not None
    run_id = int(str(row[0]))
    measured = 0
    try:
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
        tags = clone.tags_by_commit() if work else {}
        for sha, missing in work:
            _measure_commit(conn, clone, repo, repo_id, sha, missing, measure_ids, tags)
            measured += 1
            obs.COMMITS_MEASURED.add(1, {obs.TRIGGER: str(trigger)})
        _finish_run(conn, run_id, obs.Outcome.SUCCESS, measured)
        span.set_attribute(obs.COMMITS_MEASURED_ATTRIBUTE, measured)
        return RunResult(run_id, obs.Outcome.SUCCESS, backlog, measured)
    except Exception:
        log.exception("collect failed for %s after %d commits", repo.name, measured)
        _finish_run(conn, run_id, obs.Outcome.FAILURE, measured)
        raise


def _measure_commit(
    conn: psycopg.Connection[tuple[object, ...]],
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
                log.warning("skipped %d unsafe tree members at %s", tree.skipped, sha)
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
                        raise NotImplementedError(
                            "area-split values need area rulesets (card #495)"
                        )
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


def _iteration_at(clone: Clone, repo: RepoConfig, sha: str) -> int | None:
    for source in repo.version_sources:
        text = clone.read_file(sha, source.path)
        if text is None:
            continue
        iteration = iteration_of(version_from(source.kind, text))
        if iteration is not None:
            return iteration
    return None


def _finish_run(
    conn: psycopg.Connection[tuple[object, ...]], run_id: int, outcome: obs.Outcome, measured: int
) -> None:
    with obs.db_span("UPDATE", "finish run"):
        conn.execute(
            "UPDATE run SET outcome = %s, finished_at = now(), commits_measured = %s WHERE id = %s",
            (str(outcome), measured, run_id),
        )
