from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .mihomo import ProxyNode


EGRESS_TARGET = "__egress__"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ProbeResult:
    node_key: str
    provider: str
    proxy_name: str
    target: str
    checked_at: str
    healthy: bool
    egress_ip: str | None
    latency_ms: int | None
    status_code: int | None
    error_type: str | None
    error: str | None
    detail: dict[str, Any]


@dataclass(frozen=True)
class Candidate:
    node_key: str
    provider: str
    proxy_name: str
    node_type: str
    source_kind: str
    egress_ip: str | None
    latency_ms: int | None
    checked_at: str
    successful_probes: int
    failed_probes: int


@dataclass(frozen=True)
class LaneLease:
    lane: int
    group_name: str
    port: int
    target: str
    node_key: str
    provider: str
    proxy_name: str
    egress_ip: str
    assigned_at: str
    last_verified_at: str | None
    consecutive_failures: int
    status: str


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS nodes (
    node_key TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    proxy_name TEXT NOT NULL,
    node_type TEXT NOT NULL,
    source_kind TEXT NOT NULL DEFAULT 'subscription',
    core_alive INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS probe_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_key TEXT NOT NULL REFERENCES nodes(node_key),
    target TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    healthy INTEGER NOT NULL,
    egress_ip TEXT,
    latency_ms INTEGER,
    status_code INTEGER,
    error_type TEXT,
    error TEXT,
    detail_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_probe_results_node_target
ON probe_results(node_key, target, checked_at DESC);

CREATE INDEX IF NOT EXISTS idx_probe_results_target_healthy
ON probe_results(target, healthy, checked_at DESC);

CREATE INDEX IF NOT EXISTS idx_probe_results_checked_at
ON probe_results(checked_at);

CREATE TABLE IF NOT EXISTS node_target_state (
    node_key TEXT NOT NULL REFERENCES nodes(node_key),
    target TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    healthy INTEGER NOT NULL,
    egress_ip TEXT,
    latency_ms INTEGER,
    status_code INTEGER,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    successful_probes INTEGER NOT NULL DEFAULT 0,
    failed_probes INTEGER NOT NULL DEFAULT 0,
    error_type TEXT,
    error TEXT,
    PRIMARY KEY (node_key, target)
);

CREATE TABLE IF NOT EXISTS lane_leases (
    lane INTEGER PRIMARY KEY,
    group_name TEXT NOT NULL UNIQUE,
    port INTEGER NOT NULL UNIQUE,
    target TEXT NOT NULL,
    node_key TEXT NOT NULL REFERENCES nodes(node_key),
    provider TEXT NOT NULL,
    proxy_name TEXT NOT NULL,
    egress_ip TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    last_verified_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    lane INTEGER,
    node_key TEXT,
    target TEXT,
    detail_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_refresh_state (
    provider TEXT PRIMARY KEY,
    last_attempt_at TEXT NOT NULL,
    last_success_at TEXT,
    status TEXT NOT NULL,
    node_count INTEGER,
    error_type TEXT,
    error TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
"""


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(nodes)")
            }
            if "source_kind" not in columns:
                connection.execute(
                    "ALTER TABLE nodes ADD COLUMN source_kind TEXT NOT NULL "
                    "DEFAULT 'subscription'"
                )
            connection.execute(
                "UPDATE nodes SET source_kind = 'direct' WHERE provider = '__local__'"
            )
            provider_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(provider_refresh_state)"
                )
            }
            if "detail_json" not in provider_columns:
                connection.execute(
                    "ALTER TABLE provider_refresh_state ADD COLUMN "
                    "detail_json TEXT NOT NULL DEFAULT '{}'"
                )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _node_metadata_json(node: ProxyNode) -> str:
        fingerprint = node.metadata.get("config_fingerprint")
        if isinstance(fingerprint, str) and fingerprint:
            return json.dumps(
                {"config_fingerprint": fingerprint},
                ensure_ascii=False,
                sort_keys=True,
            )
        return json.dumps(
            {
                key: node.metadata[key]
                for key in ("server", "port", "network", "tls")
                if key in node.metadata
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @staticmethod
    def _node_metadata_changed(previous: str, current: str) -> bool:
        if previous == current:
            return False
        try:
            previous_value = json.loads(previous)
            current_value = json.loads(current)
        except (json.JSONDecodeError, TypeError):
            return True
        if (
            isinstance(previous_value, dict)
            and isinstance(current_value, dict)
            and "config_fingerprint" not in previous_value
            and "config_fingerprint" in current_value
        ):
            return False
        return True

    def upsert_nodes(self, nodes: Iterable[ProxyNode]) -> int:
        now = utc_now()
        rows = list(nodes)
        with self.connect() as connection:
            for node in rows:
                source_kind = (
                    "direct" if node.provider == "__local__" else node.source_kind
                )
                connection.execute(
                    """
                    INSERT INTO nodes (
                        node_key, provider, proxy_name, node_type, source_kind, core_alive,
                        first_seen_at, last_seen_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(node_key) DO UPDATE SET
                        provider = excluded.provider,
                        proxy_name = excluded.proxy_name,
                        node_type = excluded.node_type,
                        source_kind = excluded.source_kind,
                        core_alive = excluded.core_alive,
                        last_seen_at = excluded.last_seen_at,
                        metadata_json = excluded.metadata_json
                    """,
                    (
                        node.key,
                        node.provider,
                        node.name,
                        node.node_type,
                        source_kind,
                        None if node.alive is None else int(node.alive),
                        now,
                        now,
                        self._node_metadata_json(node),
                    ),
                )
        return len(rows)

    def sync_nodes(self, nodes: Iterable[ProxyNode]) -> int:
        """Replace the current inventory while retaining history for nodes still present."""
        rows = list(nodes)
        with self.connect() as connection:
            previous_metadata = {
                str(row["node_key"]): str(row["metadata_json"])
                for row in connection.execute(
                    "SELECT node_key, metadata_json FROM nodes"
                )
            }
        self.upsert_nodes(rows)
        changed_keys = [
            node.key
            for node in rows
            if node.key in previous_metadata
            and self._node_metadata_changed(
                previous_metadata[node.key],
                self._node_metadata_json(node),
            )
        ]
        with self.connect() as connection:
            connection.execute(
                "CREATE TEMP TABLE current_inventory_keys (node_key TEXT PRIMARY KEY)"
            )
            connection.executemany(
                "INSERT INTO current_inventory_keys (node_key) VALUES (?)",
                ((node.key,) for node in rows),
            )
            # A temporary provider refresh must never remove a live work lane.
            connection.execute(
                "INSERT OR IGNORE INTO current_inventory_keys (node_key) "
                "SELECT node_key FROM lane_leases"
            )
            if changed_keys:
                placeholders = ",".join("?" for _ in changed_keys)
                connection.execute(
                    f"DELETE FROM node_target_state WHERE node_key IN ({placeholders})",
                    changed_keys,
                )
            for table in ("probe_results", "node_target_state", "nodes"):
                connection.execute(
                    f"DELETE FROM {table} WHERE node_key NOT IN "
                    "(SELECT node_key FROM current_inventory_keys)"
                )
            connection.execute("DROP TABLE current_inventory_keys")
        return len(rows)

    def record_probe(self, result: ProbeResult) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO probe_results (
                    node_key, target, checked_at, healthy, egress_ip, latency_ms,
                    status_code, error_type, error, detail_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.node_key,
                    result.target,
                    result.checked_at,
                    int(result.healthy),
                    result.egress_ip,
                    result.latency_ms,
                    result.status_code,
                    result.error_type,
                    result.error,
                    json.dumps(result.detail, ensure_ascii=False, sort_keys=True),
                ),
            )
            connection.execute(
                """
                INSERT INTO node_target_state (
                    node_key, target, checked_at, healthy, egress_ip, latency_ms,
                    status_code, consecutive_failures, successful_probes,
                    failed_probes, error_type, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_key, target) DO UPDATE SET
                    checked_at = excluded.checked_at,
                    healthy = excluded.healthy,
                    egress_ip = COALESCE(excluded.egress_ip, node_target_state.egress_ip),
                    latency_ms = excluded.latency_ms,
                    status_code = excluded.status_code,
                    consecutive_failures = CASE
                        WHEN excluded.healthy = 1 THEN 0
                        ELSE node_target_state.consecutive_failures + 1
                    END,
                    successful_probes = node_target_state.successful_probes + excluded.successful_probes,
                    failed_probes = node_target_state.failed_probes + excluded.failed_probes,
                    error_type = excluded.error_type,
                    error = excluded.error
                """,
                (
                    result.node_key,
                    result.target,
                    result.checked_at,
                    int(result.healthy),
                    result.egress_ip,
                    result.latency_ms,
                    result.status_code,
                    0 if result.healthy else 1,
                    1 if result.healthy else 0,
                    0 if result.healthy else 1,
                    result.error_type,
                    result.error,
                ),
            )

    def reset_probe_states(self, node_keys: Iterable[str], targets: Iterable[str]) -> int:
        """Clear current snapshots for a new inventory while preserving probe history."""
        keys = tuple(node_keys)
        target_names = tuple(targets)
        if not keys or not target_names:
            return 0
        key_placeholders = ",".join("?" for _ in keys)
        target_placeholders = ",".join("?" for _ in target_names)
        with self.connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM node_target_state WHERE node_key IN ({key_placeholders}) "
                f"AND target IN ({target_placeholders})",
                (*keys, *target_names),
            )
        return int(cursor.rowcount)

    def prune_probe_history(self, retention_days: float) -> int:
        threshold = (
            datetime.now(timezone.utc) - timedelta(days=retention_days)
        ).isoformat(timespec="seconds")
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM probe_results WHERE checked_at < ?",
                (threshold,),
            )
        return int(cursor.rowcount)

    def record_provider_refresh(
        self,
        provider: str,
        status: str,
        node_count: int | None = None,
        error_type: str | None = None,
        error: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        success_at = now if status in {"success", "partial"} else None
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO provider_refresh_state (
                    provider, last_attempt_at, last_success_at, status,
                    node_count, error_type, error, detail_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    last_attempt_at = excluded.last_attempt_at,
                    last_success_at = COALESCE(
                        excluded.last_success_at,
                        provider_refresh_state.last_success_at
                    ),
                    status = excluded.status,
                    node_count = excluded.node_count,
                    error_type = excluded.error_type,
                    error = excluded.error,
                    detail_json = excluded.detail_json
                """,
                (
                    provider,
                    now,
                    success_at,
                    status,
                    node_count,
                    error_type,
                    error,
                    json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
                ),
            )

    def provider_refresh_states(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM provider_refresh_state ORDER BY provider"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["detail"] = json.loads(item.pop("detail_json"))
            except (json.JSONDecodeError, TypeError):
                item["detail"] = {}
                item.pop("detail_json", None)
            result.append(item)
        return result

    def candidates(self, target: str, max_age_hours: float) -> list[Candidate]:
        threshold = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat(timespec="seconds")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT s.node_key, n.provider, n.proxy_name, n.node_type,
                       n.source_kind,
                       s.egress_ip, s.latency_ms, s.checked_at,
                       s.successful_probes, s.failed_probes
                FROM node_target_state AS s
                JOIN nodes AS n ON n.node_key = s.node_key
                WHERE s.target = ? AND s.healthy = 1
                  AND s.checked_at >= ?
                ORDER BY
                    CASE n.source_kind
                        WHEN 'subscription' THEN 0
                        WHEN 'local' THEN 1
                        WHEN 'direct' THEN 2
                        ELSE 1
                    END,
                    CASE WHEN s.egress_ip IS NULL OR s.egress_ip = '' THEN 1 ELSE 0 END,
                    s.successful_probes DESC,
                    CASE WHEN s.latency_ms IS NULL THEN 2147483647 ELSE s.latency_ms END,
                    s.checked_at DESC,
                    n.provider,
                    n.proxy_name
                """,
                (target, threshold),
            ).fetchall()
        return [Candidate(**dict(row)) for row in rows]

    def qualified_reserve_candidates(
        self,
        target: str,
        max_age_hours: float,
        occupied_ips: set[str] | None = None,
        exclude_nodes: set[str] | None = None,
    ) -> list[Candidate]:
        excluded_nodes = exclude_nodes or set()
        seen_ips = set(occupied_ips or set())
        result: list[Candidate] = []
        candidates = [
            candidate
            for candidate in self.candidates(target, max_age_hours)
            if candidate.node_key not in excluded_nodes
        ]
        source_order = {"subscription": 0, "local": 1, "direct": 2}
        source_kinds = sorted(
            {candidate.source_kind for candidate in candidates},
            key=lambda value: (source_order.get(value, 1), value),
        )
        for source_kind in source_kinds:
            preferred: list[Candidate] = []
            repeated: list[Candidate] = []
            for candidate in candidates:
                if candidate.source_kind != source_kind:
                    continue
                if candidate.egress_ip and candidate.egress_ip in seen_ips:
                    repeated.append(candidate)
                    continue
                preferred.append(candidate)
                if candidate.egress_ip:
                    seen_ips.add(candidate.egress_ip)
            result.extend(preferred)
            result.extend(repeated)
        return result

    def ranked_candidates(
        self,
        target: str,
        max_age_hours: float,
        occupied_ips: set[str] | None = None,
        exclude_nodes: set[str] | None = None,
    ) -> list[Candidate]:
        """Compatibility alias for the target-qualified reserve pool."""
        return self.qualified_reserve_candidates(
            target,
            max_age_hours,
            occupied_ips=occupied_ips,
            exclude_nodes=exclude_nodes,
        )

    def distinct_candidates(
        self,
        target: str,
        max_age_hours: float,
        exclude_ips: set[str] | None = None,
        exclude_nodes: set[str] | None = None,
    ) -> list[Candidate]:
        """Compatibility wrapper; duplicate known IPs are deprioritized, not excluded."""
        return self.qualified_reserve_candidates(
            target,
            max_age_hours,
            occupied_ips=exclude_ips,
            exclude_nodes=exclude_nodes,
        )

    def pool_snapshot(self, target: str, max_age_hours: float) -> dict[str, Any]:
        threshold = (
            datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        ).isoformat(timespec="seconds")
        with self.connect() as connection:
            reserve = connection.execute(
                """
                SELECT
                    COUNT(*) AS reserve_total,
                    SUM(CASE WHEN s.node_key IS NULL THEN 1 ELSE 0 END) AS untested,
                    SUM(CASE WHEN s.node_key IS NOT NULL AND s.checked_at < ? THEN 1 ELSE 0 END) AS stale,
                    SUM(CASE WHEN s.checked_at >= ? AND s.healthy = 1 THEN 1 ELSE 0 END) AS qualified,
                    SUM(CASE WHEN s.checked_at >= ? AND s.healthy = 0 THEN 1 ELSE 0 END) AS rejected,
                    MAX(s.checked_at) AS last_checked_at
                FROM nodes AS n
                LEFT JOIN node_target_state AS s
                  ON s.node_key = n.node_key AND s.target = ?
                """,
                (threshold, threshold, threshold, target),
            ).fetchone()
            sources = connection.execute(
                """
                SELECT n.source_kind, COUNT(*) AS count
                FROM node_target_state AS s
                JOIN nodes AS n ON n.node_key = s.node_key
                WHERE s.target = ? AND s.healthy = 1 AND s.checked_at >= ?
                GROUP BY n.source_kind
                ORDER BY n.source_kind
                """,
                (target, threshold),
            ).fetchall()
            primary = connection.execute(
                """
                SELECT
                    COUNT(*) AS assigned,
                    SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active,
                    SUM(CASE WHEN status = 'degraded' THEN 1 ELSE 0 END) AS degraded
                FROM lane_leases
                WHERE target = ?
                """,
                (target,),
            ).fetchone()
        return {
            "target": target,
            "reserve_total": int(reserve["reserve_total"] or 0),
            "reserve_qualified": int(reserve["qualified"] or 0),
            "reserve_rejected": int(reserve["rejected"] or 0),
            "reserve_stale": int(reserve["stale"] or 0),
            "reserve_untested": int(reserve["untested"] or 0),
            "qualified_by_source": {
                str(row["source_kind"]): int(row["count"])
                for row in sources
            },
            "last_checked_at": reserve["last_checked_at"],
            "primary_assigned": int(primary["assigned"] or 0),
            "primary_active": int(primary["active"] or 0),
            "primary_degraded": int(primary["degraded"] or 0),
        }

    def replace_lease(
        self,
        lane: int,
        group_name: str,
        port: int,
        target: str,
        candidate: Candidate,
        status: str = "active",
    ) -> None:
        now = utc_now()
        previous: dict[str, Any] | None = None
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM lane_leases WHERE lane = ?", (lane,)).fetchone()
            if row is not None:
                previous = dict(row)
            connection.execute(
                """
                INSERT INTO lane_leases (
                    lane, group_name, port, target, node_key, provider, proxy_name,
                    egress_ip, assigned_at, last_verified_at, consecutive_failures, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(lane) DO UPDATE SET
                    group_name = excluded.group_name,
                    port = excluded.port,
                    target = excluded.target,
                    node_key = excluded.node_key,
                    provider = excluded.provider,
                    proxy_name = excluded.proxy_name,
                    egress_ip = excluded.egress_ip,
                    assigned_at = excluded.assigned_at,
                    last_verified_at = excluded.last_verified_at,
                    consecutive_failures = 0,
                    status = excluded.status
                """,
                (
                    lane,
                    group_name,
                    port,
                    target,
                    candidate.node_key,
                    candidate.provider,
                    candidate.proxy_name,
                    candidate.egress_ip or "",
                    now,
                    candidate.checked_at,
                    status,
                ),
            )
            connection.execute(
                """
                INSERT INTO events (created_at, event_type, lane, node_key, target, detail_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    "lane_assigned" if previous is None else "lane_replaced",
                    lane,
                    candidate.node_key,
                    target,
                    json.dumps({"previous": previous, "egress_ip": candidate.egress_ip}, ensure_ascii=False),
                ),
            )

    def leases(self, target: str | None = None) -> list[LaneLease]:
        query = "SELECT * FROM lane_leases"
        params: tuple[Any, ...] = ()
        if target is not None:
            query += " WHERE target = ?"
            params = (target,)
        query += " ORDER BY lane"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [LaneLease(**dict(row)) for row in rows]

    def clear_lease(self, lane: int, reason: str = "cleared") -> None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM lane_leases WHERE lane = ?", (lane,)).fetchone()
            if row is None:
                return
            connection.execute("DELETE FROM lane_leases WHERE lane = ?", (lane,))
            connection.execute(
                """
                INSERT INTO events (created_at, event_type, lane, node_key, target, detail_json)
                VALUES (?, 'lane_cleared', ?, ?, ?, ?)
                """,
                (
                    now,
                    lane,
                    row["node_key"],
                    row["target"],
                    json.dumps({"reason": reason}, ensure_ascii=False),
                ),
            )

    def clear_all_leases(self, reason: str = "cleared") -> int:
        lanes = [lease.lane for lease in self.leases()]
        for lane in lanes:
            self.clear_lease(lane, reason)
        return len(lanes)

    def record_lane_check(self, lane: int, healthy: bool, checked_at: str) -> int:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE lane_leases SET
                    last_verified_at = ?,
                    consecutive_failures = CASE WHEN ? = 1 THEN 0 ELSE consecutive_failures + 1 END,
                    status = CASE WHEN ? = 1 THEN 'active' ELSE 'degraded' END
                WHERE lane = ?
                """,
                (checked_at, int(healthy), int(healthy), lane),
            )
            row = connection.execute(
                "SELECT consecutive_failures FROM lane_leases WHERE lane = ?",
                (lane,),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def counts(
        self,
        healthy_max_age_hours: float | None = None,
        target: str | None = None,
    ) -> dict[str, Any]:
        threshold = None
        if healthy_max_age_hours is not None:
            threshold = (
                datetime.now(timezone.utc) - timedelta(hours=healthy_max_age_hours)
            ).isoformat(timespec="seconds")
        with self.connect() as connection:
            nodes = int(connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
            direct_nodes = int(
                connection.execute(
                    "SELECT COUNT(*) FROM nodes WHERE source_kind = 'direct'"
                ).fetchone()[0]
            )
            subscription_nodes = int(
                connection.execute(
                    "SELECT COUNT(*) FROM nodes WHERE source_kind = 'subscription'"
                ).fetchone()[0]
            )
            local_proxy_nodes = int(
                connection.execute(
                    "SELECT COUNT(*) FROM nodes WHERE source_kind = 'local'"
                ).fetchone()[0]
            )
            egress_tested = int(
                connection.execute(
                    "SELECT COUNT(*) FROM node_target_state WHERE target = ?",
                    (EGRESS_TARGET,),
                ).fetchone()[0]
            )
            age_clause = " AND checked_at >= ?" if threshold is not None else ""
            params = (threshold,) if threshold is not None else ()
            egress_healthy = int(
                connection.execute(
                    "SELECT COUNT(*) FROM node_target_state "
                    "WHERE target = ? AND healthy = 1" + age_clause,
                    (EGRESS_TARGET, *params),
                ).fetchone()[0]
            )
            egress_distinct_ips = int(
                connection.execute(
                    "SELECT COUNT(DISTINCT egress_ip) FROM node_target_state "
                    "WHERE target = ? AND healthy = 1 AND egress_ip IS NOT NULL" + age_clause,
                    (EGRESS_TARGET, *params),
                ).fetchone()[0]
            )
            if target is None:
                target_clause = "target != ?"
                target_params: tuple[Any, ...] = (EGRESS_TARGET,)
            else:
                target_clause = "target = ?"
                target_params = (target,)
            tested = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM node_target_state WHERE {target_clause}",
                    target_params,
                ).fetchone()[0]
            )
            target_age_clause = " AND checked_at >= ?" if threshold is not None else ""
            target_query_params = (*target_params, *params)
            healthy = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM node_target_state WHERE {target_clause} "
                    "AND healthy = 1" + target_age_clause,
                    target_query_params,
                ).fetchone()[0]
            )
            target_http_200 = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM node_target_state WHERE {target_clause} "
                    "AND status_code = 200" + target_age_clause,
                    target_query_params,
                ).fetchone()[0]
            )
            distinct_ips = int(
                connection.execute(
                    f"SELECT COUNT(DISTINCT egress_ip) FROM node_target_state WHERE {target_clause} "
                    "AND healthy = 1 AND egress_ip IS NOT NULL" + target_age_clause,
                    target_query_params,
                ).fetchone()[0]
            )
            probes = int(connection.execute("SELECT COUNT(*) FROM probe_results").fetchone()[0])
            leases = int(connection.execute("SELECT COUNT(*) FROM lane_leases").fetchone()[0])
            active_leases = int(
                connection.execute(
                    "SELECT COUNT(*) FROM lane_leases WHERE status = 'active'"
                ).fetchone()[0]
            )
            degraded_leases = int(
                connection.execute(
                    "SELECT COUNT(*) FROM lane_leases WHERE status = 'degraded'"
                ).fetchone()[0]
            )
        return {
            "nodes": nodes,
            "subscription_nodes": subscription_nodes,
            "local_proxy_nodes": local_proxy_nodes,
            "direct_candidates": direct_nodes,
            "egress_tested_nodes": egress_tested,
            "egress_healthy_nodes": egress_healthy,
            "distinct_egress_ips": egress_distinct_ips,
            "tested_node_targets": tested,
            "healthy_node_targets": healthy,
            "target_http_200_nodes": target_http_200,
            "distinct_healthy_egress_ips": distinct_ips,
            "probe_results": probes,
            "assigned_leases": leases,
            "active_leases": active_leases,
            "degraded_leases": degraded_leases,
        }
