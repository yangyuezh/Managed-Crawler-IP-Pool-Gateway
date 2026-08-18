from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .paths import ProjectPaths


SERVICE_LABEL = "com.yangyuezh.crawler-gateway.reserve-maintenance"
BOOTSTRAP_ATTEMPTS = 21
BOOTSTRAP_RETRY_SECONDS = 2.0


class LaunchAgentError(RuntimeError):
    pass


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"


def service_target() -> str:
    return f"gui/{os.getuid()}/{SERVICE_LABEL}"


def launch_agent_payload(
    paths: ProjectPaths,
    config_path: Path,
    python: str | None = None,
) -> dict[str, Any]:
    executable = str(Path(python or sys.executable).expanduser().resolve())
    return {
        "Label": SERVICE_LABEL,
        "ProgramArguments": [
            executable,
            "-m",
            "crawler_gateway",
            "--event-log",
            str(paths.logs_dir / "reserve-maintenance.jsonl"),
            "--config",
            str(config_path.expanduser().resolve()),
            "maintain-reserve",
        ],
        "WorkingDirectory": "/",
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "ThrottleInterval": 30,
        "EnvironmentVariables": {
            "CRAWLER_GATEWAY_BACKGROUND_SERVICE": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(paths.root),
        },
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": str(paths.logs_dir / "reserve-maintenance.err.log"),
    }


def write_launch_agent(
    paths: ProjectPaths,
    config_path: Path,
    python: str | None = None,
) -> Path:
    if sys.platform != "darwin":
        raise LaunchAgentError("automatic maintenance service is supported only on macOS")
    paths.ensure_directories()
    destination = launch_agent_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".plist.tmp")
    with temporary.open("wb") as handle:
        plistlib.dump(
            launch_agent_payload(paths, config_path, python),
            handle,
            fmt=plistlib.FMT_XML,
            sort_keys=True,
        )
    os.replace(temporary, destination)
    destination.chmod(0o600)
    return destination


def _launchctl(*arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["launchctl", *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except subprocess.TimeoutExpired as exc:
        action = arguments[0] if arguments else "operation"
        raise LaunchAgentError(f"launchctl {action} timed out") from exc


def _disabled() -> bool:
    result = _launchctl("print-disabled", f"gui/{os.getuid()}")
    if result.returncode != 0:
        return False
    pattern = rf'"{re.escape(SERVICE_LABEL)}"\s*=>\s*(?:true|disabled)'
    return re.search(pattern, result.stdout) is not None


def _bootstrap_agent(
    destination: Path,
    *,
    attempts: int = BOOTSTRAP_ATTEMPTS,
    retry_seconds: float = BOOTSTRAP_RETRY_SECONDS,
) -> subprocess.CompletedProcess[str]:
    domain = f"gui/{os.getuid()}"
    result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(max(1, attempts)):
        result = _launchctl("bootstrap", domain, str(destination))
        if result.returncode == 0:
            return result
        message = f"{result.stderr}\n{result.stdout}".casefold()
        transient = "bootstrap failed: 5" in message or "input/output error" in message
        if not transient or attempt + 1 >= max(1, attempts):
            return result
        time.sleep(max(0.0, retry_seconds))
    assert result is not None
    return result


def service_status() -> dict[str, Any]:
    destination = launch_agent_path()
    if sys.platform != "darwin":
        return {
            "supported": False,
            "installed": destination.is_file(),
            "enabled": False,
            "loaded": False,
            "running": False,
            "pid": None,
            "label": SERVICE_LABEL,
            "plist": str(destination),
        }
    result = _launchctl("print", service_target())
    output = result.stdout if result.returncode == 0 else ""
    pid_match = re.search(r"^\s*pid\s*=\s*(\d+)\s*$", output, re.MULTILINE)
    state_match = re.search(r"^\s*state\s*=\s*([^\s]+)\s*$", output, re.MULTILINE)
    state = state_match.group(1) if state_match else None
    pid = int(pid_match.group(1)) if pid_match else None
    loaded = result.returncode == 0
    return {
        "supported": True,
        "installed": destination.is_file(),
        "enabled": destination.is_file() and not _disabled(),
        "loaded": loaded,
        "running": loaded and (pid is not None or state == "running"),
        "pid": pid,
        "state": state,
        "label": SERVICE_LABEL,
        "plist": str(destination),
    }


def _maintenance_pid(paths: ProjectPaths) -> int | None:
    pid_path = paths.runtime_dir / "reserve_maintenance.pid"
    try:
        pid = int(pid_path.read_text(encoding="ascii").strip())
        os.kill(pid, 0)
    except (FileNotFoundError, OSError, ValueError):
        return None
    return pid


def install_and_start_service(
    paths: ProjectPaths,
    config_path: Path,
    python: str | None = None,
    wait_seconds: float = 8.0,
) -> dict[str, Any]:
    destination = write_launch_agent(paths, config_path, python)
    target = service_target()
    bootout = _launchctl("bootout", target)
    if bootout.returncode == 0:
        time.sleep(0.25)
    enabled = _launchctl("enable", target)
    if enabled.returncode != 0:
        raise LaunchAgentError(
            f"launchctl enable failed: {(enabled.stderr or enabled.stdout).strip()}"
        )
    loaded = _bootstrap_agent(destination)
    if loaded.returncode != 0:
        raise LaunchAgentError(
            f"launchctl bootstrap failed: {(loaded.stderr or loaded.stdout).strip()}"
        )
    deadline = time.monotonic() + max(0.0, wait_seconds)
    status = service_status()
    maintenance_pid = _maintenance_pid(paths)
    while (
        not status["running"] or maintenance_pid != status.get("pid")
    ) and time.monotonic() < deadline:
        time.sleep(0.2)
        status = service_status()
        maintenance_pid = _maintenance_pid(paths)
    status["maintenance_ready"] = maintenance_pid == status.get("pid")
    status["maintenance_pid"] = maintenance_pid
    if not status["maintenance_ready"]:
        stop_service(remove=False)
        raise LaunchAgentError(
            "automatic maintenance service loaded but did not enter its maintenance loop"
        )
    return status


def stop_service(*, remove: bool = False) -> dict[str, Any]:
    destination = launch_agent_path()
    if sys.platform != "darwin":
        if remove:
            destination.unlink(missing_ok=True)
        return service_status()
    target = service_target()
    _launchctl("disable", target)
    _launchctl("bootout", target)
    if remove:
        destination.unlink(missing_ok=True)
    return service_status()
