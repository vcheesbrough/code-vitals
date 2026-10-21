"""What a deployment checks after its collect steps; any problem fails it.

- check_repo, per repo with the clone just collected: a second catch-up finds
  nothing to do, and the values recorded at a few fixed commits are exactly
  those in expected_values.toml. Commits never change, so neither may their
  values: a change that alters them bumps the measure's version, and the same
  PR updates the table, so every change in numbers is reviewed.
  Non-recomputable measures are not in the table (fixed_value_measures).
- check_status, once every repo is collected: every version this build
  registers is `complete` (so the views show it), and every repo's last run
  succeeded.
"""

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import psycopg

from code_vitals import obs
from code_vitals.build_info import BuildInfo
from code_vitals.collect import collect
from code_vitals.measures import Measure, registered
from code_vitals.repos import RepoConfig

# Floats from SQL and from the table: equal to well within any display.
TOLERANCE = 1e-9
# A value's key in the table: `<area>/<facet>`, `-` for no area, and no
# `/<facet>` when the facet is ''.
NO_AREA = "-"

RowKey = tuple[str | None, str]


@dataclass(frozen=True)
class ExpectedMeasure:
    version: int
    values: dict[RowKey, float]


@dataclass(frozen=True)
class ExpectedRepo:
    ruleset: int
    commits: dict[str, dict[str, ExpectedMeasure]]


def row_key(area: str | None, facet: str) -> str:
    head = NO_AREA if area is None else area
    return f"{head}/{facet}" if facet else head


def parse_row_key(key: str) -> RowKey:
    head, _, facet = key.partition("/")
    return (None if head == NO_AREA else head), facet


def load_expected(text: str | None = None) -> dict[str, ExpectedRepo]:
    if text is None:
        text = (resources.files("code_vitals") / "expected_values.toml").read_text("utf-8")
    data = tomllib.loads(text)
    table = {}
    for repo, entry in data.items():
        commits = {}
        for sha, measures in entry.get("commits", {}).items():
            commits[sha] = {
                name: ExpectedMeasure(
                    int(spec["version"]),
                    {parse_row_key(k): float(v) for k, v in spec.items() if k != "version"},
                )
                for name, spec in measures.items()
            }
        table[repo] = ExpectedRepo(int(entry["ruleset"]), commits)
    return table


def check_repo(
    conninfo: str,
    repo: RepoConfig,
    clone_path: Path,
    build: BuildInfo,
    *,
    measures: Sequence[Measure] | None = None,
    expected: Mapping[str, ExpectedRepo] | None = None,
    pipeline_number: str | None = None,
) -> list[str]:
    """Problems found for one repo; empty when it passes."""
    measures = registered() if measures is None else list(measures)
    expected = load_expected() if expected is None else expected
    problems = []
    again = collect(
        conninfo,
        repo,
        clone_path,
        obs.Trigger.BACKFILL,
        build,
        measures=measures,
        pipeline_number=pipeline_number,
    )
    if again.backlog or again.outcome is not obs.Outcome.SUCCESS:
        problems.append(
            f"a second catch-up of {repo.name} had {again.backlog} commit(s) to do"
            f" ({again.outcome}, run {again.run_id})"
        )
    problems += _check_values(conninfo, repo, measures, expected.get(repo.name))
    return problems


def fixed_value_measures(measures: Sequence[Measure]) -> list[Measure]:
    """The measures the table pins. A non-recomputable one is recorded only
    from its release on, which each database dates differently, so no fixed
    commit has a value every database agrees on; check_status and the second
    catch-up still cover it."""
    return [m for m in measures if m.recomputable]


def _check_values(
    conninfo: str,
    repo: RepoConfig,
    measures: Sequence[Measure],
    expected: ExpectedRepo | None,
) -> list[str]:
    if expected is None or not expected.commits:
        return [f"expected_values.toml has no fixed commits for {repo.name}"]
    problems = []
    if expected.ruleset != repo.areas.version:
        problems.append(
            f"expected_values.toml was written for {repo.name}'s area ruleset"
            f" v{expected.ruleset}; this build has v{repo.areas.version}: update the table"
        )
    fixed = fixed_value_measures(measures)
    with psycopg.connect(conninfo) as conn:
        for sha, by_name in expected.commits.items():
            actual: dict[str, dict[RowKey, float]] = {}
            wrong = []
            for m in fixed:
                rows = _recorded(conn, repo, sha, m)
                if rows is None:
                    problems.append(f"{repo.name} {sha}: {m.name} v{m.version} is not recorded")
                    continue
                actual[m.name] = rows
                want = by_name.get(m.name)
                if want is None:
                    wrong.append(f"{m.name} v{m.version} is not in the table")
                elif want.version != m.version:
                    wrong.append(
                        f"{m.name}: the table has v{want.version}, this build v{m.version}"
                    )
                elif not _same(want.values, rows):
                    wrong.append(f"{m.name} v{m.version}: {_difference(want.values, rows)}")
            unknown = sorted(set(by_name) - {m.name for m in fixed})
            wrong += [f"{name} is not a recomputable registered measure" for name in unknown]
            if wrong:
                problems.append(
                    f"{repo.name} {sha} differs from expected_values.toml:\n  "
                    + "\n  ".join(wrong)
                    + "\nThe recorded values, to review and paste if the change is intended:\n"
                    + render(repo.name, sha, measures, actual)
                )
    return problems


def _recorded(
    conn: psycopg.Connection[tuple[object, ...]], repo: RepoConfig, sha: str, m: Measure
) -> dict[RowKey, float] | None:
    """The values of a measure version at a commit, under the repo's ruleset
    (None when the commit is not marked done for it)."""
    marked = conn.execute(
        "SELECT 1 FROM commit_measure cm JOIN commit c ON c.id = cm.commit_id"
        " JOIN repo r ON r.id = c.repo_id JOIN measure m ON m.id = cm.measure_id"
        " JOIN area_ruleset rs ON rs.id = cm.ruleset_id"
        " WHERE r.name = %s AND c.sha = %s AND m.name = %s AND m.version = %s"
        " AND rs.version = %s",
        (repo.name, sha, m.name, m.version, repo.areas.version),
    ).fetchone()
    if marked is None:
        return None
    rows = conn.execute(
        "SELECT a.name, x.facet, x.value FROM measurement x"
        " JOIN commit c ON c.id = x.commit_id JOIN repo r ON r.id = c.repo_id"
        " JOIN measure m ON m.id = x.measure_id"
        " LEFT JOIN area a ON a.id = x.area_id"
        " LEFT JOIN area_ruleset rs ON rs.id = a.ruleset_id"
        " WHERE r.name = %s AND c.sha = %s AND m.name = %s AND m.version = %s"
        " AND (x.area_id IS NULL OR rs.version = %s)",
        (repo.name, sha, m.name, m.version, repo.areas.version),
    ).fetchall()
    return {
        (None if area is None else str(area), str(facet)): float(str(value))
        for area, facet, value in rows
    }


def _same(want: Mapping[RowKey, float], got: Mapping[RowKey, float]) -> bool:
    return want.keys() == got.keys() and all(abs(want[k] - got[k]) <= TOLERANCE for k in want)


def _difference(want: Mapping[RowKey, float], got: Mapping[RowKey, float]) -> str:
    parts = []
    for key in sorted(want.keys() | got.keys(), key=lambda k: row_key(*k)):
        w, g = want.get(key), got.get(key)
        if w is None or g is None or abs(w - g) > TOLERANCE:
            parts.append(f"{row_key(*key)} expected {_number(w)} got {_number(g)}")
    return "; ".join(parts)


def _number(value: float | None) -> str:
    if value is None:
        return "nothing"
    return str(int(value)) if value.is_integer() else repr(value)


def render(
    repo: str, sha: str, measures: Sequence[Measure], values: Mapping[str, Mapping[RowKey, float]]
) -> str:
    """One commit's entries in expected_values.toml's format."""
    lines = []
    for m in measures:
        if m.name not in values:
            continue
        lines += ["", f"[{repo}.commits.{sha}.{m.name}]", f"version = {m.version}"]
        for key in sorted(values[m.name], key=lambda k: row_key(*k)):
            lines.append(f'"{row_key(*key)}" = {_number(values[m.name][key])}')
    return "\n".join(lines).lstrip("\n")


def check_status(
    conninfo: str,
    repos: Sequence[RepoConfig],
    *,
    measures: Sequence[Measure] | None = None,
) -> list[str]:
    """Problems with what every repo's collect left behind; empty when none."""
    measures = registered() if measures is None else list(measures)
    problems = []
    with psycopg.connect(conninfo) as conn:
        status = {
            (str(name), int(str(version))): str(state)
            for name, version, state in conn.execute(
                "SELECT name, version, status::text FROM measure"
            )
        }
        for m in measures:
            state = status.get((m.name, m.version), "not registered")
            if state != "complete":
                problems.append(f"measure {m.name} v{m.version} is {state}, not complete")
        for repo in repos:
            ruleset = conn.execute(
                "SELECT rs.status::text FROM area_ruleset rs JOIN repo r ON r.id = rs.repo_id"
                " WHERE r.name = %s AND rs.version = %s",
                (repo.name, repo.areas.version),
            ).fetchone()
            state = "not registered" if ruleset is None else str(ruleset[0])
            if state != "complete":
                problems.append(
                    f"{repo.name}'s area ruleset v{repo.areas.version} is {state}, not complete"
                )
            last = conn.execute(
                "SELECT run.id, run.outcome::text FROM run JOIN repo r ON r.id = run.repo_id"
                " WHERE r.name = %s ORDER BY run.started_at DESC, run.id DESC LIMIT 1",
                (repo.name,),
            ).fetchone()
            if last is None:
                problems.append(f"{repo.name} has no collector run")
            elif last[1] != "success":
                problems.append(f"{repo.name}'s last run ({last[0]}) is {last[1]}")
    return problems
