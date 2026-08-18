import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from crawler_gateway.mihomo import ProxyNode, node_key
from crawler_gateway.state import EGRESS_TARGET, ProbeResult, StateStore


def result(node: ProxyNode, ip: str | None, latency: int) -> ProbeResult:
    return ProbeResult(
        node_key=node.key,
        provider=node.provider,
        proxy_name=node.name,
        target="target",
        checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        healthy=True,
        egress_ip=ip,
        latency_ms=latency,
        status_code=200,
        error_type=None,
        error=None,
        detail={},
    )


def test_state_store_closes_connection_after_context(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")

    with store.connect() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


def test_candidates_prefer_but_do_not_require_distinct_observed_ip(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    nodes = [
        ProxyNode(node_key("p", "a"), "p", "a", "VLESS", True, {}),
        ProxyNode(node_key("p", "b"), "p", "b", "VLESS", True, {}),
        ProxyNode(node_key("p", "c"), "p", "c", "VLESS", True, {}),
    ]
    store.upsert_nodes(nodes)
    store.record_probe(result(nodes[0], "203.0.113.10", 80))
    store.record_probe(result(nodes[1], "203.0.113.10", 40))
    store.record_probe(result(nodes[2], "203.0.113.11", 60))

    candidates = store.ranked_candidates("target", max_age_hours=1)
    assert len(candidates) == 3
    assert {candidate.egress_ip for candidate in candidates[:2]} == {
        "203.0.113.10",
        "203.0.113.11",
    }
    assert candidates[2].egress_ip == "203.0.113.10"


def test_lane_lease_replacement_is_atomic(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    nodes = [
        ProxyNode(node_key("p", "a"), "p", "a", "VLESS", True, {}),
        ProxyNode(node_key("p", "b"), "p", "b", "VLESS", True, {}),
    ]
    store.upsert_nodes(nodes)
    store.record_probe(result(nodes[0], "203.0.113.10", 80))
    store.record_probe(result(nodes[1], "203.0.113.11", 60))
    candidates = store.ranked_candidates("target", max_age_hours=1)

    store.replace_lease(1, "WORK-01", 17891, "target", candidates[0])
    store.replace_lease(1, "WORK-01", 17891, "target", candidates[1])
    leases = store.leases("target")
    assert len(leases) == 1
    assert leases[0].node_key == candidates[1].node_key
    assert leases[0].consecutive_failures == 0


def test_sync_nodes_removes_stale_inventory_and_related_probe_state(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    old = ProxyNode(node_key("old", "a"), "old", "a", "VLESS", True, {})
    current = ProxyNode(node_key("current", "b"), "current", "b", "VLESS", True, {})
    store.upsert_nodes([old])
    store.record_probe(result(old, "203.0.113.10", 80))

    store.sync_nodes([current])

    assert store.counts()["nodes"] == 1
    assert store.counts()["tested_node_targets"] == 0


def test_sync_nodes_preserves_nodes_used_by_live_lanes(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    leased = ProxyNode(node_key("old", "leased"), "old", "leased", "VLESS", True, {})
    current = ProxyNode(node_key("current", "b"), "current", "b", "VLESS", True, {})
    store.upsert_nodes([leased])
    store.record_probe(result(leased, "203.0.113.10", 80))
    candidate = store.ranked_candidates("target", max_age_hours=1)[0]
    store.replace_lease(1, "WORK-01", 17891, "target", candidate)

    store.sync_nodes([current])

    assert store.counts()["nodes"] == 2
    assert store.leases("target")[0].node_key == leased.key


def test_sync_nodes_invalidates_only_nodes_whose_stable_configuration_changed(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    unchanged = ProxyNode(
        node_key("p", "unchanged"),
        "p",
        "unchanged",
        "VLESS",
        True,
        {"config_fingerprint": "stable-a"},
    )
    changed = ProxyNode(
        node_key("p", "changed"),
        "p",
        "changed",
        "VLESS",
        True,
        {"config_fingerprint": "before"},
    )
    store.sync_nodes([unchanged, changed])
    store.record_probe(result(unchanged, "203.0.113.1", 10))
    store.record_probe(result(changed, "203.0.113.2", 10))
    changed_after = ProxyNode(
        changed.key,
        changed.provider,
        changed.name,
        changed.node_type,
        changed.alive,
        {"config_fingerprint": "after"},
    )

    store.sync_nodes([unchanged, changed_after])

    candidates = store.candidates("target", 24)
    assert [item.node_key for item in candidates] == [unchanged.key]
    assert store.counts(target="target")["probe_results"] == 2


def test_sync_nodes_can_add_a_config_fingerprint_without_invalidating_probe_state(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    node = ProxyNode(
        node_key("p", "node"),
        "p",
        "node",
        "VLESS",
        True,
        {"id": "ephemeral-before"},
    )
    store.sync_nodes([node])
    store.record_probe(result(node, "203.0.113.1", 10))
    enriched = ProxyNode(
        node.key,
        node.provider,
        node.name,
        node.node_type,
        node.alive,
        {"id": "ephemeral-after", "config_fingerprint": "stable-config"},
    )

    store.sync_nodes([enriched])

    assert [item.node_key for item in store.candidates("target", 24)] == [node.key]


def test_ephemeral_mihomo_id_change_does_not_invalidate_probe_state(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    before = ProxyNode(
        node_key("p", "node"), "p", "node", "VLESS", True, {"id": "before"}
    )
    store.sync_nodes([before])
    store.record_probe(result(before, "203.0.113.1", 10))
    after = ProxyNode(
        before.key,
        before.provider,
        before.name,
        before.node_type,
        before.alive,
        {"id": "after"},
    )

    store.sync_nodes([after])

    assert [item.node_key for item in store.candidates("target", 24)] == [before.key]


def test_subscription_then_local_proxy_then_direct_priority(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    direct = ProxyNode(
        node_key("__local__", "[local] DIRECT"),
        "__local__",
        "[local] DIRECT",
        "direct",
        True,
        {},
    )
    subscription = ProxyNode(node_key("p", "fast"), "p", "fast", "VLESS", True, {})
    local_proxy = ProxyNode(
        node_key("local", "manual"),
        "local",
        "manual",
        "VMess",
        True,
        {},
        "local",
    )
    store.upsert_nodes([direct, subscription, local_proxy])
    store.record_probe(result(direct, "203.0.113.20", 200))
    store.record_probe(result(subscription, "203.0.113.21", 20))
    store.record_probe(result(local_proxy, "203.0.113.22", 10))

    candidates = store.ranked_candidates("target", max_age_hours=1)
    assert [candidate.source_kind for candidate in candidates] == [
        "subscription",
        "local",
        "direct",
    ]


def test_target_healthy_candidate_does_not_require_egress_ip(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    node = ProxyNode(node_key("p", "unknown-ip"), "p", "unknown-ip", "VLESS", True, {})
    store.upsert_nodes([node])
    store.record_probe(result(node, None, 30))

    candidates = store.ranked_candidates("target", max_age_hours=1)

    assert len(candidates) == 1
    assert candidates[0].node_key == node.key
    assert candidates[0].egress_ip is None


def test_unknown_ips_are_not_collapsed_as_duplicates(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    nodes = [
        ProxyNode(node_key("p", "a"), "p", "a", "VLESS", True, {}),
        ProxyNode(node_key("p", "b"), "p", "b", "VLESS", True, {}),
    ]
    store.upsert_nodes(nodes)
    for node in nodes:
        store.record_probe(result(node, None, 30))

    candidates = store.ranked_candidates("target", max_age_hours=1)

    assert [candidate.node_key for candidate in candidates] == [node.key for node in nodes]


def test_counts_separate_egress_inventory_from_target_validation(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    node = ProxyNode(node_key("p", "a"), "p", "a", "VLESS", True, {})
    store.upsert_nodes([node])
    egress = result(node, "203.0.113.10", 20)
    store.record_probe(
        ProbeResult(
            **{**egress.__dict__, "target": EGRESS_TARGET},
        )
    )
    store.record_probe(
        ProbeResult(
            **{
                **egress.__dict__,
                "healthy": False,
                "status_code": 404,
                "error_type": "UnexpectedStatus",
                "error": "expected 200",
            },
        )
    )

    counts = store.counts(target="target")
    assert counts["egress_tested_nodes"] == 1
    assert counts["egress_healthy_nodes"] == 1
    assert counts["distinct_egress_ips"] == 1
    assert counts["tested_node_targets"] == 1
    assert counts["target_http_200_nodes"] == 0
    assert counts["healthy_node_targets"] == 0


def test_counts_distinguish_active_and_degraded_lanes(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    nodes = [
        ProxyNode(node_key("p", "a"), "p", "a", "VLESS", True, {}),
        ProxyNode(node_key("p", "b"), "p", "b", "VLESS", True, {}),
    ]
    store.upsert_nodes(nodes)
    for node in nodes:
        store.record_probe(result(node, None, 30))
    candidates = store.ranked_candidates("target", max_age_hours=1)
    store.replace_lease(1, "WORK-01", 17891, "target", candidates[0])
    store.replace_lease(2, "WORK-02", 17892, "target", candidates[1])
    store.record_lane_check(2, False, datetime.now(timezone.utc).isoformat())

    counts = store.counts(target="target")

    assert counts["assigned_leases"] == 2
    assert counts["active_leases"] == 1
    assert counts["degraded_leases"] == 1


def test_reset_probe_states_keeps_probe_history(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    node = ProxyNode(node_key("p", "a"), "p", "a", "VLESS", True, {})
    store.upsert_nodes([node])
    store.record_probe(result(node, "203.0.113.10", 20))

    assert store.reset_probe_states([node.key], ["target"]) == 1
    counts = store.counts(target="target")
    assert counts["tested_node_targets"] == 0
    assert counts["probe_results"] == 1


def test_probe_history_retention_keeps_current_target_snapshots(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    old_node = ProxyNode(node_key("p", "old"), "p", "old", "VLESS", True, {})
    current_node = ProxyNode(
        node_key("p", "current"),
        "p",
        "current",
        "VLESS",
        True,
        {},
    )
    store.upsert_nodes([old_node, current_node])
    old_result = replace(
        result(old_node, "203.0.113.1", 10),
        checked_at=(datetime.now(timezone.utc) - timedelta(days=120)).isoformat(
            timespec="seconds"
        ),
    )
    store.record_probe(old_result)
    store.record_probe(result(current_node, "203.0.113.2", 10))

    pruned = store.prune_probe_history(90)

    assert pruned == 1
    assert store.counts(target="target")["probe_results"] == 1
    assert store.counts(target="target")["tested_node_targets"] == 2


def test_provider_refresh_state_preserves_last_success(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")

    store.record_provider_refresh("subscription", "success", node_count=100)
    first = store.provider_refresh_states()[0]
    store.record_provider_refresh(
        "subscription",
        "cache",
        node_count=95,
        error_type="TimeoutError",
        error="refresh timed out",
        detail={"sources_cached": 3},
    )
    current = store.provider_refresh_states()[0]

    assert current["status"] == "cache"
    assert current["node_count"] == 95
    assert current["last_success_at"] == first["last_success_at"]
    assert current["detail"] == {"sources_cached": 3}


def test_pool_snapshot_separates_reserve_qualification_and_primary_lanes(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    nodes = [
        ProxyNode(node_key("p", "healthy"), "p", "healthy", "VLESS", True, {}),
        ProxyNode(node_key("p", "failed"), "p", "failed", "VLESS", True, {}),
        ProxyNode(node_key("p", "untested"), "p", "untested", "VLESS", True, {}),
        ProxyNode(node_key("p", "stale"), "p", "stale", "VLESS", True, {}),
    ]
    store.upsert_nodes(nodes)
    store.record_probe(result(nodes[0], "203.0.113.10", 20))
    store.record_probe(result(nodes[1], "203.0.113.11", 30))
    stale = result(nodes[3], "203.0.113.12", 40)
    store.record_probe(
        ProbeResult(
            **{
                **stale.__dict__,
                "checked_at": "2020-01-01T00:00:00+00:00",
            }
        )
    )
    candidates = store.ranked_candidates("target", max_age_hours=24)
    by_node = {candidate.node_key: candidate for candidate in candidates}
    store.replace_lease(1, "WORK-01", 17891, "target", by_node[nodes[0].key])
    store.replace_lease(2, "WORK-02", 17892, "target", by_node[nodes[1].key])
    store.record_probe(
        ProbeResult(
            **{
                **result(nodes[1], "203.0.113.11", 30).__dict__,
                "healthy": False,
                "status_code": 503,
                "error_type": "UnexpectedStatus",
                "error": "HTTP 503",
            }
        )
    )
    store.record_lane_check(2, False, datetime.now(timezone.utc).isoformat())

    snapshot = store.pool_snapshot("target", max_age_hours=24)

    assert snapshot["reserve_total"] == 4
    assert snapshot["reserve_qualified"] == 1
    assert snapshot["reserve_rejected"] == 1
    assert snapshot["reserve_untested"] == 1
    assert snapshot["reserve_stale"] == 1
    assert snapshot["primary_assigned"] == 2
    assert snapshot["primary_active"] == 1
    assert snapshot["primary_degraded"] == 1
