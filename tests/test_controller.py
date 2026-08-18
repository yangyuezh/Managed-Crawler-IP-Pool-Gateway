from datetime import datetime, timezone
from pathlib import Path

import pytest

from crawler_gateway.config import AppConfig, GatewaySettings, ProviderConfig
from crawler_gateway.controller import (
    GatewayController,
    _provider_config_fingerprints,
    maintenance_target_names,
)
from crawler_gateway.mihomo import MihomoApi
from crawler_gateway.paths import ProjectPaths
from crawler_gateway.state import Candidate, LaneLease, ProbeResult


def result(*, healthy: bool, egress_ip: str | None, status_code: int | None, error_type: str | None):
    return ProbeResult(
        node_key="provider\x1fnode",
        provider="provider",
        proxy_name="node",
        target="target",
        checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        healthy=healthy,
        egress_ip=egress_ip,
        latency_ms=10,
        status_code=status_code,
        error_type=error_type,
        error="failed" if not healthy else None,
        detail={},
    )


def lease(lane: int, *, status: str = "active") -> LaneLease:
    return LaneLease(
        lane=lane,
        group_name=f"WORK-{lane:02d}",
        port=17890 + lane,
        target="target",
        node_key=f"provider\x1fnode-{lane}",
        provider="provider",
        proxy_name=f"node-{lane}",
        egress_ip=f"203.0.113.{lane}",
        assigned_at="2026-08-13T00:00:00+00:00",
        last_verified_at=None,
        consecutive_failures=0,
        status=status,
    )


def test_provider_fingerprint_tracks_config_but_not_display_name(
    tmp_path: Path,
) -> None:
    paths = ProjectPaths(tmp_path)
    provider_dir = paths.runtime_dir / "providers"
    provider_dir.mkdir(parents=True)
    provider_file = provider_dir / "provider.yaml"
    config = AppConfig(
        path=tmp_path / "gateway.yaml",
        gateway=GatewaySettings(backend="mihomo"),
        ip_check_urls=(),
        providers=(ProviderConfig(name="provider", type="file"),),
        targets={},
    )
    provider_file.write_text(
        "proxies:\n  - name: alpha\n    server: one.example\n    port: 443\n",
        encoding="utf-8",
    )
    first = _provider_config_fingerprints(paths, config)
    provider_file.write_text(
        "proxies:\n  - name: alpha\n    server: two.example\n    port: 443\n",
        encoding="utf-8",
    )
    second = _provider_config_fingerprints(paths, config)

    key = ("provider", "[provider] alpha")
    assert len(first[key]) == 64
    assert first[key] != second[key]


def test_target_http_failure_stops_rotation() -> None:
    assert GatewayController._target_level_failure(
        result(healthy=False, egress_ip="203.0.113.10", status_code=503, error_type="UnexpectedStatus")
    )


def test_target_http_failure_does_not_require_known_egress_ip() -> None:
    assert GatewayController._target_level_failure(
        result(
            healthy=False,
            egress_ip=None,
            status_code=404,
            error_type="UnexpectedStatus",
        )
    )


def test_node_connection_failure_can_rotate() -> None:
    assert not GatewayController._target_level_failure(
        result(healthy=False, egress_ip=None, status_code=None, error_type="EgressIpUnavailable")
    )


def test_target_transport_failure_can_rotate() -> None:
    assert not GatewayController._target_level_failure(
        result(healthy=False, egress_ip="203.0.113.10", status_code=None, error_type="ReadTimeout")
    )


def test_lane_probe_rejects_a_selector_binding_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = object.__new__(GatewayController)
    current_lease = lease(1)
    api = type("Api", (), {"group": lambda _self, _group: {"now": "DIRECT"}})()
    controller.process = type("Process", (), {"api": lambda _self: api})()
    controller.config = object()
    monkeypatch.setattr(
        "crawler_gateway.controller.probe_existing_lane",
        lambda **_kwargs: pytest.fail("a mismatched lane must not send a target request"),
    )

    probe = controller._probe_lane_lease(current_lease)

    assert probe.healthy is False
    assert probe.error_type == "RuntimeError"
    assert "binding mismatch" in str(probe.error)


def test_ensure_gateway_recovers_a_stopped_backend() -> None:
    controller = object.__new__(GatewayController)
    controller.config = type(
        "Config",
        (),
        {"gateway": type("Gateway", (), {"backend": "xray"})()},
    )()
    calls: list[object] = []

    class Api:
        def version(self):
            calls.append("version")
            return {"version": "test"}

    class Process:
        def running(self):
            return False

        def prepare(self, *, refresh):
            calls.append(("prepare", refresh))

        def start(self):
            calls.append("start")
            return 101

        def api(self):
            return Api()

    controller.process = Process()
    controller.paths = object()
    controller.state = type("State", (), {"leases": lambda _self: []})()
    events: list[dict] = []
    controller.event = events.append

    status = controller.ensure_gateway()

    assert calls == [("prepare", False), "start", "version"]
    assert status["recovered"] is True
    assert status["pid"] == 101
    assert events[-1]["event"] == "gateway_recovered"


def test_ensure_gateway_restores_only_persisted_lane_leases() -> None:
    controller = object.__new__(GatewayController)
    controller.config = type(
        "Config",
        (),
        {"gateway": type("Gateway", (), {"backend": "xray"})()},
    )()
    selected: dict[str, str] = {}

    class Api:
        def version(self):
            return {"version": "test"}

        def select(self, group, proxy_name):
            selected[group] = proxy_name

        def group(self, group):
            return {"now": selected.get(group)}

    class Process:
        def running(self):
            return False

        def prepare(self, *, refresh):
            assert refresh is False

        def start(self):
            return 102

        def api(self):
            return Api()

    class State:
        def leases(self):
            return [lease(2)]

        def record_lane_check(self, *_args):
            pytest.fail("a restored lease must not be degraded")

    controller.process = Process()
    controller.paths = object()
    controller.state = State()
    controller.event = lambda _event: None

    status = controller.ensure_gateway()

    assert selected == {"WORK-02": "node-2"}
    assert status["restored_leases"] == 1
    assert status["degraded_leases"] == 0


def test_matching_target_failures_from_distinct_egress_ips_are_shared_outage() -> None:
    failures = [
        result(
            healthy=False,
            egress_ip=f"203.0.113.{index}",
            status_code=404,
            error_type="UnexpectedStatus",
        )
        for index in range(1, 4)
    ]

    assert GatewayController._shared_target_failure_signature(failures) == "HTTP 404"


def test_matching_target_failures_from_one_or_unknown_egress_are_not_shared() -> None:
    same_ip = [
        result(
            healthy=False,
            egress_ip="203.0.113.1",
            status_code=404,
            error_type="UnexpectedStatus",
        )
        for _ in range(3)
    ]
    unknown = [
        result(
            healthy=False,
            egress_ip=None,
            status_code=404,
            error_type="UnexpectedStatus",
        )
        for _ in range(3)
    ]

    assert GatewayController._shared_target_failure_signature(same_ip) is None
    assert GatewayController._shared_target_failure_signature(unknown) is None


def test_one_or_mixed_target_failures_are_not_shared_outage() -> None:
    one = result(
        healthy=False,
        egress_ip=None,
        status_code=404,
        error_type="UnexpectedStatus",
    )
    other = result(
        healthy=False,
        egress_ip=None,
        status_code=503,
        error_type="UnexpectedStatus",
    )

    assert GatewayController._shared_target_failure_signature([one]) is None
    assert GatewayController._shared_target_failure_signature([one, other]) is None


def test_mihomo_inventory_places_direct_after_provider_nodes() -> None:
    api = object.__new__(MihomoApi)
    api.include_direct = True
    api.provider_payload = lambda: {
        "providers": {
            "p": {
                "proxies": [
                    {"name": "[p] node", "type": "VLESS", "alive": True},
                ]
            }
        }
    }
    provider = type(
        "Provider",
        (),
        {"name": "p", "type": "shadowrocket", "scope": "subscription"},
    )()

    nodes = api.discover_nodes((provider,))

    assert nodes[0].provider == "p"
    assert nodes[0].source_kind == "subscription"
    assert nodes[1].provider == "__local__"
    assert nodes[1].name == "DIRECT"
    assert nodes[1].source_kind == "direct"


def test_work_lane_accepts_target_healthy_candidate_without_egress_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = object.__new__(GatewayController)
    controller.config = type(
        "Config",
        (),
        {
            "gateway": type(
                "Gateway",
                (),
                {"work_port_base": 17891, "settle_seconds": 0},
            )()
        },
    )()
    controller.event = lambda _event: None

    class State:
        recorded: list[ProbeResult] = []

        def record_probe(self, probe_result):
            self.recorded.append(probe_result)

    controller.state = State()

    class Api:
        def select(self, _group, _name):
            return None

        def group(self, _group):
            return {"now": "node"}

    monkeypatch.setattr(
        "crawler_gateway.controller.probe_existing_lane",
        lambda **_kwargs: (
            None,
            type(
                "Response",
                (),
                {
                    "healthy": True,
                    "latency_ms": 10,
                    "status_code": 200,
                    "error_type": None,
                    "error": None,
                    "detail": {},
                },
            )(),
            {},
        ),
    )
    candidate = Candidate(
        node_key="provider\x1fnode",
        provider="provider",
        proxy_name="node",
        node_type="VLESS",
        source_kind="subscription",
        egress_ip=None,
        latency_ms=20,
        checked_at="2026-08-13T00:00:00+00:00",
        successful_probes=1,
        failed_probes=0,
    )

    assert controller._verify_candidate_on_work_lane(Api(), 1, "target", candidate)


def test_lane_check_stays_healthy_when_egress_lookup_temporarily_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = object.__new__(GatewayController)
    controller.config = type(
        "Config",
        (),
        {"gateway": type("Gateway", (), {"failure_threshold": 3})()},
    )()
    controller.process = type(
        "Process",
        (),
        {
            "running": lambda _self: True,
            "api": lambda _self: type(
                "Api",
                (),
                {
                    "version": lambda _api_self: {},
                    "group": lambda _api_self, _group: {"now": "node"},
                },
            )(),
        },
    )()
    controller.event = lambda _event: None
    lease = LaneLease(
        lane=1,
        group_name="WORK-01",
        port=17891,
        target="target",
        node_key="provider\x1fnode",
        provider="provider",
        proxy_name="node",
        egress_ip="203.0.113.10",
        assigned_at="2026-08-13T00:00:00+00:00",
        last_verified_at=None,
        consecutive_failures=0,
        status="active",
    )

    class State:
        def leases(self, _target):
            return [lease]

        def record_probe(self, _result):
            return None

        def record_lane_check(self, _lane, healthy, _checked_at):
            assert healthy is True
            return 0

    controller.state = State()
    monkeypatch.setattr(
        "crawler_gateway.controller.probe_existing_lane",
        lambda **_kwargs: (
            None,
            type(
                "Response",
                (),
                {
                    "healthy": True,
                    "latency_ms": 10,
                    "status_code": 200,
                    "error_type": None,
                    "error": None,
                    "detail": {},
                },
            )(),
            {},
        ),
    )

    results = controller.check_lanes("target", replace_failed=True)

    assert results[0]["healthy"] is True
    assert results[0]["observed_egress_ip"] is None


def test_failed_lanes_are_handled_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = object.__new__(GatewayController)
    controller.config = type(
        "Config",
        (),
        {"gateway": type("Gateway", (), {"failure_threshold": 1})()},
    )()
    api = type("Api", (), {"version": lambda _self: {}})()
    controller.process = type(
        "Process",
        (),
        {
            "running": lambda _self: True,
            "api": lambda _self: api,
        },
    )()
    lane_leases = [lease(1), lease(2), lease(3)]

    class State:
        def leases(self, _target):
            return lane_leases

        def record_probe(self, _result):
            return None

        def record_lane_check(self, lane_number, healthy, _checked_at):
            return 0 if healthy else 1

    controller.state = State()
    controller.event = lambda _event: None
    probes = {
        1: result(
            healthy=False,
            egress_ip=None,
            status_code=None,
            error_type="ReadTimeout",
        ),
        2: result(
            healthy=False,
            egress_ip=None,
            status_code=None,
            error_type="TlsError",
        ),
        3: result(
            healthy=True,
            egress_ip="203.0.113.3",
            status_code=200,
            error_type=None,
        ),
    }
    monkeypatch.setattr(controller, "_probe_lane_lease", lambda item: probes[item.lane])
    replaced: list[int] = []

    def replace_lane(_api, failed):
        replaced.append(failed.lane)
        if failed.lane == 1:
            raise RuntimeError("lane 1 replacement failed")
        return None

    monkeypatch.setattr(controller, "replace_lane", replace_lane)

    results = controller.check_lanes("target", replace_failed=True)

    assert replaced == [1, 2]
    assert len(results) == 3
    assert results[2]["healthy"] is True
    assert "failover_error" in results[0]
    assert results[1]["replacement"] is None


def test_shared_target_outage_does_not_rotate_any_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = object.__new__(GatewayController)
    controller.config = type(
        "Config",
        (),
        {"gateway": type("Gateway", (), {"failure_threshold": 3})()},
    )()
    api = type("Api", (), {"version": lambda _self: {}})()
    controller.process = type(
        "Process",
        (),
        {
            "running": lambda _self: True,
            "api": lambda _self: api,
        },
    )()
    lane_leases = [lease(1), lease(2), lease(3)]

    class State:
        def leases(self, _target):
            return lane_leases

        def record_probe(self, _result):
            return None

        def record_lane_check(self, _lane, _healthy, _checked_at):
            return 1

    controller.state = State()
    events: list[dict] = []
    controller.event = events.append
    failures = {
        lane_number: result(
            healthy=False,
            egress_ip=f"203.0.113.{lane_number}",
            status_code=404,
            error_type="UnexpectedStatus",
        )
        for lane_number in range(1, 4)
    }
    monkeypatch.setattr(
        controller,
        "_probe_lane_lease",
        lambda current_lease: failures[current_lease.lane],
    )
    monkeypatch.setattr(
        controller,
        "replace_lane",
        lambda *_args: pytest.fail("shared target outage must not rotate lanes"),
    )

    results = controller.check_lanes("target", replace_failed=True)

    assert all(item["failover_suppressed"] == "shared_target_failure" for item in results)
    assert any(event["event"] == "target_outage_suspected" for event in events)


def test_persistent_shared_target_failure_rotates_only_one_canary_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = object.__new__(GatewayController)
    controller.config = type(
        "Config",
        (),
        {"gateway": type("Gateway", (), {"failure_threshold": 3})()},
    )()
    api = type("Api", (), {"version": lambda _self: {}})()
    controller.process = type(
        "Process",
        (),
        {
            "running": lambda _self: True,
            "api": lambda _self: api,
        },
    )()
    lane_leases = [lease(1), lease(2), lease(3)]

    class State:
        def leases(self, _target):
            return lane_leases

        def record_probe(self, _result):
            return None

        def record_lane_check(self, _lane, _healthy, _checked_at):
            return 3

    controller.state = State()
    events: list[dict] = []
    controller.event = events.append
    failures = {
        lane_number: result(
            healthy=False,
            egress_ip=f"203.0.113.{lane_number}",
            status_code=404,
            error_type="UnexpectedStatus",
        )
        for lane_number in range(1, 4)
    }
    monkeypatch.setattr(
        controller,
        "_probe_lane_lease",
        lambda current_lease: failures[current_lease.lane],
    )
    replaced: list[int] = []
    monkeypatch.setattr(
        controller,
        "replace_lane",
        lambda _api, current_lease: replaced.append(current_lease.lane) or None,
    )

    results = controller.check_lanes("target", replace_failed=True)

    assert replaced == [1]
    assert "failover_suppressed" not in results[0]
    assert results[1]["failover_suppressed"] == "shared_target_canary_only"
    assert results[2]["failover_suppressed"] == "shared_target_canary_only"
    assert any(event["event"] == "target_outage_canary_failover" for event in events)


def test_exhausted_failover_preserves_only_the_failed_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = object.__new__(GatewayController)
    controller.config = type(
        "Config",
        (),
        {"gateway": type("Gateway", (), {"healthy_max_age_hours": 24})()},
    )()
    failed = lease(1, status="degraded")
    candidate = Candidate(
        node_key="provider\x1freplacement",
        provider="provider",
        proxy_name="replacement",
        node_type="VLESS",
        source_kind="subscription",
        egress_ip="203.0.113.20",
        latency_ms=20,
        checked_at="2026-08-13T00:00:00+00:00",
        successful_probes=1,
        failed_probes=0,
    )

    class State:
        def leases(self, _target):
            return [failed, lease(2), lease(3)]

        def qualified_reserve_candidates(self, *_args, **_kwargs):
            return [candidate]

    controller.state = State()
    events: list[dict] = []
    controller.event = events.append

    class Api:
        selected: dict[str, str] = {}

        def select(self, group, proxy_name):
            self.selected[group] = proxy_name

        def group(self, group):
            return {"now": self.selected.get(group)}

    api = Api()

    def reject_candidate(api_value, lane, _target, replacement):
        api_value.select(f"WORK-{lane:02d}", replacement.proxy_name)
        return False

    monkeypatch.setattr(controller, "_verify_candidate_on_work_lane", reject_candidate)

    replacement = controller._replace_lane_from_candidates(api, failed)

    assert replacement is None
    assert api.selected[failed.group_name] == failed.proxy_name
    assert any(event["event"] == "failover_candidate_batch_exhausted" for event in events)


def test_ensure_lanes_never_forces_healthy_lane_reassignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = object.__new__(GatewayController)
    controller.config = type("Config", (), {"targets": {"target": object()}})()
    active = [lease(1), lease(2), lease(3)]
    calls: list[bool] = []

    def assign(_target, *, lanes, replace):
        calls.append(replace)
        return active[:2] if len(calls) == 1 else active

    monkeypatch.setattr(controller, "assign", assign)
    monkeypatch.setattr(controller, "inventory", lambda *_args, **_kwargs: None)
    controller.event = lambda _event: None

    assert controller.ensure_lanes("target", lanes=3) == active
    assert calls == [False, False]


def test_proxy_urls_exclude_degraded_lanes() -> None:
    controller = object.__new__(GatewayController)
    controller.config = type(
        "Config",
        (),
        {"gateway": type("Gateway", (), {"listen": "127.0.0.1"})()},
    )()
    controller.state = type(
        "State",
        (),
        {"leases": lambda _self, _target: [lease(1), lease(2, status="degraded")]},
    )()

    assert controller.proxy_urls("target") == ["http://127.0.0.1:17891"]


def test_maintenance_targets_support_config_defaults_and_runtime_override() -> None:
    config = type(
        "Config",
        (),
        {
            "gateway": type(
                "Gateway",
                (),
                {"maintenance_targets": ("second",)},
            )(),
            "targets": {"first": object(), "second": object()},
        },
    )()

    assert maintenance_target_names(config) == ("second",)
    assert maintenance_target_names(config, ["first", "second"]) == (
        "first",
        "second",
    )
    with pytest.raises(ValueError, match="unknown targets"):
        maintenance_target_names(config, ["missing"])


def test_multi_target_reserve_probe_continues_after_one_target_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = object.__new__(GatewayController)
    controller.config = type(
        "Config",
        (),
        {
            "gateway": type(
                "Gateway",
                (),
                {
                    "maintenance_targets": ("first", "second"),
                    "healthy_max_age_hours": 24,
                },
            )(),
            "targets": {"first": object(), "second": object()},
        },
    )()

    class State:
        def pool_snapshot(self, target, _max_age_hours):
            return {"target": target, "reserve_total": 2}

    controller.state = State()
    controller.event = lambda _event: None
    called: list[str] = []

    def inventory(target, **_kwargs):
        called.append(target)
        if target == "first":
            raise RuntimeError("first target failed")
        return {"target": target, "healthy": 1}

    monkeypatch.setattr(controller, "inventory", inventory)

    report = controller.probe_reserve()

    assert called == ["first", "second"]
    assert report["results"]["first"]["error"]["error_type"] == "RuntimeError"
    assert report["results"]["second"]["inventory"]["healthy"] == 1


def test_reserve_maintenance_once_runs_refresh_probe_and_factual_report(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = object.__new__(GatewayController)
    controller.config = type(
        "Config",
        (),
        {
            "gateway": type(
                "Gateway",
                (),
                {
                    "reserve_refresh_interval_seconds": 3600,
                    "maintenance_error_retry_seconds": 60,
                    "probe_history_retention_days": 90,
                },
            )(),
        },
    )()
    controller.paths = type("Paths", (), {"runtime_dir": tmp_path})()
    controller.state = type(
        "State",
        (),
        {"prune_probe_history": lambda _self, _days: 0},
    )()
    controller.event = lambda _event: None
    calls: list[str] = []
    monkeypatch.setattr(controller, "resolve_targets", lambda _requested: ("target",))
    monkeypatch.setattr(
        controller,
        "ensure_gateway",
        lambda: calls.append("gateway") or {"running": True},
    )

    def refresh():
        calls.append("refresh")
        return {"reserve_total": 10}

    def probe(_targets, **_kwargs):
        calls.append("probe")
        return {"results": {"target": {"error": None}}}

    def pools(_targets):
        calls.append("report")
        return {"targets": {"target": {"reserve_qualified": 4}}}

    monkeypatch.setattr(controller, "refresh_reserve", refresh)
    monkeypatch.setattr(controller, "probe_reserve", probe)
    monkeypatch.setattr(controller, "pool_report", pools)
    monkeypatch.setattr(
        "crawler_gateway.controller.runtime_parameters",
        lambda *_args, **_kwargs: {},
    )

    result_value = controller.maintain_reserve(once=True)

    assert calls == ["gateway", "refresh", "probe", "report"]
    assert result_value["cycle"] == 1
    assert result_value["pools"]["target"]["reserve_qualified"] == 4
    assert not (tmp_path / "reserve_maintenance.pid").exists()
