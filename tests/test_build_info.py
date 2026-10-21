import json
from importlib import metadata
from pathlib import Path

import pytest

from code_vitals.build_info import UNKNOWN_REVISION, BuildInfo, load_build_info


def test_reads_version_and_revision_from_the_build_info_file(tmp_path: Path) -> None:
    path = tmp_path / "build-info.json"
    path.write_text(json.dumps({"version": "0.1.339", "revision": "0123abc"}))

    assert load_build_info(path) == BuildInfo(version="0.1.339", revision="0123abc")


def test_falls_back_to_package_metadata_outside_the_image(tmp_path: Path) -> None:
    info = load_build_info(tmp_path / "absent.json")

    assert info == BuildInfo(version=metadata.version("code-vitals"), revision=UNKNOWN_REVISION)


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"version": "0.1.339"}),
        json.dumps({"version": "", "revision": "0123abc"}),
        json.dumps({"version": "0.1.339", "revision": 7}),
    ],
)
def test_a_malformed_build_info_file_fails_loudly(tmp_path: Path, content: str) -> None:
    path = tmp_path / "build-info.json"
    path.write_text(content)

    with pytest.raises((ValueError, KeyError)):
        load_build_info(path)
