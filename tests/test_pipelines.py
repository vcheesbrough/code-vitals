"""Pins the pipeline gates that nothing else would catch until a bad deploy.

Ported in spirit from sovereign-config's tests/pipeline_prod_gating.rs: prod is
deployable from `main` only, enforced in the workflow's own `when:` so a
deployment from another branch never starts; dev from any branch; a push never
deploys; and every image is pinned by digest.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

WOODPECKER = Path(__file__).resolve().parent.parent / ".woodpecker"


def _workflow(name: str) -> dict[str, Any]:
    data = yaml.safe_load((WOODPECKER / name).read_text())
    assert isinstance(data, dict)
    return data


def _when(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    when = workflow["when"]
    return when if isinstance(when, list) else [when]


def test_prod_deploys_only_from_main_and_only_to_prod() -> None:
    clauses = _when(_workflow("deploy-prod.yml"))
    # Every clause (Woodpecker ORs them) must carry both gates, or one clause
    # without them would open prod to any branch.
    assert clauses
    for clause in clauses:
        assert clause["event"] == "deployment"
        assert clause["branch"] == "main"
        assert clause["evaluate"] == 'CI_PIPELINE_DEPLOY_TARGET == "prod"'


def test_no_prod_step_widens_the_workflow_gate() -> None:
    for name, step in _workflow("deploy-prod.yml")["steps"].items():
        assert "when" not in step, f"step {name} has its own when:"


def test_dev_deploys_from_any_branch_and_only_to_dev() -> None:
    clauses = _when(_workflow("deploy-dev.yml"))
    assert clauses
    for clause in clauses:
        assert clause["event"] == "deployment"
        assert "branch" not in clause
        assert clause["evaluate"] == 'CI_PIPELINE_DEPLOY_TARGET == "dev"'


def test_dev_deploys_dev_and_prod_deploys_prod() -> None:
    def deploy_commands(name: str) -> str:
        return "\n".join(_workflow(name)["steps"]["deploy"]["commands"])

    assert "deploy-stack.sh dev " in deploy_commands("deploy-dev.yml")
    assert "deploy-stack.sh prod " in deploy_commands("deploy-prod.yml")
    for env in ("dev", "prod"):
        environment = _workflow(f"deploy-{env}.yml")["steps"]["deploy"]["environment"]
        secrets = {
            value["from_secret"] for value in environment.values() if isinstance(value, dict)
        }
        other = "prod" if env == "dev" else "dev"
        assert not any(s.startswith(f"code_vitals_{other}_") for s in secrets)


def test_a_push_never_deploys() -> None:
    for clause in _when(_workflow("build.yml")):
        events = clause["event"]
        events = events if isinstance(events, list) else [events]
        assert "deployment" not in events


@pytest.mark.parametrize("name", sorted(p.name for p in WOODPECKER.glob("*.yml")))
def test_every_step_image_is_pinned_by_digest(name: str) -> None:
    for step_name, step in _workflow(name)["steps"].items():
        assert "@sha256:" in step["image"], f"{name}: step {step_name} image is not digest-pinned"
