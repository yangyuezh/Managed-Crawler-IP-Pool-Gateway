from __future__ import annotations

import concurrent.futures
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import replace
from typing import Any, Callable

import yaml

from .config import AppConfig
from .mihomo import (
    MihomoError,
    ProxyNode,
    probe_group,
    render_config,
    work_group,
)
from .paths import ProjectPaths
from .probe import probe_existing_lane, probe_selected_node, utc_now
from .state import Candidate, LaneLease, ProbeResult, StateStore
from .subscriptions import materialize_provider


EventSink = Callable[[dict[str, Any]], None]


def _provider_config_fingerprints(
    paths: ProjectPaths,
    config: AppConfig,
) -> dict[tuple[str, str], str]:
    fingerprints: dict[tuple[str, str], str] = {}
    for provider in config.providers:
        filename = provider.name.replace("/", "_") + ".yaml"
        path = paths.runtime_dir / "providers" / filename
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        proxies = payload.get("proxies") if isinstance(payload, dict) else None
        if not isinstance(proxies, list):
            continue
        for proxy in proxies:
            if not isinstance(proxy, dict):
                continue
            original_name = str(proxy.get("name") or "").strip()
            if not original_name:
                continue
            stable_config = {key: value for key, value in proxy.items() if key != "name"}
            canonical = json.dumps(
                stable_config,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            displayed_name = f"[{provider.name}] {original_name}"
            fingerprints[(provider.name, displayed_name)] = hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest()
    return fingerprints


def maintenance_target_names(
    config: AppConfig,
    requested: list[str] | tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    names = tuple(dict.fromkeys(requested or config.gateway.maintenance_targets or config.targets))
    unknown = set(names) - set(config.targets)
    if unknown:
        raise ValueError(f"unknown targets: {', '.join(sorted(unknown))}")
    if not names:
        raise ValueError("at least one target is required")
    return names


def runtime_parameters(
    config: AppConfig,
    requested_targets: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    targets = maintenance_target_names(config, requested_targets)
    return {
        "config": str(config.path),
        "backend": config.gateway.backend,
        "listen": config.gateway.listen,
        "controller": config.gateway.controller,
        "work_lanes": config.gateway.work_lanes,
        "max_work_lanes": config.gateway.max_work_lanes,
        "work_ports": [
            config.gateway.work_port_base,
            config.gateway.work_port_base + config.gateway.work_lanes - 1,
        ],
        "probe_lanes": config.gateway.probe_lanes,
        "probe_ports": [
            config.gateway.probe_port_base,
            config.gateway.probe_port_base + config.gateway.probe_lanes - 1,
        ],
        "inventory_concurrency": config.gateway.inventory_concurrency,
        "probe_timeout_seconds": config.gateway.probe_timeout_seconds,
        "target_probe_attempts": config.gateway.target_probe_attempts,
        "target_probe_retry_seconds": config.gateway.target_probe_retry_seconds,
        "healthy_max_age_hours": config.gateway.healthy_max_age_hours,
        "failure_threshold": config.gateway.failure_threshold,
        "monitor_interval_seconds": config.gateway.monitor_interval_seconds,
        "reserve_refresh_interval_seconds": config.gateway.reserve_refresh_interval_seconds,
        "maintenance_error_retry_seconds": config.gateway.maintenance_error_retry_seconds,
        "probe_history_retention_days": config.gateway.probe_history_retention_days,
        "direct_outbound_interface": config.gateway.direct_outbound_interface or "system-default",
        "node_outbound_interface": config.gateway.node_outbound_interface or "system-default",
        "routing_priority": ["subscription", "local", "direct"],
        "providers": [
            {
                "name": provider.name,
                "type": provider.type,
                "scope": provider.scope,
                "interval_seconds": provider.interval_seconds,
                "refresh_remote": provider.refresh_remote,
                "include_filter": bool(provider.include),
                "exclude_filter": bool(provider.exclude),
            }
            for provider in config.providers
        ],
        "maintenance_targets": list(targets),
        "targets": [
            {
                "name": name,
                "method": config.targets[name].method,
                "url": config.targets[name].url,
                "expected_statuses": list(config.targets[name].expected_statuses),
                "header_names": sorted(config.targets[name].headers),
                "form_fields": sorted((config.targets[name].form or {}).keys()),
                "json_fields": sorted((config.targets[name].json_body or {}).keys()),
                "json_checks": [check.path for check in config.targets[name].json_checks],
            }
            for name in targets
        ],
        "redacted": "provider URLs, provider paths, header values and request values are not printed",
    }


def print_event(event: dict[str, Any]) -> None:
    print(json.dumps(event, ensure_ascii=False, sort_keys=True), flush=True)


def print_human_event(event: dict[str, Any]) -> None:
    kind = str(event.get("event") or "event")
    if kind == "provider_refresh_started":
        message = f"正在刷新订阅：{event.get('provider')}"
    elif kind == "provider_refresh_finished":
        message = f"订阅刷新完成：{event.get('provider')}，节点 {event.get('nodes')} 个"
    elif kind == "provider_refresh_partial":
        message = (
            f"订阅部分更新：{event.get('provider')}，节点 {event.get('nodes')} 个；"
            f"新鲜来源 {event.get('sources_fresh')}，缓存来源 "
            f"{event.get('sources_cached')}，失败来源 {event.get('sources_failed')}"
        )
    elif kind == "provider_refresh_failed_using_cache":
        message = (
            f"订阅刷新失败，继续使用本地缓存：{event.get('provider')}，"
            f"节点 {event.get('nodes')} 个"
        )
    elif kind == "provider_refresh_failed":
        message = (
            f"订阅刷新失败且没有缓存：{event.get('provider')}，"
            f"{event.get('error_type')}"
        )
    elif kind == "nodes_discovered":
        message = f"共载入 {event.get('nodes')} 个节点，来自 {event.get('providers')} 组订阅"
    elif kind == "node_probe_finished":
        target_label = str(event.get("target") or "目标")
        if event.get("target_healthy") and not event.get("egress_healthy"):
            outcome = f"{target_label}有效，但出口IP未识别"
        elif event.get("egress_error_type") == "EgressCheckSkipped":
            target_result = event.get("target_status_code") or event.get("target_error_type")
            outcome = f"{target_label}失败({target_result})，未继续检测公网IP"
        elif not event.get("egress_healthy"):
            target_result = event.get("target_status_code") or event.get("target_error_type")
            outcome = (
                f"公网IP未识别({event.get('egress_error_type')})，"
                f"{target_label} {target_result}"
            )
        elif event.get("target_healthy"):
            outcome = f"{target_label}有效"
        else:
            outcome = (
                f"公网IP已识别，{target_label}失败"
                f"({event.get('status_code') or event.get('error_type')})"
            )
        message = (
            f"检测 {event.get('done')}/{event.get('total')} | 公网IP已识别 {event.get('egress_healthy_count')} | "
            f"已知独立IP {event.get('distinct_egress_ips')} | 目标有效 {event.get('target_healthy_count')} | "
            f"{outcome} | {event.get('proxy_name')}"
        )
    elif kind == "inventory_complete":
        message = (
            f"目标 {event.get('target')} 体检完成：节点 {event.get('tested')}，"
            f"公网IP已识别 {event.get('egress_healthy')}，"
            f"已知独立IP {event.get('distinct_egress_ips')}，目标已测 {event.get('target_tested')}，"
            f"目标有效200 {event.get('target_healthy')}"
        )
    elif kind == "assignment_complete":
        message = (
            f"工作通道分配：{event.get('assigned_lanes')}/{event.get('requested_lanes')}，"
            f"已知独立IP {event.get('distinct_egress_ips')}"
        )
    elif kind == "runtime_parameters":
        message = "运行参数（敏感值已隐藏）：\n" + json.dumps(
            event.get("parameters") or {},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    elif kind == "reserve_refresh_complete":
        message = (
            f"备用池更新完成：总节点 {event.get('reserve_total')}，"
            f"订阅 {event.get('subscription_nodes')}，本地 {event.get('local_nodes')}，"
            f"直连 {event.get('direct_nodes')}"
        )
    elif kind == "reserve_probe_target_complete":
        pool = event.get("pool") or {}
        message = (
            f"目标 {event.get('target')} 检测完成：备用池 {pool.get('reserve_total')}，"
            f"通过 {pool.get('reserve_qualified')}，失败 {pool.get('reserve_rejected')}，"
            f"未检测 {pool.get('reserve_untested')}，过期 {pool.get('reserve_stale')}，"
            f"主池 {pool.get('primary_active')}/{pool.get('primary_assigned')}"
        )
    elif kind == "reserve_maintenance_cycle_complete":
        message = (
            f"备用池维护第 {event.get('cycle')} 轮完成，耗时 "
            f"{event.get('elapsed_seconds')} 秒，下轮等待 "
            f"{event.get('next_cycle_seconds')} 秒。事实结果：\n"
            + json.dumps(event.get("pools") or {}, ensure_ascii=False, indent=2, sort_keys=True)
        )
    elif kind == "gateway_recovered":
        message = (
            f"独立网关已自动恢复：后端 {event.get('backend')}，"
            f"PID {event.get('pid')}"
        )
    elif kind == "gateway_health_failed":
        message = (
            f"独立网关健康检查失败：{event.get('error_type')}，"
            "正在自动重建。"
        )
    elif kind == "lane_failover_complete":
        message = (
            f"通道 {event.get('lane')} 已切换出口："
            f"{event.get('old_egress_ip')} -> {event.get('new_egress_ip')}"
        )
    elif kind == "target_outage_suspected":
        message = (
            f"全部通道同时返回 {event.get('signature')}，暂不批量换节点；"
            "健康监控会继续。"
        )
    elif kind == "target_outage_canary_failover":
        message = (
            f"共同异常已连续达到阈值，仅切换通道 {event.get('lane')} "
            "作为探路通道，其他通道保持不动。"
        )
    elif kind == "lane_failover_exhausted":
        message = (
            f"通道 {event.get('lane')} 暂无可用替代节点，保留为降级状态；"
            "其他通道继续运行。"
        )
    elif kind == "monitor_cycle_failed":
        message = (
            f"本轮通道检查异常：{event.get('error_type')}，"
            "监控未退出，下轮会继续。"
        )
    elif kind in {"node_probe_started", "lane_candidate_verified", "existing_lane_kept"}:
        return
    else:
        message = json.dumps(event, ensure_ascii=False, sort_keys=True)
    print(message, flush=True)


class GatewayController:
    def __init__(
        self,
        config: AppConfig,
        paths: ProjectPaths,
        process: Any,
        state: StateStore,
        event_sink: EventSink = print_event,
    ) -> None:
        self.config = config
        self.paths = paths
        self.process = process
        self.state = state
        self.event = event_sink
        self._last_reinventory_at = 0.0

    def ensure_gateway(self) -> dict[str, Any]:
        was_running = bool(self.process.running())
        if was_running:
            try:
                version = self.process.api().version()
                return {
                    "running": True,
                    "recovered": False,
                    "pid": self.process.pid(),
                    "version": version,
                }
            except Exception as exc:  # noqa: BLE001
                self.event(
                    {
                        "event": "gateway_health_failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
                self.process.stop()

        if self.config.gateway.backend == "xray":
            self.process.prepare(refresh=False)
        else:
            secret = getattr(self.process, "secret", None)
            if not isinstance(secret, str) or not secret:
                raise MihomoError("Mihomo controller secret is unavailable")
            render_config(
                self.config,
                self.paths,
                secret,
                refresh_providers=False,
            )
        pid = self.process.start()
        api = self.process.api()
        version = api.version()
        restored_leases = 0
        degraded_leases = 0
        for lease in self.state.leases():
            try:
                api.select(lease.group_name, lease.proxy_name)
                selected = api.group(lease.group_name).get("now")
                if selected != lease.proxy_name:
                    raise RuntimeError(
                        f"group selected {selected!r}, expected {lease.proxy_name!r}"
                    )
                restored_leases += 1
                self.event(
                    {
                        "event": "gateway_lease_restored",
                        "lane": lease.lane,
                        "target": lease.target,
                        "proxy_name": lease.proxy_name,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                degraded_leases += 1
                self.state.record_lane_check(lease.lane, False, utc_now())
                self.event(
                    {
                        "event": "gateway_lease_restore_failed",
                        "lane": lease.lane,
                        "target": lease.target,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
        result = {
            "running": True,
            "recovered": True,
            "was_running": was_running,
            "backend": self.config.gateway.backend,
            "pid": pid,
            "version": version,
            "restored_leases": restored_leases,
            "degraded_leases": degraded_leases,
        }
        self.event({"event": "gateway_recovered", **result})
        return result

    @staticmethod
    def _target_level_failure(result: ProbeResult) -> bool:
        if result.healthy:
            return False
        if result.status_code is not None:
            return True
        return result.error_type in {"InvalidJson", "JsonCheckFailed"}

    @staticmethod
    def _target_probe_result(
        *,
        node_key: str,
        provider: str,
        proxy_name: str,
        target: str,
        egress_ip: str | None,
        response: Any | None,
        error: Exception | None = None,
    ) -> ProbeResult:
        return ProbeResult(
            node_key=node_key,
            provider=provider,
            proxy_name=proxy_name,
            target=target,
            checked_at=utc_now(),
            healthy=bool(response and response.healthy),
            egress_ip=egress_ip,
            latency_ms=response.latency_ms if response is not None else None,
            status_code=response.status_code if response is not None else None,
            error_type=(
                response.error_type
                if response is not None
                else type(error).__name__ if error is not None else "UnknownError"
            ),
            error=(
                response.error
                if response is not None
                else str(error) if error is not None else "target probe did not return a response"
            ),
            detail=response.detail if response is not None else {},
        )

    def _probe_lane_lease(self, lease: LaneLease) -> ProbeResult:
        egress_ip: str | None = None
        try:
            selected = self.process.api().group(lease.group_name).get("now")
            if selected != lease.proxy_name:
                raise RuntimeError(
                    f"lane binding mismatch: selected {selected!r}, "
                    f"expected {lease.proxy_name!r}"
                )
            egress_ip, response, _ = probe_existing_lane(
                config=self.config,
                port=lease.port,
                target_name=lease.target,
            )
            return self._target_probe_result(
                node_key=lease.node_key,
                provider=lease.provider,
                proxy_name=lease.proxy_name,
                target=lease.target,
                egress_ip=egress_ip,
                response=response,
            )
        except Exception as exc:  # noqa: BLE001
            return self._target_probe_result(
                node_key=lease.node_key,
                provider=lease.provider,
                proxy_name=lease.proxy_name,
                target=lease.target,
                egress_ip=egress_ip,
                response=None,
                error=exc,
            )

    @classmethod
    def _shared_target_failure_signature(
        cls,
        results: list[ProbeResult],
    ) -> str | None:
        if len(results) < 2:
            return None
        signatures: list[str] = []
        distinct_egress_ips: set[str] = set()
        for result in results:
            if not cls._target_level_failure(result):
                return None
            if result.egress_ip:
                distinct_egress_ips.add(result.egress_ip)
            if result.status_code is not None:
                signatures.append(f"HTTP {result.status_code}")
            else:
                signatures.append(str(result.error_type or "invalid target response"))
        if len(distinct_egress_ips) < 2:
            return None
        return signatures[0] if len(set(signatures)) == 1 else None

    def require_api(self) -> Any:
        if not self.process.running():
            raise MihomoError("gateway is stopped; run crawler-gateway start first")
        api = self.process.api()
        api.version()
        return api

    def discover(self, refresh: bool = False) -> list[ProxyNode]:
        api = self.require_api()
        if refresh:
            for provider in self.config.providers:
                self.event({"event": "provider_refresh_started", "provider": provider.name})
                self.state.record_provider_refresh(provider.name, "running")
                try:
                    refresh_status = "success"
                    refresh_detail: dict[str, Any] = {}
                    if self.config.gateway.backend == "xray":
                        count = self.process.refresh_provider(provider.name)
                    else:
                        filename = provider.name.replace("/", "_") + ".yaml"
                        destination = self.paths.runtime_dir / "providers" / filename
                        materialized = materialize_provider(
                            provider,
                            self.config,
                            destination,
                        )
                        count = materialized.node_count
                        refresh_status = materialized.status
                        refresh_detail = {
                            "sources_total": materialized.sources_total,
                            "sources_fresh": materialized.sources_fresh,
                            "sources_cached": materialized.sources_cached,
                            "sources_failed": materialized.sources_failed,
                            "error_types": list(materialized.error_types),
                        }
                        api.refresh_provider(provider.name)
                except Exception as exc:  # noqa: BLE001
                    try:
                        if self.config.gateway.backend == "xray":
                            count = self.process.provider_count(provider)
                        else:
                            destination = self.paths.runtime_dir / "providers" / (
                                provider.name.replace("/", "_") + ".yaml"
                            )
                            if not destination.is_file():
                                raise FileNotFoundError(destination)
                            count = sum(
                                node.provider == provider.name
                                for node in api.discover_nodes((provider,))
                            )
                    except Exception as cache_exc:  # noqa: BLE001
                        self.state.record_provider_refresh(
                            provider.name,
                            "failed",
                            error_type=type(exc).__name__,
                            error=str(exc),
                        )
                        self.event(
                            {
                                "event": "provider_refresh_failed",
                                "provider": provider.name,
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                                "cache_error": str(cache_exc),
                            }
                        )
                        raise exc
                    self.state.record_provider_refresh(
                        provider.name,
                        "cache",
                        node_count=count,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    self.event(
                        {
                            "event": "provider_refresh_failed_using_cache",
                            "provider": provider.name,
                            "nodes": count,
                            "error_type": type(exc).__name__,
                        }
                    )
                    continue
                self.state.record_provider_refresh(
                    provider.name,
                    refresh_status,
                    node_count=count,
                    error_type=(
                        ",".join(refresh_detail.get("error_types") or ()) or None
                    ),
                    detail=refresh_detail,
                )
                if refresh_status == "success":
                    event_kind = "provider_refresh_finished"
                elif refresh_status == "partial":
                    event_kind = "provider_refresh_partial"
                else:
                    event_kind = "provider_refresh_failed_using_cache"
                self.event(
                    {
                        "event": event_kind,
                        "provider": provider.name,
                        "nodes": count,
                        **refresh_detail,
                    }
                )
        nodes = api.discover_nodes(self.config.providers)
        if self.config.gateway.backend == "mihomo":
            fingerprints = _provider_config_fingerprints(self.paths, self.config)
            nodes = [
                replace(
                    node,
                    metadata={
                        **node.metadata,
                        "config_fingerprint": fingerprints[(node.provider, node.name)],
                    },
                )
                if (node.provider, node.name) in fingerprints
                else node
                for node in nodes
            ]
        self.state.sync_nodes(nodes)
        self.event(
            {
                "event": "nodes_discovered",
                "nodes": len(nodes),
                "providers": len(self.config.providers),
            }
        )
        return nodes

    def resolve_targets(
        self,
        requested: list[str] | tuple[str, ...] | None = None,
    ) -> tuple[str, ...]:
        return maintenance_target_names(self.config, requested)

    def pool_report(
        self,
        requested: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        targets = self.resolve_targets(requested)
        return {
            "reserve_model": {
                "discovered": "all current provider nodes",
                "qualified": "fresh target-healthy subset",
                "primary": "fixed work-lane leases selected from the qualified subset",
            },
            "providers": self.state.provider_refresh_states(),
            "targets": {
                target: self.state.pool_snapshot(
                    target,
                    self.config.gateway.healthy_max_age_hours,
                )
                for target in targets
            },
        }

    def refresh_reserve(self) -> dict[str, Any]:
        nodes = self.discover(refresh=True)
        counts = self.state.counts(self.config.gateway.healthy_max_age_hours)
        summary = {
            "reserve_total": len(nodes),
            "subscription_nodes": counts["subscription_nodes"],
            "local_nodes": counts["local_proxy_nodes"],
            "direct_nodes": counts["direct_candidates"],
            "providers": self.state.provider_refresh_states(),
        }
        self.event({"event": "reserve_refresh_complete", **summary})
        return summary

    def probe_reserve(
        self,
        requested: list[str] | tuple[str, ...] | None = None,
        *,
        limit: int | None = None,
        concurrency: int | None = None,
    ) -> dict[str, Any]:
        targets = self.resolve_targets(requested)
        summaries: dict[str, Any] = {}
        for target in targets:
            try:
                inventory = self.inventory(
                    target,
                    refresh=False,
                    limit=limit,
                    concurrency=concurrency,
                )
                error = None
            except Exception as exc:  # noqa: BLE001
                inventory = None
                error = {
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                self.event(
                    {
                        "event": "reserve_probe_target_failed",
                        "target": target,
                        **error,
                    }
                )
            pool = self.state.pool_snapshot(
                target,
                self.config.gateway.healthy_max_age_hours,
            )
            summaries[target] = {
                "inventory": inventory,
                "pool": pool,
                "error": error,
            }
            self.event(
                {
                    "event": "reserve_probe_target_complete",
                    "target": target,
                    "inventory": inventory,
                    "pool": pool,
                    "error": error,
                }
            )
        return {
            "targets": list(targets),
            "results": summaries,
        }

    def maintain_reserve(
        self,
        requested: list[str] | tuple[str, ...] | None = None,
        *,
        once: bool = False,
        interval_seconds: float | None = None,
        limit: int | None = None,
        concurrency: int | None = None,
    ) -> dict[str, Any]:
        targets = self.resolve_targets(requested)
        interval = (
            self.config.gateway.reserve_refresh_interval_seconds
            if interval_seconds is None
            else float(interval_seconds)
        )
        if interval <= 0:
            raise ValueError("maintenance interval must be greater than 0")
        self.event(
            {
                "event": "runtime_parameters",
                "parameters": runtime_parameters(self.config, targets),
            }
        )
        stop = threading.Event()

        def request_stop(signum: int, _frame: Any) -> None:
            self.event({"event": "reserve_maintenance_stop_requested", "signal": signum})
            stop.set()

        lock_path = self.paths.runtime_dir / "reserve_maintenance.lock"
        pid_path = self.paths.runtime_dir / "reserve_maintenance.pid"
        lock_path.touch(mode=0o600, exist_ok=True)
        lock_handle = lock_path.open("r+")
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_handle.close()
            raise MihomoError("another reserve maintenance process is already running") from exc
        pid_path.write_text(f"{os.getpid()}\n", encoding="ascii")
        pid_path.chmod(0o600)
        cycle = 0
        last_result: dict[str, Any] = {}
        previous_handlers: dict[int, Any] = {}
        try:
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGINT, signal.SIGTERM):
                    previous_handlers[signum] = signal.getsignal(signum)
                    signal.signal(signum, request_stop)
            while not stop.is_set():
                cycle += 1
                started = time.monotonic()
                self.event(
                    {
                        "event": "reserve_maintenance_cycle_started",
                        "cycle": cycle,
                        "targets": list(targets),
                        "interval_seconds": interval,
                    }
                )
                try:
                    gateway = self.ensure_gateway()
                    gateway_error = None
                except Exception as exc:  # noqa: BLE001
                    gateway = None
                    gateway_error = {
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                    self.event(
                        {
                            "event": "reserve_maintenance_gateway_failed",
                            **gateway_error,
                        }
                    )
                if gateway_error is None:
                    try:
                        refresh = self.refresh_reserve()
                        refresh_error = None
                    except Exception as exc:  # noqa: BLE001
                        refresh = None
                        refresh_error = {
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        }
                    probes = self.probe_reserve(
                        targets,
                        limit=limit,
                        concurrency=concurrency,
                    )
                else:
                    refresh = None
                    refresh_error = None
                    probes = {
                        "targets": list(targets),
                        "results": {
                            target: {
                                "inventory": None,
                                "pool": self.state.pool_snapshot(
                                    target,
                                    self.config.gateway.healthy_max_age_hours,
                                ),
                                "error": gateway_error,
                            }
                            for target in targets
                        },
                    }
                pools = self.pool_report(targets)
                pruned_probe_results = self.state.prune_probe_history(
                    self.config.gateway.probe_history_retention_days
                )
                probe_errors = any(
                    item.get("error")
                    for item in (probes.get("results", {}) or {}).values()
                )
                has_errors = bool(gateway_error or refresh_error or probe_errors)
                next_wait = min(
                    interval,
                    self.config.gateway.maintenance_error_retry_seconds,
                ) if has_errors else interval
                last_result = {
                    "cycle": cycle,
                    "gateway": gateway,
                    "gateway_error": gateway_error,
                    "refresh": refresh,
                    "refresh_error": refresh_error,
                    "probes": probes,
                    "pools": pools["targets"],
                    "pruned_probe_results": pruned_probe_results,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "next_cycle_seconds": None if once else next_wait,
                }
                self.event({"event": "reserve_maintenance_cycle_complete", **last_result})
                if once:
                    return last_result
                stop.wait(next_wait)
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
            try:
                pid_path.unlink()
            except FileNotFoundError:
                pass
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()
        return last_result

    def reserve_maintenance_status(self) -> dict[str, Any]:
        pid_path = self.paths.runtime_dir / "reserve_maintenance.pid"
        try:
            pid = int(pid_path.read_text(encoding="ascii").strip())
        except (FileNotFoundError, ValueError):
            return {"running": False, "pid": None}
        command = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        ).stdout
        if "crawler_gateway" not in command or "maintain-reserve" not in command:
            try:
                pid_path.unlink()
            except FileNotFoundError:
                pass
            return {"running": False, "pid": None}
        return {"running": True, "pid": pid}

    def stop_reserve_maintenance(self, wait_seconds: float = 10.0) -> int | None:
        status = self.reserve_maintenance_status()
        if not status["running"]:
            return None
        pid = int(status["pid"])
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return None
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        return pid

    def inventory(
        self,
        target: str,
        *,
        refresh: bool = False,
        limit: int | None = None,
        concurrency: int | None = None,
    ) -> dict[str, Any]:
        if target not in self.config.targets:
            raise ValueError(f"unknown target: {target}")
        lock_path = self.paths.runtime_dir / "inventory.lock"
        lock_path.touch(mode=0o600, exist_ok=True)
        lock_handle = lock_path.open("r+")
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_handle.close()
            raise MihomoError("another node inventory is already running") from exc
        try:
            return self._inventory_locked(
                target,
                refresh=refresh,
                limit=limit,
                concurrency=concurrency,
            )
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()

    def _inventory_locked(
        self,
        target: str,
        *,
        refresh: bool,
        limit: int | None,
        concurrency: int | None,
    ) -> dict[str, Any]:
        nodes = self.discover(refresh=refresh)
        if limit is not None:
            nodes = nodes[: max(0, limit)]
        if not nodes:
            return {"target": target, "tested": 0, "healthy": 0, "distinct_egress_ips": 0}
        queue_lock = threading.Lock()
        iterator = iter(nodes)
        results_lock = threading.Lock()
        egress_results: list[ProbeResult] = []
        target_results: list[ProbeResult] = []

        def next_node() -> ProxyNode | None:
            with queue_lock:
                try:
                    return next(iterator)
                except StopIteration:
                    return None

        def lane_worker(probe_lane: int) -> None:
            api = self.process.api()
            group = probe_group(probe_lane)
            port = self.config.gateway.probe_port_base + probe_lane - 1
            while True:
                node = next_node()
                if node is None:
                    return
                self.event(
                    {
                        "event": "node_probe_started",
                        "target": target,
                        "probe_lane": probe_lane,
                        "provider": node.provider,
                        "proxy_name": node.name,
                    }
                )
                outcome = probe_selected_node(
                    api=api,
                    config=self.config,
                    group=group,
                    port=port,
                    node=node,
                    target_name=target,
                )
                self.state.record_probe(outcome.egress)
                if outcome.target is not None:
                    self.state.record_probe(outcome.target)
                with results_lock:
                    egress_results.append(outcome.egress)
                    if outcome.target is not None:
                        target_results.append(outcome.target)
                    done = len(egress_results)
                    egress_healthy = sum(item.healthy for item in egress_results)
                    target_healthy = sum(item.healthy for item in target_results)
                    distinct_ips = len(
                        {item.egress_ip for item in egress_results if item.healthy and item.egress_ip}
                    )
                displayed = outcome.target or outcome.egress
                self.event(
                    {
                        "event": "node_probe_finished",
                        "target": target,
                        "done": done,
                        "total": len(nodes),
                        "egress_healthy_count": egress_healthy,
                        "target_healthy_count": target_healthy,
                        "distinct_egress_ips": distinct_ips,
                        "probe_lane": probe_lane,
                        "provider": node.provider,
                        "proxy_name": node.name,
                        "egress_ip": outcome.egress.egress_ip,
                        "latency_ms": displayed.latency_ms,
                        "status_code": displayed.status_code,
                        "egress_healthy": outcome.egress.healthy,
                        "target_healthy": bool(outcome.target and outcome.target.healthy),
                        "error_type": displayed.error_type,
                        "egress_error_type": outcome.egress.error_type,
                        "target_error_type": outcome.target.error_type if outcome.target else None,
                        "target_status_code": outcome.target.status_code if outcome.target else None,
                    }
                )

        workers = min(
            concurrency or self.config.gateway.inventory_concurrency,
            self.config.gateway.probe_lanes,
            len(nodes),
        )
        if workers < 1:
            raise ValueError("inventory concurrency must be at least 1")
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(lane_worker, index) for index in range(1, workers + 1)]
                for future in concurrent.futures.as_completed(futures):
                    future.result()
        finally:
            for index in range(1, workers + 1):
                group = probe_group(index)
                try:
                    self.process.api().release(group)
                except Exception as exc:  # noqa: BLE001
                    self.event(
                        {
                            "event": "probe_lane_release_failed",
                            "probe_lane": index,
                            "error": str(exc),
                        }
                    )

        summary = {
            "target": target,
            "tested": len(egress_results),
            "egress_healthy": sum(item.healthy for item in egress_results),
            "distinct_egress_ips": len(
                {item.egress_ip for item in egress_results if item.healthy and item.egress_ip}
            ),
            "target_tested": len(target_results),
            "target_http_200": sum(item.status_code == 200 for item in target_results),
            "target_healthy": sum(item.healthy for item in target_results),
            "target_statuses": {
                str(status): sum(item.status_code == status for item in target_results)
                for status in sorted({item.status_code for item in target_results}, key=lambda value: (-1 if value is None else value))
            },
            "healthy": sum(item.healthy for item in target_results),
            "failed": len(egress_results) - sum(item.healthy for item in target_results),
        }
        self.event({"event": "inventory_complete", **summary})
        return summary

    def _verify_candidate_on_work_lane(
        self,
        api: Any,
        lane: int,
        target: str,
        candidate: Candidate,
    ) -> bool:
        group = work_group(lane)
        port = self.config.gateway.work_port_base + lane - 1
        egress_ip: str | None = None
        response: Any | None = None
        error: Exception | None = None
        try:
            api.select(group, candidate.proxy_name)
            time.sleep(self.config.gateway.settle_seconds)
            selected = api.group(group).get("now")
            if selected != candidate.proxy_name:
                raise RuntimeError(
                    f"group selected {selected!r}, expected {candidate.proxy_name!r}"
                )
            egress_ip, response, _ = probe_existing_lane(
                config=self.config,
                port=port,
                target_name=target,
            )
        except Exception as exc:  # noqa: BLE001
            error = exc
        result = self._target_probe_result(
            node_key=candidate.node_key,
            provider=candidate.provider,
            proxy_name=candidate.proxy_name,
            target=target,
            egress_ip=egress_ip,
            response=response,
            error=error,
        )
        self.state.record_probe(result)
        self.event(
            {
                "event": "lane_candidate_verified",
                "lane": lane,
                "target": target,
                "proxy_name": candidate.proxy_name,
                "expected_egress_ip": candidate.egress_ip,
                "observed_egress_ip": egress_ip,
                "status_code": result.status_code,
                "healthy": result.healthy,
                "error_type": result.error_type,
            }
        )
        return result.healthy

    def assign(self, target: str, lanes: int | None = None, replace: bool = False) -> list[LaneLease]:
        if target not in self.config.targets:
            raise ValueError(f"unknown target: {target}")
        requested = lanes if lanes is not None else self.config.gateway.work_lanes
        if requested < 1 or requested > self.config.gateway.work_lanes:
            raise ValueError(f"lanes must be between 1 and {self.config.gateway.work_lanes}")
        api = self.require_api()
        existing_by_lane = {lease.lane: lease for lease in self.state.leases(target)}
        kept: dict[int, LaneLease] = {}
        used_ips: set[str] = set()
        used_nodes: set[str] = set()

        if not replace:
            for lane in range(1, requested + 1):
                lease = existing_by_lane.get(lane)
                if lease is None:
                    continue
                try:
                    api.select(work_group(lane), lease.proxy_name)
                    time.sleep(self.config.gateway.settle_seconds)
                    selected = api.group(work_group(lane)).get("now")
                    if selected != lease.proxy_name:
                        raise RuntimeError(
                            f"group selected {selected!r}, expected {lease.proxy_name!r}"
                        )
                    probe_result = self._probe_lane_lease(lease)
                except Exception as exc:  # noqa: BLE001
                    probe_result = self._target_probe_result(
                        node_key=lease.node_key,
                        provider=lease.provider,
                        proxy_name=lease.proxy_name,
                        target=target,
                        egress_ip=None,
                        response=None,
                        error=exc,
                    )
                self.state.record_probe(probe_result)
                failures = self.state.record_lane_check(
                    lane,
                    probe_result.healthy,
                    probe_result.checked_at,
                )
                if not probe_result.healthy:
                    self.event(
                        {
                            "event": "existing_lane_invalid",
                            "lane": lane,
                            "status_code": probe_result.status_code,
                            "error_type": probe_result.error_type,
                            "error": probe_result.error,
                            "consecutive_failures": failures,
                        }
                    )
                    continue
                kept[lane] = lease
                if lease.egress_ip:
                    used_ips.add(lease.egress_ip)
                used_nodes.add(lease.node_key)
                self.event({"event": "existing_lane_kept", "lane": lane, "egress_ip": lease.egress_ip})
        else:
            used_nodes.update(lease.node_key for lease in existing_by_lane.values())
            used_ips.update(
                lease.egress_ip for lease in existing_by_lane.values() if lease.egress_ip
            )

        for lane in range(1, requested + 1):
            if lane in kept:
                continue
            candidates = self.state.qualified_reserve_candidates(
                target,
                self.config.gateway.healthy_max_age_hours,
                occupied_ips=used_ips,
                exclude_nodes=used_nodes,
            )
            assigned = False
            for candidate in candidates:
                used_nodes.add(candidate.node_key)
                try:
                    valid = self._verify_candidate_on_work_lane(api, lane, target, candidate)
                except Exception as exc:  # noqa: BLE001
                    self.event(
                        {
                            "event": "lane_assignment_failed",
                            "lane": lane,
                            "target": target,
                            "proxy_name": candidate.proxy_name,
                            "reason": str(exc),
                        }
                    )
                    valid = False
                if not valid:
                    continue
                port = self.config.gateway.work_port_base + lane - 1
                self.state.replace_lease(lane, work_group(lane), port, target, candidate)
                if candidate.egress_ip:
                    used_ips.add(candidate.egress_ip)
                assigned = True
                break
            if not assigned:
                previous = existing_by_lane.get(lane)
                if previous is not None:
                    try:
                        api.select(previous.group_name, previous.proxy_name)
                        restored = api.group(previous.group_name).get("now") == previous.proxy_name
                    except Exception as exc:  # noqa: BLE001
                        restored = False
                        self.event(
                            {
                                "event": "lane_restore_failed",
                                "lane": lane,
                                "target": target,
                                "error": str(exc),
                            }
                        )
                    self.event(
                        {
                            "event": "lane_replacement_unavailable",
                            "lane": lane,
                            "target": target,
                            "preserved_existing_lease": True,
                            "route_restored": restored,
                        }
                    )
                else:
                    self.state.clear_lease(lane, "no target-verified candidate")
                    try:
                        api.release(work_group(lane))
                    except Exception as exc:  # noqa: BLE001
                        self.event({"event": "work_lane_release_failed", "lane": lane, "error": str(exc)})
                    self.event({"event": "lane_unassigned", "lane": lane, "target": target})

        for lane in range(requested + 1, self.config.gateway.work_lanes + 1):
            self.state.clear_lease(lane, "outside requested lane count")
            try:
                api.release(work_group(lane))
            except Exception as exc:  # noqa: BLE001
                self.event({"event": "work_lane_release_failed", "lane": lane, "error": str(exc)})
        all_leases = [
            lease
            for lease in self.state.leases(target)
            if lease.lane <= requested
        ]
        leases = [lease for lease in all_leases if lease.status == "active"]
        self.event(
            {
                "event": "assignment_complete",
                "target": target,
                "requested_lanes": requested,
                "assigned_lanes": len(leases),
                "degraded_lanes": sum(lease.status == "degraded" for lease in all_leases),
                "distinct_egress_ips": len(
                    {lease.egress_ip for lease in leases if lease.egress_ip}
                ),
            }
        )
        return leases

    def check_lanes(self, target: str, replace_failed: bool = True) -> list[dict[str, Any]]:
        api = self.require_api()
        results: list[dict[str, Any]] = []
        leases = self.state.leases(target)
        if not leases:
            return results
        probe_results: dict[int, ProbeResult] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(leases)) as pool:
            futures = {
                pool.submit(self._probe_lane_lease, lease): lease
                for lease in leases
            }
            for future in concurrent.futures.as_completed(futures):
                lease = futures[future]
                try:
                    probe_results[lease.lane] = future.result()
                except Exception as exc:  # noqa: BLE001
                    probe_results[lease.lane] = self._target_probe_result(
                        node_key=lease.node_key,
                        provider=lease.provider,
                        proxy_name=lease.proxy_name,
                        target=lease.target,
                        egress_ip=None,
                        response=None,
                        error=exc,
                    )

        for lease in leases:
            probe_result = probe_results[lease.lane]
            self.state.record_probe(probe_result)
            failures = self.state.record_lane_check(
                lease.lane,
                probe_result.healthy,
                probe_result.checked_at,
            )
            item = {
                "lane": lease.lane,
                "target": target,
                "healthy": probe_result.healthy,
                "expected_egress_ip": lease.egress_ip,
                "observed_egress_ip": probe_result.egress_ip,
                "consecutive_failures": failures,
                "status_code": probe_result.status_code,
                "error_type": probe_result.error_type,
                "error": probe_result.error,
            }
            results.append(item)
            self.event({"event": "lane_checked", **item})

        shared_failure = self._shared_target_failure_signature(
            [probe_results[lease.lane] for lease in leases]
        )
        if shared_failure is not None:
            eligible = [
                item
                for item in results
                if item["consecutive_failures"]
                >= self.config.gateway.failure_threshold
            ]
            if not eligible:
                self.event(
                    {
                        "event": "target_outage_suspected",
                        "target": target,
                        "lanes": len(leases),
                        "signature": shared_failure,
                    }
                )
                for item in results:
                    item["failover_suppressed"] = "shared_target_failure"
                return results

            canary = min(eligible, key=lambda item: int(item["lane"]))
            canary_lane = int(canary["lane"])
            self.event(
                {
                    "event": "target_outage_canary_failover",
                    "target": target,
                    "lane": canary_lane,
                    "signature": shared_failure,
                }
            )
            failed_lease = next(
                lease for lease in leases if lease.lane == canary_lane
            )
            try:
                replacement = self.replace_lane(api, failed_lease)
                canary["replacement"] = (
                    replacement.proxy_name if replacement else None
                )
            except Exception as exc:  # noqa: BLE001
                canary["failover_error"] = str(exc)
                self.event(
                    {
                        "event": "lane_failover_failed",
                        "lane": canary_lane,
                        "target": target,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
            for item in results:
                if int(item["lane"]) != canary_lane:
                    item["failover_suppressed"] = "shared_target_canary_only"
            return results

        if replace_failed:
            result_by_lane = {int(item["lane"]): item for item in results}
            for lease in leases:
                item = result_by_lane[lease.lane]
                if item["healthy"] or item["consecutive_failures"] < self.config.gateway.failure_threshold:
                    continue
                try:
                    replacement = self.replace_lane(api, lease)
                except Exception as exc:  # noqa: BLE001
                    item["failover_error"] = str(exc)
                    self.event(
                        {
                            "event": "lane_failover_failed",
                            "lane": lease.lane,
                            "target": target,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        }
                    )
                    continue
                item["replacement"] = replacement.proxy_name if replacement else None
        return results

    def replace_lane(self, api: Any, failed: LaneLease) -> LaneLease | None:
        replacement = self._replace_lane_from_candidates(api, failed)
        if replacement is not None:
            return replacement
        now = time.monotonic()
        if now - self._last_reinventory_at >= self.config.gateway.reinventory_cooldown_seconds:
            self._last_reinventory_at = now
            self.event(
                {
                    "event": "failover_reinventory_started",
                    "lane": failed.lane,
                    "target": failed.target,
                }
            )
            try:
                self.inventory(failed.target, refresh=True)
            except Exception as exc:  # noqa: BLE001
                self.event(
                    {
                        "event": "failover_reinventory_failed",
                        "lane": failed.lane,
                        "target": failed.target,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
                return None
            replacement = self._replace_lane_from_candidates(api, failed)
            if replacement is not None:
                return replacement
        self.event(
            {
                "event": "lane_failover_exhausted",
                "lane": failed.lane,
                "target": failed.target,
                "preserved_existing_lease": True,
            }
        )
        return None

    def _replace_lane_from_candidates(self, api: Any, failed: LaneLease) -> LaneLease | None:
        leases = self.state.leases(failed.target)
        excluded_ips = {lease.egress_ip for lease in leases if lease.egress_ip}
        excluded_nodes = {lease.node_key for lease in leases}
        candidates = self.state.qualified_reserve_candidates(
            failed.target,
            self.config.gateway.healthy_max_age_hours,
            occupied_ips=excluded_ips,
            exclude_nodes=excluded_nodes,
        )
        for candidate in candidates:
            try:
                if not self._verify_candidate_on_work_lane(api, failed.lane, failed.target, candidate):
                    continue
            except Exception as exc:  # noqa: BLE001
                self.event(
                    {
                        "event": "failover_candidate_failed",
                        "lane": failed.lane,
                        "proxy_name": candidate.proxy_name,
                        "error": str(exc),
                    }
                )
                continue
            self.state.replace_lease(
                failed.lane,
                failed.group_name,
                failed.port,
                failed.target,
                candidate,
            )
            replacement = next(
                (lease for lease in self.state.leases(failed.target) if lease.lane == failed.lane),
                None,
            )
            self.event(
                {
                    "event": "lane_failover_complete",
                    "lane": failed.lane,
                    "old_egress_ip": failed.egress_ip,
                    "new_egress_ip": candidate.egress_ip,
                    "proxy_name": candidate.proxy_name,
                }
            )
            return replacement
        try:
            api.select(failed.group_name, failed.proxy_name)
            restored = api.group(failed.group_name).get("now") == failed.proxy_name
        except Exception as exc:  # noqa: BLE001
            restored = False
            self.event(
                {
                    "event": "lane_restore_failed",
                    "lane": failed.lane,
                    "target": failed.target,
                    "error": str(exc),
                }
            )
        self.event(
            {
                "event": "failover_candidate_batch_exhausted",
                "lane": failed.lane,
                "target": failed.target,
                "candidates": len(candidates),
                "preserved_existing_lease": True,
                "route_restored": restored,
            }
        )
        return None

    def ensure_lanes(self, target: str, lanes: int, refresh: bool = True) -> list[LaneLease]:
        if target not in self.config.targets:
            raise ValueError(f"unknown target: {target}")
        existing = self.assign(target, lanes=lanes, replace=False)
        if len(existing) == lanes:
            return existing
        self.event(
            {
                "event": "lane_inventory_needed",
                "target": target,
                "requested_lanes": lanes,
                "currently_assigned": len(existing),
            }
        )
        self.inventory(target, refresh=refresh)
        return self.assign(target, lanes=lanes, replace=False)

    def monitor(self, target: str, once: bool = False) -> None:
        stop = threading.Event()

        def request_stop(signum: int, _frame: Any) -> None:
            self.event({"event": "monitor_stop_requested", "signal": signum})
            stop.set()

        previous_handlers: dict[int, Any] = {}
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, request_stop)
        try:
            while not stop.is_set():
                try:
                    self.check_lanes(target, replace_failed=True)
                except Exception as exc:  # noqa: BLE001
                    self.event(
                        {
                            "event": "monitor_cycle_failed",
                            "target": target,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        }
                    )
                    if once:
                        raise
                if once:
                    return
                stop.wait(self.config.gateway.monitor_interval_seconds)
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)

    def proxy_urls(self, target: str) -> list[str]:
        leases = [
            lease for lease in self.state.leases(target) if lease.status == "active"
        ]
        return [f"http://{self.config.gateway.listen}:{lease.port}" for lease in leases]
