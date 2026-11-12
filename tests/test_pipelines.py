"""Pins the pipeline gates that nothing else would catch until a bad deploy.

Ported in spirit from sovereign-config's tests/pipeline_prod_gating.rs: prod is
deployable from `main` only, enforced in the workflow's own `when:` so a
deployment from another branch never starts; dev from any branch; a push never
deploys; and every image is pinned by digest. Also that every deployment
collects and checks every repo with the deployed image before it is tagged,
and that only prod moves :stable, last.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

from code_vitals.repos import REPOS

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
        other = "prod" if env == "dev" else "dev"
        for name, step in _workflow(f"deploy-{env}.yml")["steps"].items():
            environment = step.get("environment") or {}
            secrets = {
                value["from_secret"] for value in environment.values() if isinstance(value, dict)
            }
            assert not any(s.startswith(f"code_vitals_{other}_") for s in secrets), name


def test_dev_applies_only_the_dev_blueprint_with_only_dev_secrets() -> None:
    settings = _workflow("deploy-dev.yml")["steps"]["apply-authentik-blueprint"]["settings"]
    assert settings["file"] == "authentik/blueprint-dev.yaml"
    assert list(settings["vars"]) == ["AUTHENTIK_CODE_VITALS_DEV_CLIENT_SECRET"]
    assert settings["vars"]["AUTHENTIK_CODE_VITALS_DEV_CLIENT_SECRET"]["from_secret"] == (
        "code_vitals_dev_oidc_client_secret"
    )


def test_prod_applies_the_full_blueprint() -> None:
    settings = _workflow("deploy-prod.yml")["steps"]["apply-authentik-blueprint"]["settings"]
    assert settings["file"] == "authentik/blueprint.yaml"
    assert set(settings["vars"]) == {
        "AUTHENTIK_CODE_VITALS_DEV_CLIENT_SECRET",
        "AUTHENTIK_CODE_VITALS_PROD_CLIENT_SECRET",
    }


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_a_failed_smoke_check_leaves_the_commit_untagged(env: str) -> None:
    steps = _workflow(f"deploy-{env}.yml")["steps"]
    assert "smoke-superset.sh " + env in "\n".join(steps["smoke"]["commands"])
    assert steps["smoke"]["depends_on"] == ["deploy"]
    assert steps["tag-release"]["depends_on"] == ["smoke", "check"]


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_every_repo_is_collected_and_checked_with_the_deployed_image(env: str) -> None:
    steps = _workflow(f"deploy-{env}.yml")["steps"]
    collects = sorted(name for name in steps if name.startswith("collect-"))
    assert collects == sorted(f"collect-{repo}" for repo in REPOS)
    for repo in REPOS:
        step = steps[f"collect-{repo}"]
        assert step["depends_on"] == ["deploy"]
        assert (
            f'./scripts/run-collector.sh {env} "$$(cat .deploy-image)" repo {repo}'
            in step["commands"]
        )
    assert sorted(steps["check"]["depends_on"]) == collects
    assert (
        f'./scripts/run-collector.sh {env} "$$(cat .deploy-image)" status'
        in (steps["check"]["commands"])
    )


def test_the_collect_script_knows_exactly_the_configured_repos() -> None:
    script = (Path(__file__).resolve().parent.parent / "scripts" / "run-collector.sh").read_text()
    assert "      v-note | bored | sovereign-config) ;;" in script
    assert sorted(REPOS) == sorted(["v-note", "bored", "sovereign-config"])


def test_only_prod_moves_stable_and_only_after_it_is_checked_and_tagged() -> None:
    prod = _workflow("deploy-prod.yml")["steps"]
    assert prod["promote-stable"]["depends_on"] == ["tag-release"]
    # Last: nothing waits on it, so nothing can run after :stable moved.
    assert not any("promote-stable" in s.get("depends_on", []) for s in prod.values())
    for env, steps in (("dev", _workflow("deploy-dev.yml")["steps"]), ("prod", prod)):
        for name, step in steps.items():
            text = yaml.safe_dump(step)
            if env == "prod" and name == "promote-stable":
                assert "code-vitals-collector:stable" in text
            else:
                assert ":stable" not in text, f"{env}: {name} touches :stable"


def test_the_superset_image_is_published_before_the_collector() -> None:
    # A deployment resolves its build through the collector's :commit-<sha>,
    # so that tag must only exist once the Superset image is published too.
    commands = _workflow("build.yml")["steps"]["publish-image"]["commands"]
    superset = next(i for i, c in enumerate(commands) if "code-vitals-superset" in c)
    collector = next(
        i
        for i, c in enumerate(commands)
        if "publish-release-image.sh" in c and "IMAGE_REPO" not in c
    )
    assert superset < collector


@pytest.mark.parametrize("name", ["blueprint.yaml", "blueprint-dev.yaml"])
def test_every_oauth2_provider_allows_the_authorization_code_grant(name: str) -> None:
    # Authentik 2026.x defaults grant_types to an empty list, which refuses
    # every authorize request; Superset needs the authorization code grant.
    class Loader(yaml.SafeLoader):
        pass

    def find(loader: yaml.SafeLoader, node: yaml.Node) -> list[Any]:
        assert isinstance(node, yaml.SequenceNode)
        return loader.construct_sequence(node)

    Loader.add_constructor("!Find", find)
    path = Path(__file__).resolve().parent.parent / "authentik" / name
    entries = yaml.load(path.read_text(), Loader=Loader)["entries"]
    providers = [e for e in entries if e["model"] == "authentik_providers_oauth2.oauth2provider"]
    assert providers
    for provider in providers:
        assert provider["attrs"]["grant_types"] == ["authorization_code"], provider["attrs"]["name"]


def test_a_push_never_deploys() -> None:
    for clause in _when(_workflow("build.yml")):
        events = clause["event"]
        events = events if isinstance(events, list) else [events]
        assert "deployment" not in events


@pytest.mark.parametrize("name", sorted(p.name for p in WOODPECKER.glob("*.yml")))
def test_every_step_image_is_pinned_by_digest(name: str) -> None:
    for step_name, step in _workflow(name)["steps"].items():
        assert "@sha256:" in step["image"], f"{name}: step {step_name} image is not digest-pinned"
