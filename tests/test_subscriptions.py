from __future__ import annotations

import base64
import plistlib
from pathlib import Path

import pytest
import requests
import yaml

from crawler_gateway.config import ProviderConfig
from crawler_gateway.subscriptions import (
    BACKGROUND_SERVICE_ENV,
    SubscriptionError,
    materialize_provider,
    subscription_to_provider,
)


def _shadowrocket_subscription_archive(path: Path) -> None:
    objects = [
        "$null",
        {"NS.objects": [plistlib.UID(2)], "$class": plistlib.UID(5)},
        {
            "type": plistlib.UID(3),
            "host": plistlib.UID(4),
            "$class": plistlib.UID(6),
        },
        "Subscribe",
        "https://subscription.example.test/list",
        {"$classname": "NSMutableArray", "$classes": ["NSMutableArray", "NSArray", "NSObject"]},
        {"$classname": "DLWServer", "$classes": ["DLWServer", "NSObject"]},
    ]
    path.write_bytes(
        plistlib.dumps(
            {
                "$version": 100000,
                "$archiver": "NSKeyedArchiver",
                "$top": {"root": plistlib.UID(1)},
                "$objects": objects,
            },
            fmt=plistlib.FMT_BINARY,
        )
    )


def test_converts_base64_vless_websocket_tls_subscription() -> None:
    uri = (
        "vless://12345678-1234-1234-1234-123456789abc@example.com:443"
        "?type=ws&security=tls&sni=edge.example.com&fp=chrome&fragment=1%2C40-60%2C30-50%2Ctlshello"
        "&host=edge.example.com&path=%2Fws%3Fed%3D2560#CF%20node"
    )
    payload = base64.b64encode(uri.encode("utf-8"))

    provider = subscription_to_provider(payload, "coffer")

    assert provider == {
        "proxies": [
            {
                "name": "CF node",
                "type": "vless",
                "server": "example.com",
                "port": 443,
                "uuid": "12345678-1234-1234-1234-123456789abc",
                "udp": True,
                "network": "ws",
                "encryption": "none",
                "tls": True,
                "servername": "edge.example.com",
                "client-fingerprint": "chrome",
                "ws-opts": {
                    "path": "/ws?ed=2560",
                    "headers": {"Host": "edge.example.com"},
                },
                "_xray_fragment": "1,40-60,30-50,tlshello",
            }
        ]
    }


def test_rejects_unsupported_uri_scheme() -> None:
    with pytest.raises(SubscriptionError, match="unsupported node types: trojan"):
        subscription_to_provider(b"trojan://secret@example.com:443#node", "provider")


def test_deduplicates_node_names() -> None:
    first = "vless://uuid@example.com:443?type=ws#same"
    second = "vless://uuid@example.net:443?type=ws#same"
    provider = subscription_to_provider(f"{first}\n{second}".encode(), "provider")
    assert [item["name"] for item in provider["proxies"]] == ["same", "same (2)"]


def test_shadowrocket_provider_can_refresh_hidden_remote_subscriptions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "ServerManager"
    destination = tmp_path / "provider.yaml"
    _shadowrocket_subscription_archive(source)
    uri = "vless://uuid@example.test:443?type=ws#remote-node"

    class Response:
        content = uri.encode()

        def raise_for_status(self):
            return None

    class Session:
        trust_env = True

        def get(self, url, **_kwargs):
            assert url == "https://subscription.example.test/list"
            return Response()

    monkeypatch.setattr("crawler_gateway.subscriptions.requests.Session", Session)
    provider = ProviderConfig(
        name="shadowrocket",
        type="shadowrocket",
        path=str(source),
        scope="subscription",
        refresh_remote=True,
    )
    config = type("Config", (), {"path": tmp_path / "gateway.yaml"})()

    result = materialize_provider(provider, config, destination)
    payload = yaml.safe_load(destination.read_text(encoding="utf-8"))

    assert result.node_count == 1
    assert result.status == "success"
    assert payload["proxies"][0]["name"] == "remote-node"


def test_background_refresh_uses_cached_subscription_source_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "ServerManager"
    destination = tmp_path / "provider.yaml"
    _shadowrocket_subscription_archive(source)

    class Response:
        content = b"vless://uuid@example.test:443?type=ws#remote-node"

        def raise_for_status(self):
            return None

    class Session:
        trust_env = True

        def get(self, _url, **_kwargs):
            return Response()

    monkeypatch.setattr("crawler_gateway.subscriptions.requests.Session", Session)
    provider = ProviderConfig(
        name="shadowrocket",
        type="shadowrocket",
        path=str(source),
        scope="subscription",
        refresh_remote=True,
    )
    config = type("Config", (), {"path": tmp_path / "gateway.yaml"})()
    materialize_provider(provider, config, destination)
    monkeypatch.setenv(BACKGROUND_SERVICE_ENV, "1")
    monkeypatch.setattr(
        "crawler_gateway.subscriptions.shadowrocket_subscription_urls",
        lambda _source: pytest.fail("background refresh must use the secure manifest"),
    )

    refreshed = materialize_provider(provider, config, destination)
    manifests = list(
        (destination.parent / ".subscription-source-cache").rglob("sources.json")
    )

    assert refreshed.node_count == 1
    assert refreshed.sources_fresh == 1
    assert len(manifests) == 1
    assert manifests[0].stat().st_mode & 0o777 == 0o600


def test_shadowrocket_remote_sources_refresh_independently_with_per_source_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "ServerManager"
    source.write_bytes(b"fixture")
    destination = tmp_path / "provider.yaml"
    urls = tuple(f"https://subscription.example.test/{name}" for name in "abc")
    url_state = {"value": urls}
    monkeypatch.setattr(
        "crawler_gateway.subscriptions.shadowrocket_subscription_urls",
        lambda _source: url_state["value"],
    )
    phase = {"value": 1}

    class Response:
        def __init__(self, content: bytes) -> None:
            self.content = content

        def raise_for_status(self):
            return None

    class Session:
        trust_env = True

        def get(self, url, **_kwargs):
            name = url.rsplit("/", 1)[-1]
            if phase["value"] == 2 and name == "b":
                raise requests.Timeout("temporary failure")
            suffix = "old" if phase["value"] == 1 else "new"
            uri = f"vless://uuid-{name}@{name}.example.test:443?type=ws#{name}-{suffix}"
            return Response(uri.encode())

    monkeypatch.setattr("crawler_gateway.subscriptions.requests.Session", Session)
    provider = ProviderConfig(
        name="shadowrocket",
        type="shadowrocket",
        path=str(source),
        scope="subscription",
        refresh_remote=True,
    )
    config = type("Config", (), {"path": tmp_path / "gateway.yaml"})()

    initial = materialize_provider(provider, config, destination)
    phase["value"] = 2
    updated = materialize_provider(provider, config, destination)
    payload = yaml.safe_load(destination.read_text(encoding="utf-8"))
    names = [item["name"] for item in payload["proxies"]]

    assert initial.status == "success"
    assert updated.status == "partial"
    assert updated.sources_fresh == 2
    assert updated.sources_cached == 1
    assert updated.sources_failed == 0
    assert names == ["a-new", "b-old", "c-new"]

    url_state["value"] = urls[:2]
    final = materialize_provider(provider, config, destination)
    cache_files = list(
        (destination.parent / ".subscription-source-cache").rglob("*.yaml")
    )

    assert final.node_count == 2
    assert len(cache_files) == 2
