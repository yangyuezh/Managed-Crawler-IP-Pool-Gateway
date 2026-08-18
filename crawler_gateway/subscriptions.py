from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import requests
import yaml

from .config import AppConfig, ProviderConfig
from .shadowrocket import (
    ShadowrocketArchiveError,
    shadowrocket_archive_to_provider,
    shadowrocket_subscription_urls,
)


class SubscriptionError(RuntimeError):
    pass


@dataclass(frozen=True)
class MaterializeResult:
    node_count: int
    status: str = "success"
    sources_total: int = 1
    sources_fresh: int = 1
    sources_cached: int = 0
    sources_failed: int = 0
    error_types: tuple[str, ...] = ()


BACKGROUND_SERVICE_ENV = "CRAWLER_GATEWAY_BACKGROUND_SERVICE"


def _first(values: dict[str, list[str]], *keys: str, default: str = "") -> str:
    for key in keys:
        items = values.get(key)
        if items:
            return items[0]
    return default


def _decode_subscription(payload: bytes, provider_name: str) -> str:
    raw = payload.strip()
    if not raw:
        raise SubscriptionError(f"provider {provider_name!r} returned an empty subscription")
    text = raw.decode("utf-8", "replace").lstrip("\ufeff").strip()
    if "://" in text or text.startswith(("proxies:", "---")):
        return text
    padded = raw + b"=" * (-len(raw) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            decoded = decoder(padded).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if "://" in decoded or decoded.lstrip().startswith(("proxies:", "---")):
            return decoded.lstrip("\ufeff").strip()
    raise SubscriptionError(
        f"provider {provider_name!r} returned an unsupported subscription encoding"
    )


def _vless_proxy(uri: str, fallback_name: str) -> dict[str, Any]:
    parsed = urlsplit(uri)
    if not parsed.hostname or parsed.port is None or not parsed.username:
        raise SubscriptionError("VLESS entry is missing server, port, or UUID")
    query = parse_qs(parsed.query, keep_blank_values=True)
    name = unquote(parsed.fragment).strip() or fallback_name
    network = _first(query, "type", "network", default="tcp").lower()
    security = _first(query, "security", default="none").lower()
    proxy: dict[str, Any] = {
        "name": name,
        "type": "vless",
        "server": parsed.hostname,
        "port": parsed.port,
        "uuid": unquote(parsed.username),
        "udp": True,
        "network": network,
        "encryption": _first(query, "encryption", default="none"),
    }
    if security in {"tls", "reality"}:
        proxy["tls"] = True
        servername = _first(query, "sni", "servername")
        if servername:
            proxy["servername"] = servername
        fingerprint = _first(query, "fp", "client-fingerprint")
        if fingerprint:
            proxy["client-fingerprint"] = fingerprint
    if security == "reality":
        proxy["reality-opts"] = {
            key: value
            for key, value in {
                "public-key": _first(query, "pbk", "public-key"),
                "short-id": _first(query, "sid", "short-id"),
            }.items()
            if value
        }
    if network == "ws":
        ws_options: dict[str, Any] = {}
        path = _first(query, "path")
        if path:
            ws_options["path"] = path
        host = _first(query, "host")
        if host:
            ws_options["headers"] = {"Host": host}
        if ws_options:
            proxy["ws-opts"] = ws_options
    elif network == "grpc":
        service_name = _first(query, "serviceName", "service-name")
        if service_name:
            proxy["grpc-opts"] = {"grpc-service-name": service_name}
    flow = _first(query, "flow")
    if flow:
        proxy["flow"] = flow
    fragment = _first(query, "fragment")
    if fragment:
        proxy["_xray_fragment"] = fragment
    return proxy


def _unique_names(proxies: list[dict[str, Any]]) -> None:
    seen: dict[str, int] = {}
    for proxy in proxies:
        original = str(proxy.get("name") or "node").strip() or "node"
        count = seen.get(original, 0) + 1
        seen[original] = count
        if count > 1:
            proxy["name"] = f"{original} ({count})"


def subscription_to_provider(payload: bytes, provider_name: str) -> dict[str, Any]:
    text = _decode_subscription(payload, provider_name)
    try:
        yaml_value = yaml.safe_load(text)
    except yaml.YAMLError:
        yaml_value = None
    if isinstance(yaml_value, dict) and isinstance(yaml_value.get("proxies"), list):
        proxies = [item for item in yaml_value["proxies"] if isinstance(item, dict)]
    else:
        proxies = []
        unsupported: set[str] = set()
        for index, line in enumerate(text.splitlines(), start=1):
            uri = line.strip()
            if not uri or uri.startswith("#"):
                continue
            scheme = urlsplit(uri).scheme.lower()
            if scheme == "vless":
                proxies.append(_vless_proxy(uri, f"{provider_name}-{index:03d}"))
            else:
                unsupported.add(scheme or "unknown")
        if unsupported:
            kinds = ", ".join(sorted(unsupported))
            raise SubscriptionError(
                f"provider {provider_name!r} contains unsupported node types: {kinds}"
            )
    if not proxies:
        raise SubscriptionError(f"provider {provider_name!r} contains no proxy nodes")
    _unique_names(proxies)
    return {"proxies": proxies}


def _atomic_yaml_write(destination: Path, payload: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.parent.chmod(0o700)
    except OSError:
        pass
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    destination.chmod(0o600)


def _download_subscription(
    *,
    url: str,
    provider_name: str,
    user_agent: str,
    source_number: int | None = None,
) -> bytes:
    session = requests.Session()
    session.trust_env = False
    try:
        try:
            response = session.get(
                url,
                headers={"User-Agent": user_agent, "Accept": "*/*"},
                timeout=30,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            source = f" source {source_number}" if source_number is not None else ""
            raise SubscriptionError(
                f"provider {provider_name!r}{source} subscription download failed: "
                f"{type(exc).__name__}"
            ) from exc
        return response.content
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            close()


def _cached_source(path: Path) -> dict[str, Any] | None:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    proxies = payload.get("proxies") if isinstance(payload, dict) else None
    return payload if isinstance(proxies, list) and bool(proxies) else None


def _source_cache_directory(destination: Path, provider_name: str) -> Path:
    provider_key = hashlib.sha256(provider_name.encode("utf-8")).hexdigest()[:16]
    return destination.parent / ".subscription-source-cache" / provider_key


def _source_cache_path(destination: Path, provider_name: str, url: str) -> Path:
    source_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return _source_cache_directory(destination, provider_name) / f"{source_key}.yaml"


def _source_manifest_path(destination: Path, provider_name: str) -> Path:
    return _source_cache_directory(destination, provider_name) / "sources.json"


def _cached_subscription_urls(
    destination: Path,
    provider_name: str,
) -> tuple[str, ...]:
    path = _source_manifest_path(destination, provider_name)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    values = payload.get("urls") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        return ()
    return tuple(
        dict.fromkeys(
            value.strip()
            for value in values
            if isinstance(value, str)
            and value.strip().startswith(("http://", "https://"))
        )
    )


def _write_subscription_urls(
    destination: Path,
    provider_name: str,
    urls: tuple[str, ...],
) -> None:
    path = _source_manifest_path(destination, provider_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps({"urls": list(urls)}, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)
    path.chmod(0o600)


def _refresh_shadowrocket_subscriptions(
    source: Path,
    provider: ProviderConfig,
    destination: Path,
    *,
    prefer_cached_urls: bool,
) -> tuple[dict[str, Any], MaterializeResult]:
    cached_urls = _cached_subscription_urls(destination, provider.name)
    urls = cached_urls if prefer_cached_urls else ()
    if not urls:
        try:
            urls = shadowrocket_subscription_urls(source)
        except ShadowrocketArchiveError as exc:
            if not cached_urls:
                raise SubscriptionError(str(exc)) from exc
            urls = cached_urls
        else:
            _write_subscription_urls(destination, provider.name, urls)
    proxies: list[dict[str, Any]] = []
    fresh = 0
    cached = 0
    failed = 0
    error_types: list[str] = []
    active_cache_paths: set[Path] = set()
    for index, url in enumerate(urls, start=1):
        cache_path = _source_cache_path(destination, provider.name, url)
        active_cache_paths.add(cache_path)
        try:
            payload = _download_subscription(
                url=url,
                provider_name=provider.name,
                user_agent=provider.user_agent,
                source_number=index,
            )
            converted = subscription_to_provider(payload, f"{provider.name}-{index}")
            _atomic_yaml_write(cache_path, converted)
            fresh += 1
        except Exception as exc:  # noqa: BLE001
            error_types.append(type(exc).__name__)
            converted = _cached_source(cache_path)
            if converted is None:
                failed += 1
                continue
            cached += 1
        proxies.extend(converted["proxies"])
    _unique_names(proxies)
    if not proxies:
        raise SubscriptionError(
            f"provider {provider.name!r} remote refresh returned no proxy nodes"
        )
    cache_directory = next(iter(active_cache_paths)).parent if active_cache_paths else None
    if cache_directory is not None:
        for old_cache in cache_directory.glob("*.yaml"):
            if old_cache not in active_cache_paths:
                old_cache.unlink(missing_ok=True)
    if fresh == len(urls):
        status = "success"
    elif fresh == 0 and cached == len(urls):
        status = "cache"
    else:
        status = "partial"
    result = MaterializeResult(
        node_count=len(proxies),
        status=status,
        sources_total=len(urls),
        sources_fresh=fresh,
        sources_cached=cached,
        sources_failed=failed,
        error_types=tuple(sorted(set(error_types))),
    )
    return {"proxies": proxies}, result


def materialize_provider(
    provider: ProviderConfig,
    config: AppConfig,
    destination: Path,
) -> MaterializeResult:
    if provider.type in {"file", "shadowrocket"}:
        source = Path(provider.path).expanduser()
        if not source.is_absolute():
            source = config.path.parent / source
        source = source.resolve()
        if not source.is_file():
            raise SubscriptionError(f"provider file does not exist: {source}")
        if provider.type == "shadowrocket":
            if provider.refresh_remote:
                converted, result = _refresh_shadowrocket_subscriptions(
                    source,
                    provider,
                    destination,
                    prefer_cached_urls=(
                        os.environ.get(BACKGROUND_SERVICE_ENV) == "1"
                    ),
                )
            else:
                converted = (
                    _cached_source(destination)
                    if os.environ.get(BACKGROUND_SERVICE_ENV) == "1"
                    else None
                )
                if converted is None:
                    try:
                        converted = shadowrocket_archive_to_provider(
                            source,
                            scope=provider.scope,
                        )
                    except ShadowrocketArchiveError as exc:
                        raise SubscriptionError(str(exc)) from exc
                    result = MaterializeResult(node_count=len(converted["proxies"]))
                else:
                    result = MaterializeResult(
                        node_count=len(converted["proxies"]),
                        sources_fresh=0,
                        sources_cached=1,
                    )
            _atomic_yaml_write(destination, converted)
            return result
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        destination.chmod(0o600)
        value = yaml.safe_load(destination.read_text(encoding="utf-8")) or {}
        count = len(value.get("proxies") or []) if isinstance(value, dict) else 0
        return MaterializeResult(node_count=count)

    payload = _download_subscription(
        url=provider.url,
        provider_name=provider.name,
        user_agent=provider.user_agent,
    )
    converted = subscription_to_provider(payload, provider.name)
    _atomic_yaml_write(destination, converted)
    return MaterializeResult(node_count=len(converted["proxies"]))
