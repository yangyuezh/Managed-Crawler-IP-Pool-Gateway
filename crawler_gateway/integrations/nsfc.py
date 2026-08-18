from __future__ import annotations

import os
import json
import signal
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..controller import GatewayController
from ..faults import clear_fault, fault_path


_GATEWAY_ROOT = Path(__file__).resolve().parents[2]
_SIBLING_NSFC_REPO_NAMES = (
    "NSFC-Official-Projects-Database",
    "NSFC-Official-Final-Projects",
)
_NSFC_USER_AGENT_ENV = "NSFC_USER_AGENT"


def _default_nsfc_repo() -> Path:
    configured = os.environ.get("NSFC_REPO", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()

    candidates = [_GATEWAY_ROOT]
    candidates.extend(_GATEWAY_ROOT.parent / name for name in _SIBLING_NSFC_REPO_NAMES)
    for candidate in candidates:
        if (candidate / "crawler" / "nsfc_detail_autopilot.py").is_file():
            return candidate
    return _GATEWAY_ROOT.parent / _SIBLING_NSFC_REPO_NAMES[0]


def _default_nsfc_data(repo: Path) -> Path:
    configured = os.environ.get("NSFC_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return repo / "data"


DEFAULT_NSFC_REPO = _default_nsfc_repo()
DEFAULT_NSFC_DATA = _default_nsfc_data(DEFAULT_NSFC_REPO)


def existing_nsfc_processes(data_dir: Path) -> list[dict[str, Any]]:
    """Return Python crawler/controller processes writing the requested data directory."""
    resolved_data = str(data_dir.expanduser().resolve())
    if os.name == "nt":
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | ForEach-Object { "
                "\"$($_.ProcessId)`t$($_.CommandLine)\" }",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
        )
    found: list[dict[str, Any]] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        command = parts[1]
        if pid == os.getpid() or resolved_data not in command:
            continue
        if not any(
            marker in command
            for marker in ("nsfc_detail_autopilot.py", "scrape_nsfc_official_final.py")
        ):
            continue
        try:
            executable = Path(shlex.split(command)[0]).name.casefold()
        except (ValueError, IndexError):
            continue
        if "python" not in executable:
            continue
        role = "autopilot" if "nsfc_detail_autopilot.py" in command else "detail_worker"
        found.append({"pid": pid, "role": role, "command": command})
    return found


def stop_nsfc_processes(data_dir: Path, wait_seconds: float = 15.0) -> list[dict[str, Any]]:
    found = existing_nsfc_processes(data_dir)
    if not found:
        return []
    for item in sorted(found, key=lambda value: value["role"] != "autopilot"):
        pid = int(item["pid"])
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            else:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + wait_seconds
    remaining = existing_nsfc_processes(data_dir)
    while remaining and time.monotonic() < deadline:
        time.sleep(0.2)
        remaining = existing_nsfc_processes(data_dir)
    for item in remaining:
        pid = int(item["pid"])
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            else:
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return found


def nsfc_progress(repo: Path, data_dir: Path) -> dict[str, Any]:
    script = repo.expanduser().resolve() / "crawler" / "nsfc_status.py"
    if not script.is_file():
        return {"available": False, "error": f"status script does not exist: {script}"}
    try:
        result = subprocess.run(
            [sys.executable, str(script), "--data-dir", str(data_dir.expanduser()), "--json"],
            cwd=repo.expanduser().resolve(),
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "error": str(exc)}
    if result.returncode != 0:
        return {
            "available": False,
            "error": (result.stderr or result.stdout).strip()[-1000:],
        }
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {"available": False, "error": f"invalid status output: {exc}"}
    controls = existing_nsfc_processes(data_dir)
    process_state = str(payload.get("process_state") or "STOPPED")
    if process_state == "STOPPED" and controls:
        process_state = "WAITING"
    return {
        "available": True,
        "base_rows": payload.get("base_rows"),
        "detail_success_files": payload.get("detail_success_files"),
        "continuous_done": payload.get("continuous_done"),
        "remaining": payload.get("remaining"),
        "percent": payload.get("percent"),
        "next": payload.get("next"),
        "active_detail_errors": payload.get("active_detail_errors"),
        "validation_errors": payload.get("validation_errors"),
        "recent": payload.get("recent"),
        "rates_per_hour": payload.get("rates_per_hour"),
        "process_state": process_state,
        "control_processes": [
            {"pid": item["pid"], "role": item["role"]} for item in controls
        ],
    }


def nsfc_command(
    *,
    python: str,
    repo: Path,
    data_dir: Path,
    proxy_urls: list[str],
    min_delay: float,
    max_delay: float,
    retries: int,
    timeout: float,
) -> list[str]:
    script = repo / "crawler" / "nsfc_detail_autopilot.py"
    if not script.is_file():
        raise ValueError(f"NSFC autopilot script does not exist: {script}")
    database = data_dir / "final" / "nsfc_official_final_projects.sqlite"
    if not database.is_file():
        raise ValueError(f"NSFC base database does not exist: {database}")
    if not proxy_urls:
        raise ValueError("no assigned proxy lanes are available")
    command = [
        python,
        str(script),
        "--data-dir",
        str(data_dir),
        "--workers",
        str(len(proxy_urls)),
        "--min-delay",
        str(min_delay),
        "--max-delay",
        str(max_delay),
        "--retries",
        str(retries),
        "--timeout",
        str(timeout),
        "--blocked-wait",
        "60",
        "--restart-wait",
        "10",
        "--monitor-interval",
        "5",
        "--status-interval",
        "60",
        "--stall-timeout",
        "900",
    ]
    for proxy in proxy_urls:
        command.extend(["--proxy-list", proxy])
    return command


def monitor_command(*, python: str, config_path: Path, target: str) -> list[str]:
    return [
        python,
        "-m",
        "crawler_gateway",
        "--config",
        str(config_path),
        "monitor",
        target,
    ]


def nsfc_child_environment(headers: dict[str, str]) -> dict[str, str]:
    environment = os.environ.copy()
    user_agent = headers.get("User-Agent", "").strip()
    if user_agent:
        environment[_NSFC_USER_AGENT_ENV] = user_agent
    return environment


def run_nsfc(
    *,
    controller: GatewayController,
    config_path: Path,
    target: str,
    lanes: int,
    repo: Path,
    data_dir: Path,
    min_delay: float,
    max_delay: float,
    retries: int,
    timeout: float,
    wait_for_lanes: bool = False,
    lane_retry_seconds: float = 300.0,
) -> int:
    if target not in controller.config.targets:
        raise ValueError(f"unknown gateway target: {target}")
    process = controller.process
    if not process.running():
        raise ValueError("gateway is stopped; start it before run-nsfc")
    existing = existing_nsfc_processes(data_dir)
    if existing:
        processes = ", ".join(
            f"PID {item['pid']} ({item['role']})" for item in existing
        )
        raise ValueError(
            "an NSFC process is already using this data directory; "
            f"refusing a duplicate start: {processes}"
        )
    while True:
        leases = controller.ensure_lanes(target, lanes=lanes, refresh=True)
        proxy_urls = controller.proxy_urls(target)
        if len(leases) == lanes and len(proxy_urls) == lanes:
            break
        if not wait_for_lanes:
            raise ValueError(
                f"requested {lanes} lanes but only {len(proxy_urls)} target-verified lanes are assigned"
            )
        print(
            f"healthy lanes unavailable ({len(proxy_urls)}/{lanes}); "
            f"refreshing again in {lane_retry_seconds:.0f}s",
            flush=True,
        )
        time.sleep(lane_retry_seconds)

    python = sys.executable
    child_environment = nsfc_child_environment(controller.config.targets[target].headers)
    monitor = subprocess.Popen(
        monitor_command(python=python, config_path=config_path, target=target),
        cwd=controller.paths.root,
        start_new_session=True,
    )
    try:
        crawler = subprocess.Popen(
            nsfc_command(
                python=python,
                repo=repo.resolve(),
                data_dir=data_dir.resolve(),
                proxy_urls=proxy_urls,
                min_delay=min_delay,
                max_delay=max_delay,
                retries=retries,
                timeout=timeout,
            ),
            cwd=repo.resolve(),
            env=child_environment,
            start_new_session=True,
        )
    except Exception:
        try:
            os.killpg(monitor.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        raise
    clear_fault(fault_path(controller.config.gateway.fault_file))
    print(
        f"NSFC multi-lane crawler started: crawler_pid={crawler.pid} "
        f"monitor_pid={monitor.pid} lanes={lanes}",
        flush=True,
    )
    for lease in leases:
        print(
            f"lane={lease.lane} proxy=http://{controller.config.gateway.listen}:{lease.port} "
            f"egress_ip={lease.egress_ip} provider={lease.provider}",
            flush=True,
        )

    stop_requested = False

    def request_stop(signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True
        print(f"received signal {signum}; stopping NSFC crawler and lane monitor", flush=True)

    previous_handlers: dict[int, Any] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_stop)
    try:
        while not stop_requested:
            crawler_code = crawler.poll()
            monitor_code = monitor.poll()
            if crawler_code is not None:
                print(f"NSFC crawler exited with code={crawler_code}", flush=True)
                return int(crawler_code)
            if monitor_code is not None:
                print(f"lane monitor exited with code={monitor_code}; stopping crawler", flush=True)
                return int(monitor_code or 1)
            time.sleep(1)
    finally:
        for child in (crawler, monitor):
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and any(child.poll() is None for child in (crawler, monitor)):
            time.sleep(0.2)
        for child in (crawler, monitor):
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return 130
