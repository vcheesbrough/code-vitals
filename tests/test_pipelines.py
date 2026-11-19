"""Pins the pipeline gates that nothing else would catch until a bad deploy.

Ported in spirit from sovereign-config's tests/pipeline_prod_gating.rs: prod is
deployable from `main` only, enforced in the workflow's own `when:` so a
deployment from another branch never starts; dev from any branch; a push never
deploys; and every image is pinned by digest. Also that every deployment
collects and checks every registered repo with the deployed image before it is
tagged, that the plugin image is built, checked and published with the
collector, and that only prod moves :stable (both images'), last. And that
the workflow measuring this repo itself (card #562) can never gate a build.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

from code_vitals import settings
from code_vitals.measures.functionality import OPERATIONS, SUITES

from .measured_repos import MEASURED

WOODPECKER = Path(__file__).resolve().parent.parent / ".woodpecker"
# The workflow that measures this repo itself (card #562).
SELF_MEASURE = "code-vitals.yml"


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
def test_every_registered_repo_is_collected_and_checked_with_the_deployed_image(env: str) -> None:
    # Card #564: one step over the repos the database lists, not one per repo
    # from a list in this repo; check-status then covers the repos it collected.
    steps = _workflow(f"deploy-{env}.yml")["steps"]
    assert sorted(name for name in steps if name.startswith("collect")) == ["collect"]
    assert steps["collect"]["depends_on"] == ["deploy"]
    assert (
        f'./scripts/run-collector.sh {env} "$$(cat .deploy-image)" repos'
        in steps["collect"]["commands"]
    )
    assert steps["check"]["depends_on"] == ["collect"]
    assert (
        f'./scripts/run-collector.sh {env} "$$(cat .deploy-image)" status'
        in (steps["check"]["commands"])
    )


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_every_deployment_verifies_the_builds_plugin_image(env: str) -> None:
    script = "\n".join(_workflow(f"deploy-{env}.yml")["steps"]["verify-image"]["commands"])
    assert "PLUGIN=registry.desync.link/code-vitals-plugin:$$RELEASE_TAG" in script
    assert (
        './scripts/check-image-metadata.sh "$$PLUGIN" "$$RELEASE_TAG" "$$CI_COMMIT_SHA"' in script
    )
    assert "> .deploy-plugin-image" in script


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
                # The tag the measured repos' plugin steps run, by the digest
                # verify-image recorded.
                assert "code-vitals-plugin:stable" in text
                assert "$$(cat .deploy-plugin-image)" in text
            else:
                assert ":stable" not in text, f"{env}: {name} touches :stable"


@pytest.mark.parametrize("image", ["code-vitals-superset", "code-vitals-plugin"])
def test_every_other_image_is_published_before_the_collector(image: str) -> None:
    # A deployment resolves its build through the collector's :commit-<sha>,
    # so that tag must only exist once the build's other images are published.
    commands = _workflow("build.yml")["steps"]["publish-image"]["commands"]
    other = next(
        i for i, c in enumerate(commands) if f"IMAGE_REPO=registry.desync.link/{image} " in c
    )
    collector = next(
        i
        for i, c in enumerate(commands)
        if "publish-release-image.sh" in c and "IMAGE_REPO" not in c
    )
    assert other < collector


def test_the_plugin_image_is_built_on_the_collector_and_checked_before_publishing() -> None:
    steps = _workflow("build.yml")["steps"]
    script = "\n".join(steps["build"]["commands"])
    assert "-f plugin/Dockerfile" in script
    assert '--build-arg COLLECTOR_IMAGE="$$IMAGE"' in script
    assert (
        './scripts/check-image-metadata.sh "$$PLUGIN" "$$RELEASE_TAG" "$$CI_COMMIT_SHA"' in script
    )
    assert './scripts/check-plugin-image.sh "$$PLUGIN" "$$RELEASE_TAG"' in script
    # publish-image waits on build, so an unchecked plugin is never published.
    assert "build" in steps["publish-image"]["depends_on"]


def test_the_dashboard_import_is_tested_before_anything_is_published() -> None:
    # Card #548: the import test runs on the images the build just made, and
    # a failure stops publish-image, so no image with a broken import ships.
    steps = _workflow("build.yml")["steps"]
    check = steps["check-superset-import"]
    assert check["depends_on"] == ["build"]
    script = "\n".join(check["commands"])
    assert "./scripts/with-test-postgres.sh ./scripts/check-superset-import.sh" in script
    assert "code-vitals-superset:$$RELEASE_TAG" in script
    assert "code-vitals-collector:$$RELEASE_TAG" in script
    assert sorted(steps["publish-image"]["depends_on"]) == ["build", "check-superset-import"]


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
        if (name, step_name) == (SELF_MEASURE, "code-vitals"):
            # The one approved moving tag (AGENTS.md § Deviations register):
            # exactly the plugin's :stable, as in every other measured repo.
            assert step["image"] == f"{settings.PLUGIN_IMAGE}:stable"
            continue
        assert "@sha256:" in step["image"], f"{name}: step {step_name} image is not digest-pinned"


# --- code-vitals measures itself (card #562) ----------------------------------


def test_the_self_measuring_workflow_is_where_deployments_read_it() -> None:
    # The plugin refuses any other workflow, and a deployment's backfill reads
    # the settings from this path at the trunk head.
    assert f".woodpecker/{SELF_MEASURE}" == settings.WORKFLOW_PATH
    assert (WOODPECKER / SELF_MEASURE).is_file()


def test_this_repo_is_measured_on_trunk_pushes_only() -> None:
    workflow = _workflow(SELF_MEASURE)
    assert _when(workflow) == [{"event": ["push", "manual"], "branch": "main"}]
    # The collector clones the repo itself, with full history.
    assert workflow["skip_clone"] is True
    assert list(workflow["steps"]) == ["code-vitals"]
    # No step-level `when:` to widen the gate.
    assert "when" not in workflow["steps"]["code-vitals"]


def test_measuring_this_repo_never_gates_its_build() -> None:
    measure, build = _workflow(SELF_MEASURE), _workflow("build.yml")
    # Neither workflow waits on the other: the build publishes whether or not
    # the measurement ran, and the measurement runs whether or not it built.
    assert "depends_on" not in measure
    assert "depends_on" not in build
    step = measure["steps"]["code-vitals"]
    assert "depends_on" not in step
    # A failed measurement is caught up by the next run; it never fails a push.
    assert step["failure"] == "ignore"
    # Every `main` push builds, and nothing in the build names the measurement.
    assert any("branch" not in clause for clause in _when(build))
    assert "code-vitals-plugin:stable" not in yaml.safe_dump(build)


def test_the_self_measuring_step_is_the_documented_plugin_step() -> None:
    step = _workflow(SELF_MEASURE)["steps"]["code-vitals"]
    assert set(step) == {"image", "pull", "failure", "volumes", "settings"}
    # :stable moves with every prod deployment, so it is pulled every run.
    assert step["pull"] is True
    assert step["volumes"] == ["/var/run/docker.sock:/var/run/docker.sock"]
    # The prod writer, from this repo's own broker layer; no other secret.
    assert step["settings"]["writer_password"] == {
        "from_secret": "code_vitals_prod_writer_password"
    }
    assert "from_secret" not in yaml.safe_dump(
        {k: v for k, v in step["settings"].items() if k != "writer_password"}
    )


def test_the_self_measuring_settings_pass_the_shared_validator() -> None:
    found = settings.from_workflow((WOODPECKER / SELF_MEASURE).read_text())
    assert found is not None
    assert found.trunk == "main"
    assert [(s.path, str(s.kind)) for s in found.version_sources] == [
        ("pyproject.toml", "pyproject")
    ]
    # The copy the area and expected-value tests are written against is this
    # file's settings exactly, so neither can drift from what is recorded.
    assert found == MEASURED["code-vitals"]
    # As the plugin hands them to the collector.
    assert settings.from_json(found.as_json()) == found


# Ruleset v1 as registered. Changing the rules means a new version (and a
# repin in expected_values.toml), never an edit to this tuple.
CODE_VITALS_RULESET_V1 = (
    ("scripts/test-*.sh", "tests"),
    ("superset/*_test.py", "tests"),
    ("src/code_vitals/expected_values.toml", "tests"),
    ("src/**", "collector"),
    ("superset/**", "dashboard"),
)


def test_a_rule_change_bumps_this_repos_ruleset() -> None:
    # The collector refuses a known ruleset version whose rules changed, but
    # only after merge, behind `failure: ignore`; this fails the build instead.
    found = MEASURED["code-vitals"]
    if found.areas.version == 1:
        assert found.area_rules == CODE_VITALS_RULESET_V1


def test_this_repo_has_no_operations_or_suite_rules() -> None:
    # Decided on card #562: it serves no routes or tools and has no e2e suite
    # in those measures' sense. Adding rules is a measure version bump.
    assert "code-vitals" not in OPERATIONS
    assert "code-vitals" not in SUITES
