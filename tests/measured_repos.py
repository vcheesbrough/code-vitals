"""The plugin settings of the repos code-vitals measured before self-registration.

Until card #564 these lived in src/code_vitals/repos.py; each repo now carries
them in its own `.woodpecker/code-vitals.yml` (v-note #569, bored #567,
sovereign-config #568). They are kept here, exactly as those steps must write
them, because the tests of the area rules and of expected_values.toml are
written against them: the same rules at the same ruleset version, so moving
them into the repos re-measures nothing.
"""

import yaml

from code_vitals import settings
from code_vitals.settings import Settings

SETTINGS_YAML = {
    # version.txt is older than [workspace.package] in v-note and has gone
    # stale (0.54 against Cargo's 0.69 on 2026-10-21), so Cargo comes first.
    "v-note": """
trunk: master
version_sources:
  - {path: Cargo.toml, kind: cargo-workspace}
  - {path: Cargo.toml, kind: cargo-package}
  - {path: version.txt, kind: plain}
areas:
  version: 1
  rules:
    - {glob: "crates/**", area: server}  # the axum server and its protocol crate
    - {glob: "frontend/**", area: web}
    - {glob: "android/**", area: android}
""",
    "bored": """
trunk: main
version_sources:
  - {path: Cargo.toml, kind: cargo-workspace}
  - {path: Cargo.toml, kind: cargo-package}
areas:
  version: 1
  rules:
    - {glob: "backend/**", area: backend}
    - {glob: "frontend/**", area: web}
    - {glob: "shared/**", area: shared}
    - {glob: "mcp/**", area: shared}
""",
    "sovereign-config": """
trunk: main
version_sources:
  - {path: Cargo.toml, kind: cargo-workspace}
  - {path: Cargo.toml, kind: cargo-package}
areas:
  version: 1
  rules:
    - {glob: "crates/sovereign-config-server/**", area: server}
    - {glob: "crates/sovereign-config-woodpecker-broker/**", area: server}
    - {glob: "crates/sovereign-config-web/**", area: web}
    - {glob: "crates/**", area: libraries}
    - {glob: "proto/**", area: libraries}
""",
}


def workflow(repo: str) -> str:
    """The repo's `.woodpecker/code-vitals.yml`, as README § Measuring a repo
    shows it."""
    body = "\n".join("      " + line for line in SETTINGS_YAML[repo].strip().splitlines())
    trunk = MEASURED[repo].trunk
    return f"""when:
  - event: [push, manual]
    branch: {trunk}
skip_clone: true
steps:
  code-vitals:
    image: registry.desync.link/code-vitals-plugin:stable
    failure: ignore
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    settings:
{body}
      writer_password:
        from_secret: code_vitals_prod_writer_password
"""


def _load(repo: str) -> Settings:
    return settings.parse(yaml.safe_load(SETTINGS_YAML[repo]))


MEASURED: dict[str, Settings] = {name: _load(name) for name in SETTINGS_YAML}
