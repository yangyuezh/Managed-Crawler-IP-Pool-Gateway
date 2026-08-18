import signal
from pathlib import Path

import pytest

from crawler_gateway.integrations import nsfc
from crawler_gateway.integrations.nsfc import (
    existing_nsfc_processes,
    nsfc_child_environment,
    nsfc_command,
    stop_nsfc_processes,
)


def test_default_nsfc_paths_find_common_standalone_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = tmp_path / "managed_crawler_ip_pool_gateway"
    repo = tmp_path / "github_grant_datasets" / "NSFC-Official-Projects-Database"
    script = repo / "crawler" / "nsfc_detail_autopilot.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    data = tmp_path / "nsfc_project_db" / "official_final"
    database = data / "final" / "nsfc_official_final_projects.sqlite"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"")
    monkeypatch.delenv("NSFC_REPO", raising=False)
    monkeypatch.delenv("NSFC_DATA_DIR", raising=False)
    monkeypatch.setattr(nsfc, "_GATEWAY_ROOT", gateway)

    assert nsfc._default_nsfc_repo() == repo
    assert nsfc._default_nsfc_data(repo) == data


def test_nsfc_child_environment_passes_target_user_agent_without_cli_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NSFC_USER_AGENT", "ambient-profile")

    environment = nsfc_child_environment({"User-Agent": "validated-target-profile"})

    assert environment["NSFC_USER_AGENT"] == "validated-target-profile"


def test_nsfc_command_has_one_worker_per_proxy_lane(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    script = repo / "crawler" / "nsfc_detail_autopilot.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    data = tmp_path / "data"
    database = data / "final" / "nsfc_official_final_projects.sqlite"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"")
    proxies = ["http://127.0.0.1:17891", "http://127.0.0.1:17892"]

    command = nsfc_command(
        python="python3",
        repo=repo,
        data_dir=data,
        proxy_urls=proxies,
        min_delay=0.15,
        max_delay=0.55,
        retries=8,
        timeout=30,
    )

    assert command[command.index("--workers") + 1] == "2"
    assert command.count("--proxy-list") == 2
    assert all(proxy in command for proxy in proxies)


def test_nsfc_command_requires_proxy_lanes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    script = repo / "crawler" / "nsfc_detail_autopilot.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    data = tmp_path / "data"
    database = data / "final" / "nsfc_official_final_projects.sqlite"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"")

    with pytest.raises(ValueError, match="no assigned"):
        nsfc_command(
            python="python3",
            repo=repo,
            data_dir=data,
            proxy_urls=[],
            min_delay=0.15,
            max_delay=0.55,
            retries=8,
            timeout=30,
        )


def test_existing_nsfc_processes_only_matches_python_and_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    output = "\n".join(
        [
            f"99101 /usr/bin/python3 /repo/crawler/nsfc_detail_autopilot.py --data-dir {data}",
            f"99102 SCREEN python3 /repo/crawler/nsfc_detail_autopilot.py --data-dir {data}",
            "99103 /usr/bin/python3 /repo/crawler/nsfc_detail_autopilot.py --data-dir /other",
        ]
    )

    def fake_run(*_args, **_kwargs):
        return type("Result", (), {"stdout": output})()

    monkeypatch.setattr("crawler_gateway.integrations.nsfc.subprocess.run", fake_run)
    assert existing_nsfc_processes(data) == [
        {
            "pid": 99101,
            "role": "autopilot",
            "command": f"/usr/bin/python3 /repo/crawler/nsfc_detail_autopilot.py --data-dir {data}",
        }
    ]


def test_stop_nsfc_processes_sends_term(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    calls = iter(
        [
            [{"pid": 99101, "role": "autopilot", "command": "python"}],
            [],
        ]
    )
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "crawler_gateway.integrations.nsfc.existing_nsfc_processes",
        lambda _data: next(calls),
    )
    monkeypatch.setattr(
        "crawler_gateway.integrations.nsfc.os.kill",
        lambda pid, sig: killed.append((pid, sig)),
    )
    assert stop_nsfc_processes(data) == [
        {"pid": 99101, "role": "autopilot", "command": "python"}
    ]
    assert killed == [(99101, signal.SIGTERM)]
