from __future__ import annotations

import plistlib
from pathlib import Path
from typing import Any


class ShadowrocketArchiveError(RuntimeError):
    pass


def _resolve(objects: list[Any], value: Any, stack: tuple[int, ...] = ()) -> Any:
    if isinstance(value, plistlib.UID):
        index = value.data
        if index < 0 or index >= len(objects):
            raise ShadowrocketArchiveError("Shadowrocket archive contains an invalid object reference")
        if index in stack:
            return None
        return _resolve(objects, objects[index], (*stack, index))
    if isinstance(value, list):
        return [_resolve(objects, item, stack) for item in value]
    if not isinstance(value, dict):
        return value

    class_name = ""
    class_ref = value.get("$class")
    if isinstance(class_ref, plistlib.UID) and class_ref.data < len(objects):
        class_value = objects[class_ref.data]
        if isinstance(class_value, dict):
            class_name = str(class_value.get("$classname") or "")

    if "NS.keys" in value and "NS.objects" in value:
        keys = [_resolve(objects, item, stack) for item in value["NS.keys"]]
        items = [_resolve(objects, item, stack) for item in value["NS.objects"]]
        return dict(zip(keys, items))
    if "NS.objects" in value:
        return [_resolve(objects, item, stack) for item in value["NS.objects"]]
    if "NS.string" in value:
        return _resolve(objects, value["NS.string"], stack)
    if class_name == "NSUUID":
        return None
    return {
        key: _resolve(objects, item, stack)
        for key, item in value.items()
        if key != "$class"
    }


def read_shadowrocket_servers(path: Path) -> list[dict[str, Any]]:
    try:
        archive = plistlib.loads(path.read_bytes())
    except FileNotFoundError as exc:
        raise ShadowrocketArchiveError(f"Shadowrocket archive does not exist: {path}") from exc
    except (OSError, plistlib.InvalidFileException) as exc:
        raise ShadowrocketArchiveError(
            f"cannot read Shadowrocket archive: {type(exc).__name__}"
        ) from exc
    objects = archive.get("$objects") if isinstance(archive, dict) else None
    if not isinstance(objects, list):
        raise ShadowrocketArchiveError("Shadowrocket archive has no object table")

    result: list[dict[str, Any]] = []
    for value in objects:
        if not isinstance(value, dict):
            continue
        class_ref = value.get("$class")
        if not isinstance(class_ref, plistlib.UID) or class_ref.data >= len(objects):
            continue
        class_value = objects[class_ref.data]
        if not isinstance(class_value, dict) or class_value.get("$classname") != "DLWServer":
            continue
        resolved = _resolve(objects, value)
        if isinstance(resolved, dict):
            result.append(resolved)
    return result


def _text(value: Any) -> str:
    return str(value or "").strip()


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(str(value or default))
    except (TypeError, ValueError):
        return default


def _websocket_options(server: dict[str, Any]) -> dict[str, Any]:
    path = _text(server.get("file")) or "/"
    host = _text(server.get("obfsParam")) or _text(server.get("peer"))
    options: dict[str, Any] = {"path": path}
    if host:
        options["headers"] = {"Host": host}
    return options


def _convert_server(server: dict[str, Any]) -> dict[str, Any] | None:
    node_type = _text(server.get("type")).casefold()
    if node_type == "subscribe":
        return None
    name = _text(server.get("title"))
    address = _text(server.get("host"))
    credential = _text(server.get("password"))
    port = _integer(server.get("port"))
    if not name or not address or not credential or not 1 <= port <= 65535:
        return None

    websocket = _text(server.get("obfs")).casefold() == "websocket"
    tls = bool(server.get("tls"))
    servername = _text(server.get("peer")) or _text(server.get("obfsParam"))

    if node_type == "vless":
        proxy: dict[str, Any] = {
            "name": name,
            "type": "vless",
            "server": address,
            "port": port,
            "uuid": credential,
            "udp": True,
            "network": "ws" if websocket else "tcp",
            "encryption": _text(server.get("method")) or "none",
        }
        if tls:
            proxy.update(
                {
                    "tls": True,
                    "servername": servername,
                    "client-fingerprint": "chrome",
                    "skip-cert-verify": bool(server.get("allowInsecure")),
                }
            )
        if websocket:
            proxy["ws-opts"] = _websocket_options(server)
        return proxy

    if node_type == "vmess":
        proxy = {
            "name": name,
            "type": "vmess",
            "server": address,
            "port": port,
            "uuid": credential,
            "alterId": _integer(server.get("alterId")),
            "cipher": "auto",
            "udp": True,
            "network": "ws" if websocket else "tcp",
        }
        if tls:
            proxy.update(
                {
                    "tls": True,
                    "servername": servername,
                    "client-fingerprint": "chrome",
                    "skip-cert-verify": bool(server.get("allowInsecure")),
                }
            )
        if websocket:
            proxy["ws-opts"] = _websocket_options(server)
        return proxy
    return None


def _server_scope(server: dict[str, Any]) -> str:
    # Imported nodes retain their parent subscription URL in Shadowrocket's data field.
    return "subscription" if _text(server.get("data")) else "local"


def shadowrocket_subscription_urls(path: Path) -> tuple[str, ...]:
    urls: list[str] = []
    for server in read_shadowrocket_servers(path):
        if _text(server.get("type")).casefold() != "subscribe":
            continue
        url = _text(server.get("host"))
        if not url.startswith(("http://", "https://")):
            continue
        if url not in urls:
            urls.append(url)
    if not urls:
        raise ShadowrocketArchiveError(
            "Shadowrocket archive contains no HTTP(S) subscription records"
        )
    return tuple(urls)


def _unique_names(proxies: list[dict[str, Any]]) -> None:
    seen: dict[str, int] = {}
    for proxy in proxies:
        original = _text(proxy.get("name")) or "node"
        count = seen.get(original, 0) + 1
        seen[original] = count
        if count > 1:
            proxy["name"] = f"{original} ({count})"


def shadowrocket_archive_to_provider(
    path: Path,
    *,
    scope: str = "all",
) -> dict[str, Any]:
    if scope not in {"all", "subscription", "local"}:
        raise ShadowrocketArchiveError(f"unsupported Shadowrocket node scope: {scope}")
    proxies = [
        converted
        for server in read_shadowrocket_servers(path)
        if scope == "all" or _server_scope(server) == scope
        if (converted := _convert_server(server)) is not None
    ]
    if not proxies and scope == "all":
        raise ShadowrocketArchiveError("Shadowrocket archive contains no supported proxy nodes")
    _unique_names(proxies)
    return {"proxies": proxies}
