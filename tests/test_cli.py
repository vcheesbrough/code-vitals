import json
from pathlib import Path

import pytest

from code_vitals import build_info, cli


def test_version_prints_version_and_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "build-info.json"
    path.write_text(json.dumps({"version": "0.1.339", "revision": "0123abc"}))
    monkeypatch.setattr(build_info, "BUILD_INFO_PATH", path)

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == "code-vitals 0.1.339 (0123abc)"


def test_no_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 0
    assert "usage: code-vitals" in capsys.readouterr().out
