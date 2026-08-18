from pathlib import Path

import yaml

from crawler_gateway.config import GatewaySettings, ProviderConfig
from crawler_gateway.mihomo import ProxyNode, node_key
from crawler_gateway.paths import ProjectPaths
from crawler_gateway.xray import XrayProcess, _matching_lane_pids, parse_fragment, xray_config_for_node


def node() -> ProxyNode:
    metadata = {
        "name": "node",
        "type": "vless",
        "server": "203.0.113.10",
        "port": 443,
        "uuid": "12345678-1234-1234-1234-123456789abc",
        "network": "ws",
        "encryption": "none",
        "tls": True,
        "servername": "edge.example.test",
        "client-fingerprint": "chrome",
        "ws-opts": {"path": "/ws", "headers": {"Host": "edge.example.test"}},
        "_xray_fragment": "1,40-60,30-50,tlshello",
    }
    return ProxyNode(node_key("p", "node"), "p", "node", "vless", None, metadata)


def test_fragment_parameter() -> None:
    assert parse_fragment("1,40-60,30-50,tlshello") == {
        "packets": "tlshello",
        "length": "40-60",
        "delay": "30-50",
    }
    assert parse_fragment("0,40-60,30-50,tlshello") is None


def test_xray_config_has_fixed_http_lane_and_ignores_client_specific_fragment() -> None:
    payload = xray_config_for_node(
        node(),
        listen="127.0.0.1",
        port=17891,
        outbound_interface="en0",
    )
    assert payload["inbounds"][0]["port"] == 17891
    outbound = payload["outbounds"][0]
    assert outbound["settings"]["address"] == "203.0.113.10"
    assert outbound["streamSettings"]["network"] == "ws"
    assert outbound["streamSettings"]["sockopt"]["interface"] == "en0"
    assert "finalmask" not in outbound["streamSettings"]


def test_xray_config_supports_local_direct_candidate() -> None:
    direct = ProxyNode(
        node_key("__local__", "[local] DIRECT"),
        "__local__",
        "[local] DIRECT",
        "direct",
        None,
        {"type": "direct"},
    )
    payload = xray_config_for_node(
        direct,
        listen="127.0.0.1",
        port=17891,
        outbound_interface="en0",
        direct_dns_servers=("192.168.0.1",),
    )
    outbound = payload["outbounds"][0]
    assert outbound["protocol"] == "freedom"
    assert outbound["settings"]["domainStrategy"] == "UseIPv4"
    assert outbound["streamSettings"]["sockopt"]["interface"] == "en0"
    assert payload["dns"]["servers"][0]["address"] == "192.168.0.1"


def test_provider_count_uses_cached_nodes_without_refresh(tmp_path: Path) -> None:
    paths = ProjectPaths(tmp_path)
    paths.ensure_directories()
    provider = ProviderConfig(
        name="cached",
        type="http",
        url="https://example.invalid/subscription",
    )
    config = type(
        "Config",
        (),
        {
            "providers": (provider,),
            "gateway": GatewaySettings(),
        },
    )()
    process = XrayProcess(paths, config)
    path = process.provider_path(provider)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"proxies": [{"name": "a"}, {"name": "b"}]}),
        encoding="utf-8",
    )

    assert process.provider_count(provider) == 2


def test_matching_lane_pids_only_returns_exact_config_processes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = tmp_path / "probe-01" / "config.json"

    class Result:
        stdout = (
            f"101 /opt/homebrew/bin/xray run -config {config}\n"
            f"102 /opt/homebrew/bin/xray run -config {tmp_path / 'probe-02' / 'config.json'}\n"
        )

    monkeypatch.setattr("crawler_gateway.xray.subprocess.run", lambda *_args, **_kwargs: Result())
    assert _matching_lane_pids(config) == [101]
