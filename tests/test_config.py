from pathlib import Path

import pytest
import yaml

from crawler_gateway.config import ConfigError, load_config
from crawler_gateway.controller import runtime_parameters


def valid_payload(provider_path: Path) -> dict:
    return {
        "gateway": {
            "work_lanes": 3,
            "work_port_base": 17891,
            "probe_lanes": 2,
            "probe_port_base": 17991,
            "inventory_concurrency": 2,
        },
        "ip_check_urls": ["https://example.test/ip"],
        "providers": [
            {"name": "fixture", "type": "file", "path": str(provider_path)}
        ],
        "targets": {
            "fixture": {
                "method": "POST",
                "url": "https://example.test/detail/1",
                "expected_statuses": [200],
                "json_checks": [
                    {"path": "code", "equals": "200"},
                    {"path": "data.name", "present": True},
                ],
            }
        },
    }


def write_config(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "gateway.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def test_load_valid_config(tmp_path: Path) -> None:
    provider = tmp_path / "provider.yaml"
    provider.write_text("proxies: []\n", encoding="utf-8")
    config = load_config(write_config(tmp_path, valid_payload(provider)))
    assert config.gateway.work_lanes == 3
    assert config.gateway.max_work_lanes == 6
    assert config.gateway.probe_lanes == 2
    assert config.gateway.backend == "xray"
    assert config.gateway.include_direct is True
    assert config.gateway.direct_dns_servers == ("1.1.1.1", "8.8.8.8")
    assert config.gateway.direct_outbound_interface == ""
    assert config.gateway.node_outbound_interface == ""
    assert config.gateway.target_probe_attempts == 2
    assert config.gateway.target_probe_retry_seconds == 1.0
    assert config.gateway.maintenance_error_retry_seconds == 60.0
    assert config.gateway.probe_history_retention_days == 90.0
    assert config.gateway.fault_file == "~/Desktop/爬虫故障.txt"
    assert config.providers[0].name == "fixture"
    assert config.targets["fixture"].json_checks[0].has_equals is True


def test_accept_wildcard_does_not_include_literal_quotes(tmp_path: Path) -> None:
    provider = tmp_path / "provider.yaml"
    provider.write_text("proxies: []\n", encoding="utf-8")
    payload = valid_payload(provider)
    payload["targets"]["fixture"]["headers"] = {
        "Accept": "application/json, text/plain, */*"
    }
    config = load_config(write_config(tmp_path, payload))
    assert config.targets["fixture"].headers["Accept"].endswith("*/*")
    assert '"' not in config.targets["fixture"].headers["Accept"]


def test_inventory_concurrency_cannot_exceed_probe_lanes(tmp_path: Path) -> None:
    provider = tmp_path / "provider.yaml"
    provider.write_text("proxies: []\n", encoding="utf-8")
    payload = valid_payload(provider)
    payload["gateway"]["inventory_concurrency"] = 3
    with pytest.raises(ConfigError, match="cannot exceed"):
        load_config(write_config(tmp_path, payload))


def test_work_lanes_can_expand_to_default_safety_limit(tmp_path: Path) -> None:
    provider = tmp_path / "provider.yaml"
    provider.write_text("proxies: []\n", encoding="utf-8")
    payload = valid_payload(provider)
    payload["gateway"]["work_lanes"] = 6

    config = load_config(write_config(tmp_path, payload))

    assert config.gateway.work_lanes == 6
    assert config.gateway.work_port_base + config.gateway.work_lanes - 1 == 17896


def test_work_lanes_cannot_exceed_configured_safety_limit(tmp_path: Path) -> None:
    provider = tmp_path / "provider.yaml"
    provider.write_text("proxies: []\n", encoding="utf-8")
    payload = valid_payload(provider)
    payload["gateway"]["work_lanes"] = 7

    with pytest.raises(ConfigError, match="max_work_lanes"):
        load_config(write_config(tmp_path, payload))


def test_safety_limit_can_be_raised_deliberately(tmp_path: Path) -> None:
    provider = tmp_path / "provider.yaml"
    provider.write_text("proxies: []\n", encoding="utf-8")
    payload = valid_payload(provider)
    payload["gateway"].update({"work_lanes": 8, "max_work_lanes": 8})

    config = load_config(write_config(tmp_path, payload))

    assert config.gateway.work_lanes == 8
    assert config.gateway.max_work_lanes == 8


def test_multiple_maintenance_targets_are_configurable(tmp_path: Path) -> None:
    provider = tmp_path / "provider.yaml"
    provider.write_text("proxies: []\n", encoding="utf-8")
    payload = valid_payload(provider)
    payload["targets"]["second"] = {
        "method": "GET",
        "url": "https://second.example.test/health",
        "expected_statuses": [200, 204],
    }
    payload["gateway"].update(
        {
            "maintenance_targets": ["fixture", "second"],
            "reserve_refresh_interval_seconds": 1800,
        }
    )

    config = load_config(write_config(tmp_path, payload))

    assert config.gateway.maintenance_targets == ("fixture", "second")
    assert config.gateway.reserve_refresh_interval_seconds == 1800


def test_unknown_maintenance_target_is_rejected(tmp_path: Path) -> None:
    provider = tmp_path / "provider.yaml"
    provider.write_text("proxies: []\n", encoding="utf-8")
    payload = valid_payload(provider)
    payload["gateway"]["maintenance_targets"] = ["missing"]

    with pytest.raises(ConfigError, match="unknown targets"):
        load_config(write_config(tmp_path, payload))


def test_reserve_refresh_interval_must_be_positive(tmp_path: Path) -> None:
    provider = tmp_path / "provider.yaml"
    provider.write_text("proxies: []\n", encoding="utf-8")
    payload = valid_payload(provider)
    payload["gateway"]["reserve_refresh_interval_seconds"] = 0

    with pytest.raises(ConfigError, match="greater than 0"):
        load_config(write_config(tmp_path, payload))


def test_maintenance_error_retry_interval_must_be_positive(tmp_path: Path) -> None:
    provider = tmp_path / "provider.yaml"
    provider.write_text("proxies: []\n", encoding="utf-8")
    payload = valid_payload(provider)
    payload["gateway"]["maintenance_error_retry_seconds"] = 0

    with pytest.raises(ConfigError, match="maintenance_error_retry_seconds"):
        load_config(write_config(tmp_path, payload))


def test_probe_history_retention_must_be_positive(tmp_path: Path) -> None:
    provider = tmp_path / "provider.yaml"
    provider.write_text("proxies: []\n", encoding="utf-8")
    payload = valid_payload(provider)
    payload["gateway"]["probe_history_retention_days"] = 0

    with pytest.raises(ConfigError, match="probe_history_retention_days"):
        load_config(write_config(tmp_path, payload))


def test_runtime_parameters_print_structure_without_provider_or_header_secrets(
    tmp_path: Path,
) -> None:
    payload = valid_payload(tmp_path / "unused.yaml")
    payload["providers"] = [
        {
            "name": "remote",
            "type": "http",
            "url": "https://user:secret@example.test/subscription",
        }
    ]
    payload["targets"]["fixture"]["headers"] = {
        "Authorization": "Bearer hidden-token"
    }
    config = load_config(write_config(tmp_path, payload))

    report = runtime_parameters(config)
    serialized = str(report)

    assert report["providers"][0]["name"] == "remote"
    assert report["targets"][0]["header_names"] == ["Authorization"]
    assert "hidden-token" not in serialized
    assert "user:secret" not in serialized


def test_provider_url_must_be_http(tmp_path: Path) -> None:
    payload = valid_payload(tmp_path / "unused.yaml")
    payload["providers"] = [{"name": "bad", "type": "http", "url": "secret"}]
    with pytest.raises(ConfigError, match="requires an HTTP"):
        load_config(write_config(tmp_path, payload))


def test_shadowrocket_provider_requires_archive_path(tmp_path: Path) -> None:
    payload = valid_payload(tmp_path / "unused.yaml")
    payload["providers"] = [{"name": "shadowrocket", "type": "shadowrocket"}]
    with pytest.raises(ConfigError, match="requires path"):
        load_config(write_config(tmp_path, payload))


def test_shadowrocket_provider_accepts_node_scope(tmp_path: Path) -> None:
    archive = tmp_path / "ServerManager"
    archive.write_bytes(b"fixture")
    payload = valid_payload(tmp_path / "unused.yaml")
    payload["providers"] = [
        {
            "name": "subscriptions",
            "type": "shadowrocket",
            "path": str(archive),
            "scope": "subscription",
        }
    ]

    config = load_config(write_config(tmp_path, payload))

    assert config.providers[0].scope == "subscription"


def test_shadowrocket_subscription_provider_can_enable_remote_refresh(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "ServerManager"
    archive.write_bytes(b"fixture")
    payload = valid_payload(tmp_path / "unused.yaml")
    payload["providers"] = [
        {
            "name": "subscriptions",
            "type": "shadowrocket",
            "path": str(archive),
            "scope": "subscription",
            "refresh_remote": True,
        }
    ]

    config = load_config(write_config(tmp_path, payload))

    assert config.providers[0].refresh_remote is True


def test_remote_refresh_requires_shadowrocket_subscription_scope(tmp_path: Path) -> None:
    payload = valid_payload(tmp_path / "unused.yaml")
    payload["providers"] = [
        {
            "name": "bad",
            "type": "file",
            "path": str(tmp_path / "provider.yaml"),
            "refresh_remote": True,
        }
    ]

    with pytest.raises(ConfigError, match="refresh_remote"):
        load_config(write_config(tmp_path, payload))


def test_direct_mode_requires_dns_servers(tmp_path: Path) -> None:
    provider = tmp_path / "provider.yaml"
    provider.write_text("proxies: []\n", encoding="utf-8")
    payload = valid_payload(provider)
    payload["gateway"]["include_direct"] = True
    payload["gateway"]["direct_dns_servers"] = []
    with pytest.raises(ConfigError, match="direct_dns_servers"):
        load_config(write_config(tmp_path, payload))
