from __future__ import annotations

import plistlib
from pathlib import Path

from crawler_gateway.shadowrocket import (
    shadowrocket_archive_to_provider,
    shadowrocket_subscription_urls,
)


def _archive(path: Path, *, subscription: bool = False) -> None:
    objects = [
        "$null",
        {"NS.objects": [plistlib.UID(2)], "$class": plistlib.UID(10)},
        {
            "title": plistlib.UID(3),
            "type": plistlib.UID(4),
            "host": plistlib.UID(5),
            "port": plistlib.UID(6),
            "password": plistlib.UID(7),
            "tls": True,
            "peer": plistlib.UID(5),
            "obfs": plistlib.UID(8),
            "obfsParam": plistlib.UID(5),
            "file": plistlib.UID(9),
            "$class": plistlib.UID(11),
        },
        "positive-control",
        "Vmess",
        "edge.example.test",
        "443",
        "12345678-1234-1234-1234-123456789abc",
        "websocket",
        "/ws",
        {"$classname": "NSMutableArray", "$classes": ["NSMutableArray", "NSArray", "NSObject"]},
        {"$classname": "DLWServer", "$classes": ["DLWServer", "NSObject"]},
    ]
    if subscription:
        objects[2]["data"] = plistlib.UID(len(objects))
        objects.append("https://subscription.example.test/list")
    payload = {
        "$version": 100000,
        "$archiver": "NSKeyedArchiver",
        "$top": {"root": plistlib.UID(1)},
        "$objects": objects,
    }
    path.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_BINARY))


def _subscription_record_archive(path: Path) -> None:
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
    payload = {
        "$version": 100000,
        "$archiver": "NSKeyedArchiver",
        "$top": {"root": plistlib.UID(1)},
        "$objects": objects,
    }
    path.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_BINARY))


def test_converts_shadowrocket_vmess_archive(tmp_path: Path) -> None:
    path = tmp_path / "ServerManager"
    _archive(path)
    provider = shadowrocket_archive_to_provider(path)
    assert provider == {
        "proxies": [
            {
                "name": "positive-control",
                "type": "vmess",
                "server": "edge.example.test",
                "port": 443,
                "uuid": "12345678-1234-1234-1234-123456789abc",
                "alterId": 0,
                "cipher": "auto",
                "udp": True,
                "network": "ws",
                "tls": True,
                "servername": "edge.example.test",
                "client-fingerprint": "chrome",
                "skip-cert-verify": False,
                "ws-opts": {
                    "path": "/ws",
                    "headers": {"Host": "edge.example.test"},
                },
            }
        ]
    }


def test_shadowrocket_scope_separates_subscription_and_local_nodes(tmp_path: Path) -> None:
    local_path = tmp_path / "LocalServerManager"
    subscription_path = tmp_path / "SubscriptionServerManager"
    _archive(local_path)
    _archive(subscription_path, subscription=True)

    assert len(shadowrocket_archive_to_provider(local_path, scope="local")["proxies"]) == 1
    assert shadowrocket_archive_to_provider(local_path, scope="subscription") == {"proxies": []}
    assert len(
        shadowrocket_archive_to_provider(subscription_path, scope="subscription")["proxies"]
    ) == 1
    assert shadowrocket_archive_to_provider(subscription_path, scope="local") == {"proxies": []}


def test_reads_subscription_urls_without_exposing_node_credentials(tmp_path: Path) -> None:
    path = tmp_path / "ServerManager"
    _subscription_record_archive(path)

    assert shadowrocket_subscription_urls(path) == (
        "https://subscription.example.test/list",
    )
