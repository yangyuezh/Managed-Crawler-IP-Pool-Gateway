from __future__ import annotations

import fcntl
import os
import re
import secrets
import shutil
import signal
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import yaml

from .config import AppConfig, ProviderConfig
from .paths import ProjectPaths
from .subscriptions import materialize_provider


class MihomoError(RuntimeError):
    pass


FAIL_CLOSED_PROXY = "REJECT"


@dataclass(frozen=True)
class ProxyNode:
    key: str
    provider: str
    name: str
    node_type: str
    alive: bool | None
    metadata: dict[str, Any]
    source_kind: str = "subscription"


def work_group(index: int) -> str:
    return f"CRAWLER-WORK-{index:02d}"


def probe_group(index: int) -> str:
    return f"CRAWLER-PROBE-{index:02d}"


def node_key(provider: str, name: str) -> str:
    return f"{provider}\x1f{name}"


def split_node_key(value: str) -> tuple[str, str]:
    provider, separator, name = value.partition("\x1f")
    if not separator:
        raise ValueError(f"invalid node key: {value!r}")
    return provider, name


def find_mihomo_binary() -> Path | None:
    candidates = [
        shutil.which("mihomo"),
        "/opt/homebrew/bin/mihomo",
        "/usr/local/bin/mihomo",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    return None


def ensure_secret(path: Path) -> str:
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if len(value) < 24:
            raise MihomoError(f"controller secret is unexpectedly short: {path}")
        return value
    value = secrets.token_urlsafe(36)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)
    return value


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "provider"


def _cached_provider_is_usable(destination: Path) -> bool:
    try:
        cached = yaml.safe_load(destination.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return False
    proxies = cached.get("proxies") if isinstance(cached, dict) else None
    return isinstance(proxies, list) and bool(proxies)


def _provider_payload(
    provider: ProviderConfig,
    config: AppConfig,
    runtime_dir: Path,
    *,
    refresh: bool = True,
) -> dict[str, Any]:
    filename = _safe_filename(provider.name) + ".yaml"
    destination = runtime_dir / "providers" / filename
    payload: dict[str, Any] = {
        "type": "file",
        "path": f"./providers/{filename}",
        "interval": provider.interval_seconds,
        "health-check": {
            "enable": True,
            "url": "https://cp.cloudflare.com/generate_204",
            "interval": 1800,
            "timeout": 8000,
            "lazy": True,
            "expected-status": 204,
        },
        "override": {"additional-prefix": f"[{provider.name}] "},
    }
    if refresh or not _cached_provider_is_usable(destination):
        try:
            materialize_provider(provider, config, destination)
        except Exception:  # noqa: BLE001
            if not _cached_provider_is_usable(destination):
                raise
    if provider.include:
        payload["filter"] = provider.include
    if provider.exclude:
        payload["exclude-filter"] = provider.exclude
    return payload


def render_config(
    config: AppConfig,
    paths: ProjectPaths,
    secret: str,
    *,
    refresh_providers: bool = True,
) -> Path:
    paths.ensure_directories()
    (paths.runtime_dir / "providers").mkdir(parents=True, exist_ok=True)
    provider_names = [provider.name for provider in config.providers]
    groups: list[dict[str, Any]] = []
    listeners: list[dict[str, Any]] = []

    for index in range(1, config.gateway.work_lanes + 1):
        group = work_group(index)
        groups.append(
            {
                "name": group,
                "type": "select",
                "proxies": [FAIL_CLOSED_PROXY, "DIRECT"],
                "use": provider_names,
            }
        )
        listeners.append(
            {
                "name": f"crawler-work-{index:02d}",
                "type": "mixed",
                "listen": config.gateway.listen,
                "port": config.gateway.work_port_base + index - 1,
                "users": [],
                "proxy": group,
            }
        )
    for index in range(1, config.gateway.probe_lanes + 1):
        group = probe_group(index)
        groups.append(
            {
                "name": group,
                "type": "select",
                "proxies": [FAIL_CLOSED_PROXY, "DIRECT"],
                "use": provider_names,
            }
        )
        listeners.append(
            {
                "name": f"crawler-probe-{index:02d}",
                "type": "mixed",
                "listen": config.gateway.listen,
                "port": config.gateway.probe_port_base + index - 1,
                "users": [],
                "proxy": group,
            }
        )

    payload: dict[str, Any] = {
        "allow-lan": False,
        "bind-address": config.gateway.listen,
        "mode": "rule",
        "log-level": "warning",
        "ipv6": False,
        "external-controller": config.gateway.controller,
        "secret": secret,
        "profile": {"store-selected": False, "store-fake-ip": False},
        "dns": {
            "enable": True,
            "ipv6": False,
            "enhanced-mode": "redir-host",
            "default-nameserver": list(config.gateway.direct_dns_servers),
            "nameserver": list(config.gateway.direct_dns_servers),
            "proxy-server-nameserver": list(config.gateway.direct_dns_servers),
        },
        "proxy-providers": {
            provider.name: _provider_payload(
                provider,
                config,
                paths.runtime_dir,
                refresh=refresh_providers,
            )
            for provider in config.providers
        },
        "proxy-groups": groups,
        "listeners": listeners,
        "rules": ["MATCH,DIRECT"],
    }
    if config.gateway.node_outbound_interface:
        payload["interface-name"] = config.gateway.node_outbound_interface
    temporary = paths.runtime_config_path.with_suffix(".yaml.tmp")
    temporary.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
    os.replace(temporary, paths.runtime_config_path)
    paths.runtime_config_path.chmod(0o600)
    return paths.runtime_config_path


class MihomoApi:
    def __init__(
        self,
        controller: str,
        secret: str,
        timeout: float = 15.0,
        *,
        include_direct: bool = True,
    ) -> None:
        self.base_url = "http://" + controller.rstrip("/")
        self.timeout = timeout
        self.include_direct = include_direct
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"Authorization": f"Bearer {secret}"})

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        try:
            response = self.session.request(
                method,
                self.base_url + path,
                timeout=self.timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise MihomoError(f"Mihomo API request failed: {exc}") from exc
        if response.status_code >= 400:
            body = response.text[:500]
            raise MihomoError(f"Mihomo API {method} {path} returned {response.status_code}: {body}")
        return response

    def version(self) -> dict[str, Any]:
        return self.request("GET", "/version").json()

    def provider_payload(self) -> dict[str, Any]:
        return self.request("GET", "/providers/proxies").json()

    def refresh_provider(self, provider: str) -> None:
        encoded = urllib.parse.quote(provider, safe="")
        self.request("PUT", f"/providers/proxies/{encoded}")

    def discover_nodes(self, configured_providers: tuple[ProviderConfig, ...]) -> list[ProxyNode]:
        payload = self.provider_payload()
        providers = payload.get("providers") if isinstance(payload, dict) else None
        if not isinstance(providers, dict):
            raise MihomoError("Mihomo provider response has no providers object")
        result: list[ProxyNode] = []
        if self.include_direct:
            direct_name = "[local] DIRECT"
            result.append(
                ProxyNode(
                    key=node_key("__local__", direct_name),
                    provider="__local__",
                    name="DIRECT",
                    node_type="direct",
                    alive=True,
                    metadata={"type": "direct", "display_name": direct_name},
                    source_kind="direct",
                )
            )
        for configured in configured_providers:
            provider = providers.get(configured.name)
            if not isinstance(provider, dict):
                continue
            proxies = provider.get("proxies")
            if not isinstance(proxies, list):
                continue
            for item in proxies:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not name or name in {"DIRECT", "REJECT", "COMPATIBLE"}:
                    continue
                result.append(
                    ProxyNode(
                        key=node_key(configured.name, name),
                        provider=configured.name,
                        name=name,
                        node_type=str(item.get("type") or "unknown"),
                        alive=item.get("alive") if isinstance(item.get("alive"), bool) else None,
                        metadata=item,
                        source_kind=(
                            "local"
                            if getattr(configured, "type", "") == "shadowrocket"
                            and getattr(configured, "scope", "all") == "local"
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

    def select(self, group: str, proxy_name: str) -> None:
        encoded = urllib.parse.quote(group, safe="")
        self.request("PUT", f"/proxies/{encoded}", json={"name": proxy_name})

    def group(self, group: str) -> dict[str, Any]:
        encoded = urllib.parse.quote(group, safe="")
        return self.request("GET", f"/proxies/{encoded}").json()

    def release(self, group: str) -> None:
        self.select(group, FAIL_CLOSED_PROXY)


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class MihomoProcess:
    def __init__(self, paths: ProjectPaths, config: AppConfig, secret: str) -> None:
        self.paths = paths
        self.config = config
        self.secret = secret

    def pid(self) -> int | None:
        try:
            value = int(self.paths.pid_path.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError):
            return None
        if _pid_is_running(value):
            command = ""
            try:
                command = subprocess.run(
                    ["ps", "-p", str(value), "-o", "command="],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=3,
                ).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                command = ""
            if "mihomo" in command and str(self.paths.runtime_dir) in command:
                return value
        self.paths.pid_path.unlink(missing_ok=True)
        return None

    def api(self) -> MihomoApi:
        return MihomoApi(
            self.config.gateway.controller,
            self.secret,
            include_direct=self.config.gateway.include_direct,
        )

    def running(self) -> bool:
        return self.pid() is not None

    def start(self, wait_seconds: float = 20.0) -> int:
        lock_path = self.paths.runtime_dir / "mihomo-start.lock"
        lock_path.touch(mode=0o600, exist_ok=True)
        with lock_path.open("r+") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                return self._start_locked(wait_seconds)
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _start_locked(self, wait_seconds: float) -> int:
        current = self.pid()
        if current is not None:
            return current
        binary = find_mihomo_binary()
        if binary is None:
            raise MihomoError("mihomo is not installed or not on PATH")
        if not self.paths.runtime_config_path.exists():
            raise MihomoError("runtime configuration is missing; render it before start")
        self.paths.logs_dir.mkdir(parents=True, exist_ok=True)
        log_handle = self.paths.mihomo_log_path.open("a", encoding="utf-8")
        process = subprocess.Popen(
            [str(binary), "-d", str(self.paths.runtime_dir), "-f", str(self.paths.runtime_config_path)],
            cwd=self.paths.runtime_dir,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_handle.close()
        self.paths.pid_path.write_text(str(process.pid) + "\n", encoding="utf-8")
        deadline = time.monotonic() + wait_seconds
        last_error = ""
        while time.monotonic() < deadline:
            if process.poll() is not None:
                tail = ""
                try:
                    tail = self.paths.mihomo_log_path.read_text(encoding="utf-8")[-2000:]
                except OSError:
                    pass
                self.paths.pid_path.unlink(missing_ok=True)
                raise MihomoError(f"mihomo exited with code {process.returncode}\n{tail}")
            try:
                api = self.api()
                api.version()
                for index in range(1, self.config.gateway.work_lanes + 1):
                    api.release(work_group(index))
                for index in range(1, self.config.gateway.probe_lanes + 1):
                    api.release(probe_group(index))
                return process.pid
            except MihomoError as exc:
                last_error = str(exc)
                time.sleep(0.25)
        self.stop()
        raise MihomoError(f"mihomo API did not become ready: {last_error}")

    def stop(self, wait_seconds: float = 10.0) -> bool:
        pid = self.pid()
        if pid is None:
            return False
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if not _pid_is_running(pid):
                self.paths.pid_path.unlink(missing_ok=True)
                return True
            time.sleep(0.2)
        os.kill(pid, signal.SIGKILL)
        self.paths.pid_path.unlink(missing_ok=True)
        return True

    def describe(self) -> dict[str, Any]:
        pid = self.pid()
        result: dict[str, Any] = {"backend": "mihomo", "running": pid is not None, "pid": pid}
        if pid is not None:
            try:
                result["version"] = self.api().version()
            except MihomoError as exc:
                result["api_error"] = str(exc)
        return result
