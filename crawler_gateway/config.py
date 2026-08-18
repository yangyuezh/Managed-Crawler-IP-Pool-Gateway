from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class GatewaySettings:
    backend: str = "xray"
    include_direct: bool = True
    direct_dns_servers: tuple[str, ...] = ("1.1.1.1", "8.8.8.8")
    work_lanes: int = 3
    max_work_lanes: int = 6
    work_port_base: int = 17891
    probe_lanes: int = 2
    probe_port_base: int = 17991
    listen: str = "127.0.0.1"
    controller: str = "127.0.0.1:19090"
    settle_seconds: float = 1.5
    probe_timeout_seconds: float = 20.0
    target_probe_attempts: int = 2
    target_probe_retry_seconds: float = 1.0
    inventory_concurrency: int = 2
    healthy_max_age_hours: float = 24.0
    failure_threshold: int = 3
    monitor_interval_seconds: float = 30.0
    direct_outbound_interface: str = ""
    node_outbound_interface: str = ""
    reinventory_cooldown_seconds: float = 300.0
    reserve_refresh_interval_seconds: float = 3600.0
    maintenance_error_retry_seconds: float = 60.0
    probe_history_retention_days: float = 90.0
    maintenance_targets: tuple[str, ...] = ()
    fault_file: str = "~/Desktop/爬虫故障.txt"


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    type: str
    url: str = ""
    path: str = ""
    scope: str = "all"
    interval_seconds: int = 3600
    include: str = ""
    exclude: str = ""
    refresh_remote: bool = False
    user_agent: str = "Shadowrocket/2700 CFNetwork/1568.100.1 Darwin/24.0.0"


@dataclass(frozen=True)
class JsonCheck:
    path: str
    present: bool | None = None
    equals: Any = field(default=None)
    has_equals: bool = False


@dataclass(frozen=True)
class TargetConfig:
    name: str
    method: str
    url: str
    headers: dict[str, str]
    form: dict[str, Any] | None
    json_body: dict[str, Any] | None
    expected_statuses: tuple[int, ...]
    json_checks: tuple[JsonCheck, ...]


@dataclass(frozen=True)
class AppConfig:
    path: Path
    gateway: GatewaySettings
    ip_check_urls: tuple[str, ...]
    providers: tuple[ProviderConfig, ...]
    targets: dict[str, TargetConfig]


def _positive_int(value: Any, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if parsed < 1:
        raise ConfigError(f"{name} must be at least 1")
    return parsed


def _port(value: Any, name: str) -> int:
    parsed = _positive_int(value, name)
    if parsed > 65535:
        raise ConfigError(f"{name} must be at most 65535")
    return parsed


def _names(value: Any, name: str) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        raise ConfigError(f"{name} must be a list or comma-separated string")
    names = tuple(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))
    if not names:
        raise ConfigError(f"{name} cannot be empty when configured")
    return names


def _target(name: str, raw: Any) -> TargetConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"target {name!r} must be a mapping")
    url = str(raw.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ConfigError(f"target {name!r} has an invalid url")
    method = str(raw.get("method") or "GET").upper()
    if method not in {"GET", "POST", "HEAD"}:
        raise ConfigError(f"target {name!r} has unsupported method {method}")
    checks: list[JsonCheck] = []
    for item in raw.get("json_checks") or []:
        if not isinstance(item, dict) or not str(item.get("path") or "").strip():
            raise ConfigError(f"target {name!r} has an invalid json check")
        checks.append(
            JsonCheck(
                path=str(item["path"]),
                present=item.get("present") if "present" in item else None,
                equals=item.get("equals"),
                has_equals="equals" in item,
            )
        )
    statuses = tuple(int(value) for value in (raw.get("expected_statuses") or [200]))
    return TargetConfig(
        name=name,
        method=method,
        url=url,
        headers={str(k): str(v) for k, v in (raw.get("headers") or {}).items()},
        form=raw.get("form") if "form" in raw else None,
        json_body=raw.get("json") if "json" in raw else None,
        expected_statuses=statuses,
        json_checks=tuple(checks),
    )


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration does not exist: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("configuration root must be a mapping")

    gateway_raw = raw.get("gateway") or {}
    if not isinstance(gateway_raw, dict):
        raise ConfigError("gateway must be a mapping")
    direct_dns_raw = gateway_raw.get(
        "direct_dns_servers",
        ("1.1.1.1", "8.8.8.8"),
    )
    if not isinstance(direct_dns_raw, (list, tuple)):
        raise ConfigError("direct_dns_servers must be a list")
    work_lanes = _positive_int(gateway_raw.get("work_lanes", 3), "work_lanes")
    max_work_lanes = _positive_int(
        gateway_raw.get("max_work_lanes", 6),
        "max_work_lanes",
    )
    maintenance_targets = _names(
        gateway_raw.get("maintenance_targets"),
        "maintenance_targets",
    )
    gateway = GatewaySettings(
        backend=str(gateway_raw.get("backend") or "xray").strip().lower(),
        include_direct=bool(gateway_raw.get("include_direct", True)),
        direct_dns_servers=tuple(
            str(value).strip()
            for value in direct_dns_raw
            if str(value).strip()
        ),
        work_lanes=work_lanes,
        max_work_lanes=max_work_lanes,
        work_port_base=_port(gateway_raw.get("work_port_base", 17891), "work_port_base"),
        probe_lanes=_positive_int(gateway_raw.get("probe_lanes", 2), "probe_lanes"),
        probe_port_base=_port(gateway_raw.get("probe_port_base", 17991), "probe_port_base"),
        listen=str(gateway_raw.get("listen") or "127.0.0.1"),
        controller=str(gateway_raw.get("controller") or "127.0.0.1:19090"),
        settle_seconds=float(gateway_raw.get("settle_seconds", 1.5)),
        probe_timeout_seconds=float(gateway_raw.get("probe_timeout_seconds", 20)),
        target_probe_attempts=_positive_int(
            gateway_raw.get("target_probe_attempts", 2),
            "target_probe_attempts",
        ),
        target_probe_retry_seconds=float(
            gateway_raw.get("target_probe_retry_seconds", 1.0)
        ),
        inventory_concurrency=_positive_int(
            gateway_raw.get("inventory_concurrency", gateway_raw.get("probe_lanes", 2)),
            "inventory_concurrency",
        ),
        healthy_max_age_hours=float(gateway_raw.get("healthy_max_age_hours", 24)),
        failure_threshold=_positive_int(gateway_raw.get("failure_threshold", 3), "failure_threshold"),
        monitor_interval_seconds=float(gateway_raw.get("monitor_interval_seconds", 30)),
        direct_outbound_interface=str(
            gateway_raw.get(
                "direct_outbound_interface",
                gateway_raw.get("outbound_interface", ""),
            )
            or ""
        ).strip(),
        node_outbound_interface=str(
            gateway_raw.get("node_outbound_interface") or ""
        ).strip(),
        reinventory_cooldown_seconds=float(
            gateway_raw.get("reinventory_cooldown_seconds", 300)
        ),
        reserve_refresh_interval_seconds=float(
            gateway_raw.get("reserve_refresh_interval_seconds", 3600)
        ),
        maintenance_error_retry_seconds=float(
            gateway_raw.get("maintenance_error_retry_seconds", 60)
        ),
        probe_history_retention_days=float(
            gateway_raw.get("probe_history_retention_days", 90)
        ),
        maintenance_targets=maintenance_targets,
        fault_file=str(gateway_raw.get("fault_file") or "~/Desktop/爬虫故障.txt").strip(),
    )
    if gateway.backend not in {"xray", "mihomo"}:
        raise ConfigError("backend must be xray or mihomo")
    if gateway.include_direct and not gateway.direct_dns_servers:
        raise ConfigError("direct_dns_servers cannot be empty when include_direct is true")
    if gateway.work_lanes > gateway.max_work_lanes:
        raise ConfigError("work_lanes cannot exceed max_work_lanes")
    if gateway.work_port_base + gateway.work_lanes - 1 > 65535:
        raise ConfigError("work lane port range exceeds 65535")
    if gateway.probe_port_base + gateway.probe_lanes - 1 > 65535:
        raise ConfigError("probe lane port range exceeds 65535")
    if gateway.inventory_concurrency > gateway.probe_lanes:
        raise ConfigError("inventory_concurrency cannot exceed probe_lanes")
    if gateway.reserve_refresh_interval_seconds <= 0:
        raise ConfigError("reserve_refresh_interval_seconds must be greater than 0")
    if gateway.maintenance_error_retry_seconds <= 0:
        raise ConfigError("maintenance_error_retry_seconds must be greater than 0")
    if gateway.probe_history_retention_days <= 0:
        raise ConfigError("probe_history_retention_days must be greater than 0")

    providers: list[ProviderConfig] = []
    names: set[str] = set()
    for item in raw.get("providers") or []:
        if not isinstance(item, dict):
            raise ConfigError("each provider must be a mapping")
        name = str(item.get("name") or "").strip()
        provider_type = str(item.get("type") or "http").strip().lower()
        if not name or name in names:
            raise ConfigError(f"provider name is empty or duplicated: {name!r}")
        if provider_type not in {"http", "file", "shadowrocket"}:
            raise ConfigError(
                f"provider {name!r} type must be http, file, or shadowrocket"
            )
        url = str(item.get("url") or "").strip()
        provider_path = str(item.get("path") or "").strip()
        provider_scope = str(item.get("scope") or "all").strip().lower()
        if provider_type == "http" and not url.startswith(("http://", "https://")):
            raise ConfigError(f"provider {name!r} requires an HTTP(S) subscription URL")
        if provider_type in {"file", "shadowrocket"} and not provider_path:
            raise ConfigError(f"provider {name!r} requires path")
        if provider_type == "shadowrocket" and provider_scope not in {
            "all",
            "subscription",
            "local",
        }:
            raise ConfigError(
                f"provider {name!r} scope must be all, subscription, or local"
            )
        if provider_type != "shadowrocket" and provider_scope != "all":
            raise ConfigError(
                f"provider {name!r} scope is supported only for shadowrocket providers"
            )
        refresh_remote = bool(item.get("refresh_remote", False))
        if refresh_remote and not (
            provider_type == "shadowrocket" and provider_scope == "subscription"
        ):
            raise ConfigError(
                f"provider {name!r} refresh_remote requires shadowrocket type "
                "with subscription scope"
            )
        providers.append(
            ProviderConfig(
                name=name,
                type=provider_type,
                url=url,
                path=provider_path,
                scope=provider_scope,
                interval_seconds=_positive_int(item.get("interval_seconds", 3600), "interval_seconds"),
                include=str(item.get("include") or ""),
                exclude=str(item.get("exclude") or ""),
                refresh_remote=refresh_remote,
                user_agent=str(
                    item.get("user_agent")
                    or "Shadowrocket/2700 CFNetwork/1568.100.1 Darwin/24.0.0"
                ),
            )
        )
        names.add(name)
    if not providers:
        raise ConfigError("at least one provider is required")

    ip_check_urls = tuple(str(url) for url in (raw.get("ip_check_urls") or []) if str(url).strip())
    if not ip_check_urls:
        raise ConfigError("at least one ip_check_urls entry is required")
    targets_raw = raw.get("targets") or {}
    if not isinstance(targets_raw, dict) or not targets_raw:
        raise ConfigError("at least one target is required")
    targets = {str(name): _target(str(name), value) for name, value in targets_raw.items()}
    unknown_maintenance_targets = set(gateway.maintenance_targets) - set(targets)
    if unknown_maintenance_targets:
        unknown = ", ".join(sorted(unknown_maintenance_targets))
        raise ConfigError(f"maintenance_targets contains unknown targets: {unknown}")
    return AppConfig(
        path=config_path,
        gateway=gateway,
        ip_check_urls=ip_check_urls,
        providers=tuple(providers),
        targets=targets,
    )
