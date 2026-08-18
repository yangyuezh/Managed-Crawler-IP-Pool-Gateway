from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

from .config import AppConfig, ProviderConfig
from .mihomo import MihomoError, ProxyNode, node_key
from .paths import ProjectPaths
from .subscriptions import materialize_provider


_GROUP_PATTERN = re.compile(r"^CRAWLER-(WORK|PROBE)-(\d{2})$")


def find_xray_binary() -> Path | None:
    candidates = [
        shutil.which("xray"),
        "/opt/homebrew/opt/xray/bin/xray",
        "/usr/local/opt/xray/bin/xray",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    return None


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "provider"


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _matching_lane_pids(config_path: Path) -> list[int]:
    """Find every Xray process using one lane config, including orphaned runs."""
    try:
        output = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    marker = f"xray run -config {config_path}"
    result: list[int] = []
    for line in output.splitlines():
        pid_text, separator, command = line.strip().partition(" ")
        if separator and marker in command:
            try:
                result.append(int(pid_text))
            except ValueError:
                continue
    return result


def _terminate_pids(pids: list[int], wait_seconds: float) -> bool:
    active = {pid for pid in pids if _pid_is_running(pid)}
    if not active:
        return False
    for pid in active:
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + wait_seconds
    while active and time.monotonic() < deadline:
        active = {pid for pid in active if _pid_is_running(pid)}
        if active:
            time.sleep(0.1)
    for pid in active:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return True


def parse_fragment(value: Any) -> dict[str, Any] | None:
    text = str(value or "").strip()
    if not text:
        return None
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 4 or parts[0].lower() in {"0", "false", "off"}:
        return None
    _enabled, length, delay, packets = parts
    if not length or not delay or not packets:
        return None
    return {"packets": packets, "length": length, "delay": delay}


def xray_config_for_node(
    node: ProxyNode,
    *,
    listen: str,
    port: int,
    outbound_interface: str = "",
    direct_dns_servers: tuple[str, ...] = ("1.1.1.1", "8.8.8.8"),
    loglevel: str = "warning",
) -> dict[str, Any]:
    item = node.metadata
    node_type = str(item.get("type") or "").lower()
    if node_type == "direct":
        outbound: dict[str, Any] = {
            "tag": "node-out",
            "protocol": "freedom",
            "settings": {"domainStrategy": "UseIPv4"},
        }
        if outbound_interface:
            outbound["streamSettings"] = {
                "sockopt": {"interface": outbound_interface}
            }
        return {
            "log": {"loglevel": loglevel},
            "dns": {
                "servers": [
                    {
                        "address": address,
                        "port": 53,
                        "queryStrategy": "UseIPv4",
                    }
                    for address in direct_dns_servers
                ],
                "queryStrategy": "UseIPv4",
                "useSystemHosts": False,
            },
            "inbounds": [
                {
                    "listen": listen,
                    "port": port,
                    "protocol": "http",
                    "settings": {},
                    "tag": "crawler-in",
                }
            ],
            "outbounds": [outbound],
        }
    if node_type != "vless":
        raise MihomoError(f"Xray backend does not yet support node type {node_type!r}")
    server = str(item.get("server") or "").strip()
    uuid = str(item.get("uuid") or "").strip()
    try:
        server_port = int(item.get("port"))
    except (TypeError, ValueError) as exc:
        raise MihomoError(f"node {node.name!r} has an invalid port") from exc
    if not server or not uuid:
        raise MihomoError(f"node {node.name!r} is missing server or UUID")

    settings: dict[str, Any] = {
        "address": server,
        "port": server_port,
        "id": uuid,
        "encryption": str(item.get("encryption") or "none"),
    }
    if item.get("flow"):
        settings["flow"] = str(item["flow"])

    network = str(item.get("network") or "tcp").lower()
    stream: dict[str, Any] = {"network": network, "security": "none"}
    if item.get("tls"):
        stream["security"] = "tls"
        tls: dict[str, Any] = {
            "serverName": str(item.get("servername") or ""),
            "fingerprint": str(item.get("client-fingerprint") or "chrome"),
            "allowInsecure": bool(item.get("skip-cert-verify", False)),
        }
        if network == "ws":
            tls["alpn"] = ["http/1.1"]
        stream["tlsSettings"] = tls

    if network == "ws":
        ws = item.get("ws-opts") if isinstance(item.get("ws-opts"), dict) else {}
        headers = ws.get("headers") if isinstance(ws.get("headers"), dict) else {}
        host = str(headers.get("Host") or headers.get("host") or "")
        stream["wsSettings"] = {
            "path": str(ws.get("path") or "/"),
            "host": host,
        }
    elif network == "grpc":
        grpc = item.get("grpc-opts") if isinstance(item.get("grpc-opts"), dict) else {}
        stream["grpcSettings"] = {
            "serviceName": str(grpc.get("grpc-service-name") or ""),
        }

    if outbound_interface:
        stream["sockopt"] = {"interface": outbound_interface}
    # Subscription fragment hints are client-specific. Injecting them into
    # Xray's stream config breaks otherwise valid TLS/WebSocket nodes.

    return {
        "log": {"loglevel": loglevel},
        "inbounds": [
            {
                "listen": listen,
                "port": port,
                "protocol": "http",
                "settings": {},
                "tag": "crawler-in",
            }
        ],
        "outbounds": [
            {
                "tag": "node-out",
                "protocol": "vless",
                "settings": settings,
                "streamSettings": stream,
            }
        ],
    }


class XrayApi:
    def __init__(self, process: "XrayProcess") -> None:
        self.process = process

    def version(self) -> dict[str, Any]:
        return self.process.version()

    def refresh_provider(self, provider: str) -> int:
        return self.process.refresh_provider(provider)

    def discover_nodes(self, configured_providers: tuple[ProviderConfig, ...]) -> list[ProxyNode]:
        return self.process.discover_nodes(configured_providers)

    def select(self, group: str, proxy_name: str) -> None:
        node = next(
            (item for item in self.process.discover_nodes(self.process.config.providers) if item.name == proxy_name),
            None,
        )
        if node is None:
            raise MihomoError(f"proxy node is not present in current subscriptions: {proxy_name}")
        kind, index = self.process.parse_group(group)
        self.process.start_lane(kind, index, node)

    def group(self, group: str) -> dict[str, Any]:
        kind, index = self.process.parse_group(group)
        selected = self.process.selected_node(kind, index)
        return {"now": selected or "DIRECT"}

    def release(self, group: str) -> None:
        kind, index = self.process.parse_group(group)
        self.process.stop_lane(kind, index)


class XrayProcess:
    def __init__(self, paths: ProjectPaths, config: AppConfig) -> None:
        self.paths = paths
        self.config = config

    def binary(self) -> Path:
        binary = find_xray_binary()
        if binary is None:
            raise MihomoError("Xray is not installed or not on PATH")
        return binary

    def provider_path(self, provider: ProviderConfig) -> Path:
        return self.paths.runtime_dir / "providers" / (_safe_filename(provider.name) + ".yaml")

    def refresh_provider(self, provider_name: str) -> int:
        provider = next((item for item in self.config.providers if item.name == provider_name), None)
        if provider is None:
            raise MihomoError(f"unknown provider: {provider_name}")
        return materialize_provider(
            provider,
            self.config,
            self.provider_path(provider),
        ).node_count

    def provider_count(self, provider: ProviderConfig) -> int:
        path = self.provider_path(provider)
        if not path.is_file():
            return self.refresh_provider(provider.name)
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise MihomoError(
                f"cannot read cached provider {provider.name!r}: {type(exc).__name__}"
            ) from exc
        rows = payload.get("proxies") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            return self.refresh_provider(provider.name)
        return len(rows)

    def prepare(self, *, refresh: bool = True) -> dict[str, int]:
        self.paths.ensure_directories()
        self.binary()
        if refresh:
            return {
                provider.name: self.refresh_provider(provider.name)
                for provider in self.config.providers
            }
        return {
            provider.name: self.provider_count(provider)
            for provider in self.config.providers
        }

    def start(self) -> int:
        counts = self.prepare(refresh=False)
        payload = {
            "backend": "xray",
            "enabled_at": time.time(),
            "providers": counts,
        }
        temporary = self.paths.xray_marker_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.paths.xray_marker_path)
        self.paths.xray_marker_path.chmod(0o600)
        return sum(counts.values())

    def running(self) -> bool:
        return self.paths.xray_marker_path.is_file() and find_xray_binary() is not None

    def pid(self) -> int | None:
        for kind, count in (
            ("work", self.config.gateway.work_lanes),
            ("probe", self.config.gateway.probe_lanes),
        ):
            for index in range(1, count + 1):
                pid = self.lane_pid(kind, index)
                if pid is not None:
                    return pid
        return None

    def api(self) -> XrayApi:
        return XrayApi(self)

    def version(self) -> dict[str, Any]:
        result = subprocess.run(
            [str(self.binary()), "version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        first_line = (result.stdout or result.stderr).splitlines()[0] if (result.stdout or result.stderr) else ""
        return {"backend": "xray", "version": first_line.strip()}

    def parse_group(self, group: str) -> tuple[str, int]:
        match = _GROUP_PATTERN.fullmatch(group)
        if not match:
            raise MihomoError(f"invalid crawler lane group: {group}")
        kind = match.group(1).lower()
        index = int(match.group(2))
        limit = self.config.gateway.work_lanes if kind == "work" else self.config.gateway.probe_lanes
        if not 1 <= index <= limit:
            raise MihomoError(f"{kind} lane {index} is outside configured range 1-{limit}")
        return kind, index

    def lane_port(self, kind: str, index: int) -> int:
        base = self.config.gateway.work_port_base if kind == "work" else self.config.gateway.probe_port_base
        return base + index - 1

    def lane_dir(self, kind: str, index: int) -> Path:
        return self.paths.xray_dir / f"{kind}-{index:02d}"

    def lane_pid(self, kind: str, index: int) -> int | None:
        path = self.lane_dir(kind, index) / "xray.pid"
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError):
            return None
        if _pid_is_running(pid):
            try:
                command = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "command="],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=3,
                ).stdout
            except (OSError, subprocess.SubprocessError):
                command = ""
            if "xray" in command and str(self.lane_dir(kind, index)) in command:
                return pid
        path.unlink(missing_ok=True)
        return None

    def stop_lane(self, kind: str, index: int, wait_seconds: float = 6.0) -> bool:
        lane = self.lane_dir(kind, index)
        config_path = lane / "config.json"
        pids = _matching_lane_pids(config_path)
        marker_pid = self.lane_pid(kind, index)
        if marker_pid is not None and marker_pid not in pids:
            pids.append(marker_pid)
        stopped = _terminate_pids(pids, wait_seconds)
        (lane / "xray.pid").unlink(missing_ok=True)
        (lane / "selected.json").unlink(missing_ok=True)
        return stopped

    def start_lane(self, kind: str, index: int, node: ProxyNode) -> int:
        lane = self.lane_dir(kind, index)
        lane.mkdir(parents=True, exist_ok=True)
        self.stop_lane(kind, index)
        port = self.lane_port(kind, index)
        outbound_interface = (
            self.config.gateway.direct_outbound_interface
            if node.node_type.casefold() == "direct"
            else self.config.gateway.node_outbound_interface
        )
        config_path = lane / "config.json"
        payload = xray_config_for_node(
            node,
            listen=self.config.gateway.listen,
            port=port,
            outbound_interface=outbound_interface,
            direct_dns_servers=self.config.gateway.direct_dns_servers,
        )
        temporary = config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, config_path)
        config_path.chmod(0o600)
        binary = self.binary()
        checked = subprocess.run(
            [str(binary), "run", "-test", "-config", str(config_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        if checked.returncode != 0:
            message = (checked.stderr or checked.stdout)[-1000:]
            raise MihomoError(f"Xray rejected lane configuration: {message}")

        log_path = self.paths.logs_dir / f"xray-{kind}-{index:02d}.log"
        log_handle = log_path.open("a", encoding="utf-8")
        process = subprocess.Popen(
            [str(binary), "run", "-config", str(config_path)],
            cwd=lane,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_handle.close()
        (lane / "xray.pid").write_text(str(process.pid) + "\n", encoding="utf-8")
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if process.poll() is not None:
                (lane / "xray.pid").unlink(missing_ok=True)
                raise MihomoError(f"Xray lane {kind}-{index:02d} exited with code {process.returncode}")
            try:
                with socket.create_connection((self.config.gateway.listen, port), timeout=0.25):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            self.stop_lane(kind, index)
            raise MihomoError(f"Xray lane {kind}-{index:02d} did not open port {port}")
        duplicates = [pid for pid in _matching_lane_pids(config_path) if pid != process.pid]
        if duplicates:
            self.stop_lane(kind, index)
            raise MihomoError(
                f"Xray lane {kind}-{index:02d} has duplicate processes: {duplicates}"
            )
        selected = {
            "node_key": node.key,
            "provider": node.provider,
            "proxy_name": node.name,
            "port": port,
            "pid": process.pid,
        }
        selected_path = lane / "selected.json"
        selected_path.write_text(json.dumps(selected, ensure_ascii=False), encoding="utf-8")
        selected_path.chmod(0o600)
        return process.pid

    def selected_node(self, kind: str, index: int) -> str | None:
        if self.lane_pid(kind, index) is None:
            return None
        try:
            payload = json.loads((self.lane_dir(kind, index) / "selected.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        return str(payload.get("proxy_name") or "") or None

    def discover_nodes(self, providers: tuple[ProviderConfig, ...]) -> list[ProxyNode]:
        result: list[ProxyNode] = []
        if self.config.gateway.include_direct:
            direct_name = "[local] DIRECT"
            result.append(
                ProxyNode(
                    key=node_key("__local__", direct_name),
                    provider="__local__",
                    name=direct_name,
                    node_type="direct",
                    alive=None,
                    metadata={"type": "direct"},
                    source_kind="direct",
                )
            )
        for provider in providers:
            path = self.provider_path(provider)
            if not path.is_file():
                self.refresh_provider(provider.name)
            try:
                payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise MihomoError(f"cannot read provider {provider.name!r}: {type(exc).__name__}") from exc
            rows = payload.get("proxies") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                continue
            include = re.compile(provider.include) if provider.include else None
            exclude = re.compile(provider.exclude) if provider.exclude else None
            for item in rows:
                if not isinstance(item, dict):
                    continue
                original = str(item.get("name") or "").strip()
                if not original or (include and not include.search(original)) or (exclude and exclude.search(original)):
                    continue
                display = f"[{provider.name}] {original}"
                result.append(
                    ProxyNode(
                        key=node_key(provider.name, display),
                        provider=provider.name,
                        name=display,
                        node_type=str(item.get("type") or "unknown"),
                        alive=None,
                        metadata=dict(item),
                        source_kind=(
                            "local"
                            if provider.type == "shadowrocket" and provider.scope == "local"
                            else "subscription"
                        ),
                    )
                )
        result.sort(
            key=lambda node: (
                {"subscription": 0, "local": 1, "direct": 2}.get(
                    node.source_kind,
                    1,
                ),
                node.provider.casefold(),
                node.name.casefold(),
            )
        )
        return result

    def stop(self) -> bool:
        stopped = False
        for kind, count in (
            ("work", self.config.gateway.work_lanes),
            ("probe", self.config.gateway.probe_lanes),
        ):
            for index in range(1, count + 1):
                stopped = self.stop_lane(kind, index) or stopped
        if self.paths.xray_marker_path.exists():
            self.paths.xray_marker_path.unlink()
            stopped = True
        return stopped

    def describe(self) -> dict[str, Any]:
        lanes: list[dict[str, Any]] = []
        for kind, count in (
            ("work", self.config.gateway.work_lanes),
            ("probe", self.config.gateway.probe_lanes),
        ):
            for index in range(1, count + 1):
                pid = self.lane_pid(kind, index)
                lanes.append(
                    {
                        "kind": kind,
                        "lane": index,
                        "port": self.lane_port(kind, index),
                        "pid": pid,
                        "running": pid is not None,
                        "proxy_name": self.selected_node(kind, index),
                    }
                )
        result: dict[str, Any] = {
            "backend": "xray",
            "running": self.running(),
            "lanes": lanes,
        }
        if find_xray_binary() is not None:
            result["version"] = self.version()
        return result
