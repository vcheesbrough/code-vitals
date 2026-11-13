"""A deployment's backfill: re-measure every registered repo with the image
being deployed, from the repo's own plugin settings.

The `repo` table is the list of measured repos — a repo registers itself the
first time its plugin step runs (plugin.py). For each one a deployment clones
its URL, reads the settings of the code-vitals plugin step in
`.woodpecker/code-vitals.yml` at the trunk head, catches it up with
trigger=backfill and checks it (checks.check_repo). A repo whose workflow has
no plugin step (any more, or not yet) is skipped, not failed: it says so in
the step log and is left out of check-status.
"""

import contextlib
import logging
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import psycopg

from code_vitals import obs, settings
from code_vitals.build_info import BuildInfo
from code_vitals.checks import check_repo
from code_vitals.collect import RunResult, collect
from code_vitals.gitrepo import Clone, clone_from
from code_vitals.measures import Measure
from code_vitals.settings import Settings

log = logging.getLogger("code_vitals.backfill")


@dataclass(frozen=True)
class RegisteredRepo:
    name: str
    url: str | None
    trunk: str


def registered_repos(conninfo: str) -> list[RegisteredRepo]:
    with psycopg.connect(conninfo) as conn, obs.db_span("SELECT", "registered repos"):
        rows = conn.execute("SELECT name, url, trunk FROM repo ORDER BY name").fetchall()
    return [
        RegisteredRepo(str(name), None if url is None else str(url), str(trunk))
        for name, url, trunk in rows
    ]


def settings_at_trunk(clone: Clone, trunk: str) -> Settings | None:
    """The plugin settings in the clone's workflow file at the trunk head, or
    None when the file or its plugin step is absent. Raises SettingsError for
    a step whose settings are invalid."""
    text = clone.read_file(clone.resolve_trunk(trunk), settings.WORKFLOW_PATH)
    return None if text is None else settings.from_workflow(text)


class Outcome(StrEnum):
    CHECKED = "checked"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class BackfillResult:
    outcome: Outcome
    # Set when checked: the collect's result and what check_repo found.
    collected: RunResult | None = None
    problems: tuple[str, ...] = ()
    settings: Settings | None = None


def backfill_repo(
    conninfo: str,
    repo: RegisteredRepo,
    build: BuildInfo,
    *,
    measures: Sequence[Measure] | None = None,
    pipeline_number: str | None = None,
) -> BackfillResult:
    """Clone the repo, then skip it or catch it up and check it. Raises for a
    clone that fails or settings that are invalid."""
    if repo.url is None:
        raise ValueError(f"{repo.name} has no URL to clone")
    with contextlib.ExitStack() as stack:
        tmp = stack.enter_context(tempfile.TemporaryDirectory(prefix="code-vitals-clone-"))
        clone = clone_from(repo.url, Path(tmp) / "clone")
        found = settings_at_trunk(clone, repo.trunk)
        if found is None:
            log.info("%s has no code-vitals plugin step; skipped", repo.name)
            return BackfillResult(Outcome.SKIPPED)
        config = found.repo(repo.name, repo.url)
        collected = collect(
            conninfo,
            config,
            clone.path,
            obs.Trigger.BACKFILL,
            build,
            measures=measures,
            pipeline_number=pipeline_number,
        )
        problems = check_repo(
            conninfo,
            config,
            clone.path,
            build,
            measures=measures,
            pipeline_number=pipeline_number,
        )
        return BackfillResult(Outcome.CHECKED, collected, tuple(problems), found)
