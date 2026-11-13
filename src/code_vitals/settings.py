"""A measured repo's settings: the `settings:` of its code-vitals plugin step.

A repo is measured by adding one Woodpecker plugin step to its
`.woodpecker/code-vitals.yml` (README § Measuring a repo). Its settings reach
code-vitals three ways, all validated here, so a setting means the same
wherever it is read:

- the plugin, from Woodpecker's PLUGIN_* variables (from_plugin_env);
- the collector the plugin starts, as canonical JSON in CODE_VITALS_SETTINGS
  (from_json, written by Settings.as_json);
- a deployment's backfill, from the workflow file in the repo's clone at its
  trunk head (from_workflow), which finds no step in a repo that has none.

Invalid settings are refused with a message that names the setting; unknown
settings are refused too, so a misspelt one fails instead of being ignored.
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import yaml

from code_vitals.areas import GENERIC, AreaRuleset, ruleset
from code_vitals.repos import RepoConfig, VersionFile, VersionSource

WORKFLOW_PATH = ".woodpecker/code-vitals.yml"
# Woodpecker names a workflow after its file, and reads either extension.
WORKFLOW_NAME = "code-vitals"
WORKFLOW_PATHS = (WORKFLOW_PATH, ".woodpecker/code-vitals.yaml")
PLUGIN_IMAGE = "registry.desync.link/code-vitals-plugin"
SETTINGS_ENV = "CODE_VITALS_SETTINGS"
PLUGIN_ENV_PREFIX = "PLUGIN_"
# Read by the plugin to reach the database; never part of the settings proper.
WRITER_PASSWORD = "writer_password"

_KEYS = ("trunk", "version_sources", "areas", WRITER_PASSWORD)
# Settings Woodpecker passes JSON-encoded (it encodes every map and every list
# of maps that way).
_JSON_KEYS = ("version_sources", "areas")
_BRANCH = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]*$")
# Lower-case words: an area is also a row key in expected_values.toml, where
# `/` separates the facet and `-` means "no area".
_AREA = re.compile(r"^[a-z][a-z0-9_]*(-[a-z0-9_]+)*$")


class SettingsError(ValueError):
    """Settings that cannot be used, with a message naming the setting."""


@dataclass(frozen=True)
class Settings:
    trunk: str
    # Tried in order at each commit; the first that yields a version wins.
    version_sources: tuple[VersionSource, ...]
    areas: AreaRuleset
    # The repo's own area rules as written (the ruleset adds the shared
    # excludes, test rules and catch-all around them).
    area_rules: tuple[tuple[str, str], ...] = ()

    def repo(self, name: str, url: str | None = None) -> RepoConfig:
        return RepoConfig(name, self.trunk, self.version_sources, self.areas, url)

    def as_json(self) -> str:
        """The canonical form, which from_json reads back."""
        return json.dumps(
            {
                "trunk": self.trunk,
                "version_sources": [
                    {"path": s.path, "kind": str(s.kind)} for s in self.version_sources
                ],
                "areas": {
                    "version": self.areas.version,
                    "rules": [{"glob": g, "area": a} for g, a in self.area_rules],
                },
            },
            separators=(",", ":"),
        )


def parse(settings: Mapping[str, Any]) -> Settings:
    """Validate a settings mapping (`writer_password`, if present, is ignored)."""
    if not isinstance(settings, Mapping):
        raise SettingsError(f"settings must be a map, not {_kind(settings)}")
    unknown = sorted(str(key) for key in settings if key not in _KEYS)
    if unknown:
        raise SettingsError(f"unknown setting(s) {', '.join(unknown)}; known: {', '.join(_KEYS)}")
    trunk = settings.get("trunk")
    if not isinstance(trunk, str) or not _BRANCH.match(trunk) or ".." in trunk:
        raise SettingsError(f"trunk must be a branch name, not {trunk!r}")
    sources = _version_sources(settings.get("version_sources", []))
    version, rules = _areas(settings.get("areas"))
    areas = GENERIC if version is None else ruleset(version, *rules)
    return Settings(trunk, sources, areas, rules)


def _version_sources(value: Any) -> tuple[VersionSource, ...]:
    if not isinstance(value, list):
        raise SettingsError(f"version_sources must be a list, not {_kind(value)}")
    kinds = ", ".join(str(k) for k in VersionFile)
    sources = []
    for i, entry in enumerate(value):
        where = f"version_sources[{i}]"
        _only(entry, where, ("path", "kind"), ("path", "kind"))
        path, kind = entry.get("path"), entry.get("kind")
        _relative_path(path, f"{where}.path")
        if kind not in {str(k) for k in VersionFile}:
            raise SettingsError(f"{where}.kind must be one of {kinds}, not {kind!r}")
        sources.append(VersionSource(path, VersionFile(kind)))
    return tuple(sources)


def _areas(value: Any) -> tuple[int | None, tuple[tuple[str, str], ...]]:
    if value is None:
        return None, ()
    _only(value, "areas", ("version", "rules"), ("version",))
    version = value.get("version")
    if type(version) is not int or version < 1:
        raise SettingsError(f"areas.version must be a whole number from 1, not {version!r}")
    rules_value = value.get("rules", [])
    if not isinstance(rules_value, list):
        raise SettingsError(f"areas.rules must be a list, not {_kind(rules_value)}")
    rules = []
    for i, entry in enumerate(rules_value):
        where = f"areas.rules[{i}]"
        _only(entry, where, ("glob", "area"), ("glob", "area"))
        glob, area = entry.get("glob"), entry.get("area")
        _relative_path(glob, f"{where}.glob")
        try:
            PurePosixPath("probe").full_match(glob)
        except ValueError as error:
            raise SettingsError(f"{where}.glob {glob!r} is not a glob: {error}") from None
        if not isinstance(area, str) or not _AREA.match(area):
            raise SettingsError(
                f"{where}.area must be a lower-case name such as `server`, not {area!r}"
            )
        rules.append((glob, area))
    return version, tuple(rules)


def _only(entry: Any, where: str, keys: Sequence[str], required: Sequence[str]) -> None:
    if not isinstance(entry, Mapping):
        raise SettingsError(f"{where} must be a map, not {_kind(entry)}")
    unknown = sorted(str(key) for key in entry if key not in keys)
    if unknown:
        raise SettingsError(f"{where} has unknown key(s) {', '.join(unknown)}")
    missing = [key for key in required if key not in entry]
    if missing:
        raise SettingsError(f"{where} needs {', '.join(missing)}")


def _relative_path(value: Any, where: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or ".." in PurePosixPath(value).parts
    ):
        raise SettingsError(f"{where} must be a path relative to the repo root, not {value!r}")


def _kind(value: Any) -> str:
    return "nothing" if value is None else type(value).__name__


def from_plugin_env(environ: Mapping[str, str]) -> Settings:
    """The settings Woodpecker hands a plugin step: each `settings:` key as
    PLUGIN_<KEY>, maps and lists of maps JSON-encoded."""
    raw: dict[str, Any] = {}
    for name, text in environ.items():
        if not name.startswith(PLUGIN_ENV_PREFIX):
            continue
        key = name[len(PLUGIN_ENV_PREFIX) :].lower()
        if key in _JSON_KEYS:
            try:
                raw[key] = json.loads(text)
            except json.JSONDecodeError:
                raise SettingsError(f"{key} is not valid (expected a list or map)") from None
        else:
            raw[key] = text
    return parse(raw)


def from_json(text: str) -> Settings:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise SettingsError(f"{SETTINGS_ENV} is not JSON: {error}") from None
    return parse(value)


def is_plugin_image(image: Any) -> bool:
    if not isinstance(image, str):
        return False
    return image == PLUGIN_IMAGE or image.startswith((PLUGIN_IMAGE + ":", PLUGIN_IMAGE + "@"))


def from_workflow(text: str) -> Settings | None:
    """The settings of the code-vitals plugin step in a workflow file's text,
    or None when it has no such step."""
    try:
        workflow = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise SettingsError(f"{WORKFLOW_PATH} is not valid YAML: {error}") from None
    steps = workflow.get("steps") if isinstance(workflow, dict) else None
    if isinstance(steps, dict):
        steps = list(steps.values())
    if not isinstance(steps, list):
        return None
    plugin = [s for s in steps if isinstance(s, dict) and is_plugin_image(s.get("image"))]
    if not plugin:
        return None
    if len(plugin) > 1:
        raise SettingsError(f"{WORKFLOW_PATH} has {len(plugin)} code-vitals plugin steps; one only")
    return parse(plugin[0].get("settings") or {})
