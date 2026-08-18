from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import AppConfig, ConfigError, load_config
from .controller import (
    GatewayController,
    maintenance_target_names,
    print_human_event,
    runtime_parameters,
)
from .events import RotatingEventSink
from .faults import fault_path, write_fault
from .mihomo import MihomoError, MihomoProcess, ensure_secret, render_config
from .xray import XrayProcess
from .paths import ProjectPaths
from .state import StateStore
from .integrations.nsfc import (
    DEFAULT_NSFC_DATA,
    DEFAULT_NSFC_REPO,
    nsfc_progress,
    run_nsfc,
    stop_nsfc_processes,
)
from .launchd import (
    LaunchAgentError,
    install_and_start_service,
    service_status,
    stop_service,
)


def _paths() -> ProjectPaths:
    paths = ProjectPaths.discover()
    paths.ensure_directories()
    return paths


def _config_path(args: argparse.Namespace, paths: ProjectPaths) -> Path:
    value = getattr(args, "config", None)
    return Path(value).expanduser().resolve() if value else paths.config_path


def _load_runtime(args: argparse.Namespace) -> tuple[ProjectPaths, AppConfig, str, Any, StateStore]:
    paths = _paths()
    config = load_config(_config_path(args, paths))
    secret = ensure_secret(paths.secret_path)
    process = (
        XrayProcess(paths, config)
        if config.gateway.backend == "xray"
        else MihomoProcess(paths, config, secret)
    )
    state = StateStore(paths.state_path)
    return paths, config, secret, process, state


def _controller(args: argparse.Namespace) -> GatewayController:
    paths, config, _secret, process, state = _load_runtime(args)
    sinks = []
    if getattr(args, "human", False):
        sinks.append(print_human_event)
    event_log = getattr(args, "event_log", None)
    if event_log:
        sinks.append(RotatingEventSink(Path(event_log)))
    if not sinks:
        return GatewayController(config, paths, process, state)

    def emit(event: dict[str, Any]) -> None:
        for sink in sinks:
            sink(event)

    return GatewayController(config, paths, process, state, event_sink=emit)


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _runtime_context(
    config: AppConfig,
    state: StateStore,
    requested_targets: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    targets = maintenance_target_names(config, requested_targets)
    return {
        "parameters": runtime_parameters(config, targets),
        "provider_refresh": state.provider_refresh_states(),
        "pools": {
            target: state.pool_snapshot(target, config.gateway.healthy_max_age_hours)
            for target in targets
        },
    }


def command_init(args: argparse.Namespace) -> int:
    paths = _paths()
    destination = _config_path(args, paths)
    if destination.exists() and not args.force:
        print(f"private configuration already exists: {destination}")
        return 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(paths.example_config_path, destination)
    destination.chmod(0o600)
    print(f"created private configuration: {destination}")
    print("edit the provider URL and target probe record ID before running validate")
    return 0


def command_validate(args: argparse.Namespace) -> int:
    paths = _paths()
    config = load_config(_config_path(args, paths))
    _json(
        {
            "valid": True,
            "config": str(config.path),
            "backend": config.gateway.backend,
            "direct_outbound_interface": config.gateway.direct_outbound_interface,
            "node_outbound_interface": config.gateway.node_outbound_interface or "system-default",
            "providers": [provider.name for provider in config.providers],
            "targets": sorted(config.targets),
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
            "runtime_parameters": runtime_parameters(config),
        }
    )
    return 0


def command_render(args: argparse.Namespace) -> int:
    paths, config, secret, process, _state = _load_runtime(args)
    if config.gateway.backend == "xray":
        counts = process.prepare()
        _json({"backend": "xray", "providers": counts, "nodes": sum(counts.values())})
    else:
        output = render_config(config, paths, secret)
        print(f"rendered private Mihomo configuration: {output}")
    return 0


def command_start(args: argparse.Namespace) -> int:
    controller = _controller(args)
    gateway = controller.ensure_gateway()
    _json(
        {
            "running": controller.process.running(),
            "gateway": controller.process.describe(),
            "recovery": gateway,
            "runtime": _runtime_context(controller.config, controller.state),
        }
    )
    return 0


def command_stop(args: argparse.Namespace) -> int:
    paths, config, secret, process, state = _load_runtime(args)
    stopped = process.stop()
    legacy_stopped = False
    if config.gateway.backend == "xray":
        legacy_stopped = MihomoProcess(paths, config, secret).stop()
    cleared_leases = state.clear_all_leases("gateway stopped")
    _json(
        {
            "running": False,
            "stopped_process": stopped,
            "stopped_legacy_mihomo": legacy_stopped,
            "cleared_leases": cleared_leases,
        }
    )
    return 0


def command_stop_nsfc(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir).expanduser().resolve()
    stopped = stop_nsfc_processes(data_dir)
    _json(
        {
            "data_dir": str(data_dir),
            "stopped": len(stopped),
            "processes": [
                {"pid": item["pid"], "role": item["role"]} for item in stopped
            ],
        }
    )
    return 0


def command_stop_maintenance(args: argparse.Namespace) -> int:
    background_service = stop_service(remove=False)
    controller = _controller(args)
    pid = controller.stop_reserve_maintenance()
    _json(
        {
            "stopped": pid is not None,
            "pid": pid,
            "background_service": background_service,
            "reserve_maintenance": controller.reserve_maintenance_status(),
        }
    )
    return 0


def command_install_service(args: argparse.Namespace) -> int:
    paths = _paths()
    config = load_config(_config_path(args, paths))
    status = install_and_start_service(paths, config.path, python=sys.executable)
    _json({"operation": "install_service", "service": status})
    return 0 if status["running"] else 2


def command_service_status(_args: argparse.Namespace) -> int:
    status = service_status()
    _json(status)
    return 0


def command_disable_service(args: argparse.Namespace) -> int:
    status = stop_service(remove=args.remove)
    _json(
        {
            "operation": "uninstall_service" if args.remove else "disable_service",
            "service": status,
        }
    )
    return 0


def command_status(args: argparse.Namespace) -> int:
    paths, config, _secret, process, state = _load_runtime(args)
    controller = GatewayController(config, paths, process, state)
    selected_targets = maintenance_target_names(
        config,
        [args.target] if getattr(args, "target", None) else None,
    )
    leases = [asdict(lease) for lease in state.leases(getattr(args, "target", None))]
    result = {
        "gateway": process.describe(),
        "state": state.counts(
            config.gateway.healthy_max_age_hours,
            target=selected_targets[0] if len(selected_targets) == 1 else None,
        ),
        "runtime": _runtime_context(config, state, selected_targets),
        "reserve_maintenance": controller.reserve_maintenance_status(),
        "maintenance_service": service_status(),
        "leases": leases,
        "work_proxy_urls": [
            f"http://{config.gateway.listen}:{lease['port']}"
            for lease in leases
            if lease["status"] == "active"
        ],
    }
    if not args.skip_nsfc:
        result["nsfc"] = nsfc_progress(Path(args.nsfc_repo), Path(args.nsfc_data_dir))
    if args.plain:
        gateway = result["gateway"]
        state_value = result["state"]
        print("爬虫代理网关")
        print(f"网关状态：{'运行中' if gateway.get('running') else '已停止'} ({gateway.get('backend', 'unknown')})")
        print(
            f"候选出口：{state_value['nodes']}（订阅代理 {state_value['subscription_nodes']} + "
            f"本地节点 {state_value['local_proxy_nodes']} + "
            f"本机直连 {state_value['direct_candidates']}）"
        )
        print(
            f"出口清查：已测 {state_value['egress_tested_nodes']}/{state_value['nodes']} | "
            f"公网IP已识别 {state_value['egress_healthy_nodes']} | "
            f"已知独立IP {state_value['distinct_egress_ips']}"
        )
        print(
            f"目标验证：已测 {state_value['tested_node_targets']} | "
            f"HTTP 200 {state_value['target_http_200_nodes']} | "
            f"可用节点 {state_value['healthy_node_targets']} | "
            f"已知独立IP {state_value['distinct_healthy_egress_ips']} | "
            f"健康通道 {state_value['active_leases']}/{config.gateway.work_lanes} | "
            f"降级 {state_value['degraded_leases']}"
        )
        maintenance = result["reserve_maintenance"]
        print(
            f"备用池定时维护：{'运行中' if maintenance['running'] else '已停止'}"
            + (f" (PID {maintenance['pid']})" if maintenance["pid"] else "")
        )
        service = result["maintenance_service"]
        print(
            "后台自动维护："
            + (
                f"运行中 (PID {service['pid']})"
                if service["running"]
                else "已安装但未运行"
                if service["installed"]
                else "未安装"
            )
        )
        print("\n节点池事实")
        for target, pool in result["runtime"]["pools"].items():
            print(
                f"{target}：备用池 {pool['reserve_total']} | "
                f"通过 {pool['reserve_qualified']} | 失败 {pool['reserve_rejected']} | "
                f"未检测 {pool['reserve_untested']} | 过期 {pool['reserve_stale']} | "
                f"主池健康 {pool['primary_active']}/{pool['primary_assigned']}"
            )
        nsfc = result.get("nsfc")
        if isinstance(nsfc, dict) and nsfc.get("available"):
            print("\nNSFC 结项详情")
            print(
                f"严格连续完成：{nsfc['continuous_done']:,} / {nsfc['base_rows']:,} "
                f"({nsfc['percent']:.2f}%)"
            )
            print(f"剩余：{nsfc['remaining']:,} | 详情文件：{nsfc['detail_success_files']:,}")
            print(f"下一个断点：{nsfc['next']}")
            print(
                f"错误：{nsfc['active_detail_errors']} | 最近1小时新增：{nsfc['recent'].get('60m', 0)} | "
                f"进程：{nsfc['process_state']}"
            )
        elif isinstance(nsfc, dict):
            print(f"\nNSFC 状态读取失败：{nsfc.get('error', 'unknown error')}")
    else:
        _json(result)
    return 0


def command_discover(args: argparse.Namespace) -> int:
    controller = _controller(args)
    nodes = controller.discover(refresh=args.refresh)
    _json(
        {
            "nodes": len(nodes),
            "providers": sorted({node.provider for node in nodes}),
            "node_types": sorted({node.node_type for node in nodes}),
        }
    )
    return 0


def command_refresh_reserve(args: argparse.Namespace) -> int:
    controller = _controller(args)
    controller.event(
        {
            "event": "runtime_parameters",
            "parameters": runtime_parameters(controller.config),
        }
    )
    summary = controller.refresh_reserve()
    _json(
        {
            "operation": "refresh_reserve",
            "summary": summary,
            "facts": controller.pool_report(),
        }
    )
    return 0


def command_probe_reserve(args: argparse.Namespace) -> int:
    controller = _controller(args)
    targets = controller.resolve_targets(args.targets)
    controller.event(
        {
            "event": "runtime_parameters",
            "parameters": runtime_parameters(controller.config, targets),
        }
    )
    result = controller.probe_reserve(
        targets,
        limit=args.limit,
        concurrency=args.concurrency,
    )
    _json(
        {
            "operation": "probe_reserve",
            **result,
            "facts": controller.pool_report(targets),
        }
    )
    errors = [item["error"] for item in result["results"].values()]
    return 0 if not any(errors) else 2


def command_pool_status(args: argparse.Namespace) -> int:
    controller = _controller(args)
    targets = controller.resolve_targets(args.targets)
    report = {
        "runtime": runtime_parameters(controller.config, targets),
        "facts": controller.pool_report(targets),
    }
    if args.plain:
        print_human_event({"event": "runtime_parameters", "parameters": report["runtime"]})
        for target, pool in report["facts"]["targets"].items():
            print_human_event(
                {
                    "event": "reserve_probe_target_complete",
                    "target": target,
                    "pool": pool,
                }
            )
    else:
        _json(report)
    return 0


def command_maintain_reserve(args: argparse.Namespace) -> int:
    controller = _controller(args)
    result = controller.maintain_reserve(
        args.targets,
        once=args.once,
        interval_seconds=args.interval,
        limit=args.limit,
        concurrency=args.concurrency,
    )
    if args.once:
        _json({"operation": "maintain_reserve", "result": result})
        has_errors = bool(result.get("gateway_error") or result.get("refresh_error")) or any(
            item.get("error")
            for item in (result.get("probes", {}).get("results", {}) or {}).values()
        )
        return 2 if has_errors else 0
    return 0


def command_inventory(args: argparse.Namespace) -> int:
    controller = _controller(args)
    summary = controller.inventory(
        args.target,
        refresh=args.refresh,
        limit=args.limit,
        concurrency=args.concurrency,
    )
    _json(summary)
    if not summary["healthy"] and args.limit is None:
        progress = nsfc_progress(DEFAULT_NSFC_REPO, DEFAULT_NSFC_DATA)
        write_fault(
            fault_path(controller.config.gateway.fault_file),
            "全量节点体检未找到可用节点",
            progress,
        )
    return 0 if summary["healthy"] else 2


def command_assign(args: argparse.Namespace) -> int:
    controller = _controller(args)
    leases = controller.assign(args.target, lanes=args.lanes, replace=args.replace)
    _json(
        {
            "target": args.target,
            "assigned": len(leases),
            "leases": [asdict(lease) for lease in leases],
            "proxy_urls": controller.proxy_urls(args.target),
        }
    )
    return 0 if leases else 2


def command_check(args: argparse.Namespace) -> int:
    controller = _controller(args)
    results = controller.check_lanes(args.target, replace_failed=not args.no_replace)
    _json({"target": args.target, "results": results})
    return 0 if results and all(item["healthy"] for item in results) else 2


def command_monitor(args: argparse.Namespace) -> int:
    controller = _controller(args)
    controller.monitor(args.target, once=args.once)
    return 0


def command_proxy_list(args: argparse.Namespace) -> int:
    controller = _controller(args)
    urls = controller.proxy_urls(args.target)
    if args.format == "json":
        _json(urls)
    elif args.format == "csv":
        print(",".join(urls))
    else:
        print("\n".join(urls))
    return 0 if urls else 2


def command_run_nsfc(args: argparse.Namespace) -> int:
    controller = _controller(args)
    lanes = args.lanes if args.lanes is not None else controller.config.gateway.work_lanes
    _json(
        {
            "operation": "run_nsfc",
            "requested_lanes": lanes,
            "runtime": _runtime_context(
                controller.config,
                controller.state,
                [args.target],
            ),
        }
    )
    code = run_nsfc(
        controller=controller,
        config_path=controller.config.path,
        target=args.target,
        lanes=lanes,
        repo=Path(args.repo).expanduser(),
        data_dir=Path(args.data_dir).expanduser(),
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        retries=args.retries,
        timeout=args.timeout,
        wait_for_lanes=args.wait_for_lanes,
        lane_retry_seconds=args.lane_retry_seconds,
    )
    if code not in {0, 130}:
        progress = nsfc_progress(Path(args.repo), Path(args.data_dir))
        write_fault(
            fault_path(controller.config.gateway.fault_file),
            f"NSFC 多通道爬虫退出，返回码 {code}",
            progress,
        )
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crawler-gateway",
        description="Target-aware multi-lane proxy gateway for local crawlers.",
    )
    parser.add_argument("--config", help="private gateway YAML; defaults to private/gateway.yaml")
    parser.add_argument("--human", action="store_true", help="print concise human-readable progress events")
    parser.add_argument("--event-log", help="write rotating JSONL operational events")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create a private configuration template")
    init_parser.add_argument("--force", action="store_true")
    init_parser.set_defaults(func=command_init)

    validate_parser = subparsers.add_parser("validate", help="validate private configuration")
    validate_parser.set_defaults(func=command_validate)

    render_parser = subparsers.add_parser("render", help="prepare private gateway runtime files")
    render_parser.set_defaults(func=command_render)

    start_parser = subparsers.add_parser("start", help="prepare and start the isolated gateway")
    start_parser.set_defaults(func=command_start)

    stop_parser = subparsers.add_parser("stop", help="stop only the isolated crawler gateway")
    stop_parser.set_defaults(func=command_stop)

    stop_nsfc_parser = subparsers.add_parser(
        "stop-nsfc",
        help="stop NSFC crawler/controller processes for one data directory",
    )
    stop_nsfc_parser.add_argument("--data-dir", default=str(DEFAULT_NSFC_DATA))
    stop_nsfc_parser.set_defaults(func=command_stop_nsfc)

    stop_maintenance_parser = subparsers.add_parser(
        "stop-maintenance",
        help="stop the periodic reserve-pool maintenance process",
    )
    stop_maintenance_parser.set_defaults(func=command_stop_maintenance)

    install_service_parser = subparsers.add_parser(
        "install-service",
        help="install and start the macOS automatic maintenance service",
    )
    install_service_parser.set_defaults(func=command_install_service)

    service_status_parser = subparsers.add_parser(
        "service-status",
        help="show the macOS automatic maintenance service status",
    )
    service_status_parser.set_defaults(func=command_service_status)

    disable_service_parser = subparsers.add_parser(
        "disable-service",
        help="disable and stop the macOS automatic maintenance service",
    )
    disable_service_parser.add_argument("--remove", action="store_true")
    disable_service_parser.set_defaults(func=command_disable_service)

    status_parser = subparsers.add_parser("status", help="show process, node and lane state")
    status_parser.add_argument("--target")
    status_parser.add_argument("--plain", action="store_true")
    status_parser.add_argument("--skip-nsfc", action="store_true")
    status_parser.add_argument("--nsfc-repo", default=str(DEFAULT_NSFC_REPO))
    status_parser.add_argument("--nsfc-data-dir", default=str(DEFAULT_NSFC_DATA))
    status_parser.set_defaults(func=command_status)

    discover_parser = subparsers.add_parser("discover", help="load node inventory from providers")
    discover_parser.add_argument("--refresh", action="store_true")
    discover_parser.set_defaults(func=command_discover)

    refresh_reserve_parser = subparsers.add_parser(
        "refresh-reserve",
        help="refresh providers into the reserve node inventory without target probing",
    )
    refresh_reserve_parser.set_defaults(func=command_refresh_reserve)

    probe_reserve_parser = subparsers.add_parser(
        "probe-reserve",
        help="probe reserve nodes against one or more configured targets",
    )
    probe_reserve_parser.add_argument(
        "targets",
        nargs="*",
        help="target names; defaults to gateway.maintenance_targets or every target",
    )
    probe_reserve_parser.add_argument("--limit", type=int)
    probe_reserve_parser.add_argument("--concurrency", type=int)
    probe_reserve_parser.set_defaults(func=command_probe_reserve)

    pool_status_parser = subparsers.add_parser(
        "pool-status",
        help="print parameters and reserve/primary pool facts",
    )
    pool_status_parser.add_argument("targets", nargs="*")
    pool_status_parser.add_argument("--plain", action="store_true")
    pool_status_parser.set_defaults(func=command_pool_status)

    maintain_reserve_parser = subparsers.add_parser(
        "maintain-reserve",
        help="periodically refresh and target-test the reserve pool",
    )
    maintain_reserve_parser.add_argument("targets", nargs="*")
    maintain_reserve_parser.add_argument("--once", action="store_true")
    maintain_reserve_parser.add_argument("--interval", type=float)
    maintain_reserve_parser.add_argument("--limit", type=int)
    maintain_reserve_parser.add_argument("--concurrency", type=int)
    maintain_reserve_parser.set_defaults(func=command_maintain_reserve)

    inventory_parser = subparsers.add_parser("inventory", help="test every node against a target")
    inventory_parser.add_argument("target")
    inventory_parser.add_argument("--refresh", action="store_true")
    inventory_parser.add_argument("--limit", type=int)
    inventory_parser.add_argument("--concurrency", type=int)
    inventory_parser.set_defaults(func=command_inventory)

    assign_parser = subparsers.add_parser(
        "assign",
        help="assign target-verified proxy nodes to work lanes",
    )
    assign_parser.add_argument("target")
    assign_parser.add_argument("--lanes", type=int)
    assign_parser.add_argument("--replace", action="store_true", help="replace even currently valid leases")
    assign_parser.set_defaults(func=command_assign)

    check_parser = subparsers.add_parser("check", help="check work lanes and replace repeatedly failed lanes")
    check_parser.add_argument("target")
    check_parser.add_argument("--no-replace", action="store_true")
    check_parser.set_defaults(func=command_check)

    monitor_parser = subparsers.add_parser("monitor", help="continuously check and fail over work lanes")
    monitor_parser.add_argument("target")
    monitor_parser.add_argument("--once", action="store_true")
    monitor_parser.set_defaults(func=command_monitor)

    proxy_parser = subparsers.add_parser("proxy-list", help="print assigned work-lane proxy URLs")
    proxy_parser.add_argument("target")
    proxy_parser.add_argument("--format", choices=("lines", "csv", "json"), default="lines")
    proxy_parser.set_defaults(func=command_proxy_list)

    nsfc_parser = subparsers.add_parser(
        "run-nsfc",
        help="run the existing NSFC detail crawler through assigned work lanes",
    )
    nsfc_parser.add_argument("--target", default="nsfc_final_detail")
    nsfc_parser.add_argument(
        "--lanes",
        type=int,
        help="work lanes to use; defaults to gateway.work_lanes",
    )
    nsfc_parser.add_argument("--repo", default=str(DEFAULT_NSFC_REPO))
    nsfc_parser.add_argument("--data-dir", default=str(DEFAULT_NSFC_DATA))
    nsfc_parser.add_argument("--min-delay", type=float, default=0.15)
    nsfc_parser.add_argument("--max-delay", type=float, default=0.55)
    nsfc_parser.add_argument("--retries", type=int, default=8)
    nsfc_parser.add_argument("--timeout", type=float, default=30.0)
    nsfc_parser.add_argument("--wait-for-lanes", action="store_true")
    nsfc_parser.add_argument("--lane-retry-seconds", type=float, default=300.0)
    nsfc_parser.set_defaults(func=command_run_nsfc)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ConfigError, LaunchAgentError, MihomoError, ValueError) as exc:
        if getattr(args, "command", "") in {
            "inventory",
            "refresh-reserve",
            "probe-reserve",
            "maintain-reserve",
            "assign",
            "check",
            "monitor",
            "run-nsfc",
        }:
            try:
                paths = _paths()
                config = load_config(_config_path(args, paths))
                progress = nsfc_progress(DEFAULT_NSFC_REPO, DEFAULT_NSFC_DATA)
                write_fault(fault_path(config.gateway.fault_file), str(exc), progress)
            except Exception:  # noqa: BLE001
                pass
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
