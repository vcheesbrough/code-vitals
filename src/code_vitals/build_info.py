"""The identity of the running build: which version and commit produced it.

The image build writes both into BUILD_INFO_PATH, a file rather than an
environment variable, so a deployment cannot override what the binary is; every
run can then record exactly what measured it. Outside the image (tests, a local
checkout) the file is absent and the package metadata stands in.
"""

import json
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

BUILD_INFO_PATH = Path("/etc/code-vitals/build-info.json")
UNKNOWN_REVISION = "unknown"


@dataclass(frozen=True)
class BuildInfo:
    version: str
    revision: str


def load_build_info(path: Path | None = None) -> BuildInfo:
    path = BUILD_INFO_PATH if path is None else path
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return BuildInfo(version=metadata.version("code-vitals"), revision=UNKNOWN_REVISION)
    # A present but malformed file is a broken image: fail loudly rather than
    # record measurements under a made-up identity.
    data = json.loads(raw)
    version, revision = data["version"], data["revision"]
    if not isinstance(version, str) or not version or not isinstance(revision, str) or not revision:
        raise ValueError(f"{path}: version and revision must be non-empty strings")
    return BuildInfo(version=version, revision=revision)
