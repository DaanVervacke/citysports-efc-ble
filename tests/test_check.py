import subprocess
from collections.abc import Sequence

import pytest
from scripts import check


def test_check_help_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        check.main(["--help"])

    assert exc_info.value.code == 0
    assert "Run the local CI gate" in capsys.readouterr().out


def test_check_rejects_unknown_arguments() -> None:
    with pytest.raises(SystemExit) as exc_info:
        check.main(["--bogus"])

    assert exc_info.value.code == 2


def test_check_stops_at_first_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Sequence[str]] = []

    def run(
        command: Sequence[str], *, check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        assert not check
        calls.append(command)
        return subprocess.CompletedProcess(command, 1 if len(calls) == 2 else 0)

    monkeypatch.setattr(subprocess, "run", run)

    assert check.main([]) == 1
    assert calls == [command for _, command in check.COMMANDS[:2]]


def test_check_builds_without_sources() -> None:
    assert dict(check.COMMANDS)["build"] == ("uv", "build", "--no-sources")
