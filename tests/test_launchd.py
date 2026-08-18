import subprocess
from pathlib import Path

from crawler_gateway import launchd
from crawler_gateway.launchd import SERVICE_LABEL, launch_agent_payload
from crawler_gateway.paths import ProjectPaths


def test_launch_agent_runs_persistent_maintenance_without_secrets(tmp_path: Path) -> None:
    paths = ProjectPaths(tmp_path)
    config = tmp_path / "private" / "gateway.yaml"

    payload = launch_agent_payload(paths, config, "/usr/bin/python3")
    serialized = str(payload)

    assert payload["Label"] == SERVICE_LABEL
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["WorkingDirectory"] == "/"
    assert payload["EnvironmentVariables"]["CRAWLER_GATEWAY_BACKGROUND_SERVICE"] == "1"
    assert payload["EnvironmentVariables"]["PYTHONPATH"] == str(tmp_path)
    assert payload["ProgramArguments"][-1] == "maintain-reserve"
    assert str(config) in payload["ProgramArguments"]
    assert "subscription" not in serialized.casefold()
    assert "uuid" not in serialized.casefold()


def test_bootstrap_retries_transient_launchctl_error(
    tmp_path: Path, monkeypatch
) -> None:
    results = [
        subprocess.CompletedProcess([], 5, "", "Bootstrap failed: 5: Input/output error"),
        subprocess.CompletedProcess([], 5, "", "Bootstrap failed: 5: Input/output error"),
        subprocess.CompletedProcess([], 0, "", ""),
    ]
    calls: list[tuple[str, ...]] = []
    sleeps: list[float] = []

    def fake_launchctl(*arguments: str) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return results.pop(0)

    monkeypatch.setattr(launchd, "_launchctl", fake_launchctl)
    monkeypatch.setattr(launchd.time, "sleep", sleeps.append)

    result = launchd._bootstrap_agent(tmp_path / "agent.plist")

    assert result.returncode == 0
    assert len(calls) == 3
    assert sleeps == [2.0, 2.0]


def test_bootstrap_does_not_retry_permanent_error(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_launchctl(*arguments: str) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return subprocess.CompletedProcess([], 1, "", "Operation not permitted")

    monkeypatch.setattr(launchd, "_launchctl", fake_launchctl)

    result = launchd._bootstrap_agent(tmp_path / "agent.plist")

    assert result.returncode == 1
    assert len(calls) == 1
