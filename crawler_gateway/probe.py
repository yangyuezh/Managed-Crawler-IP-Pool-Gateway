from __future__ import annotations

import ipaddress
import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

from .config import AppConfig, TargetConfig
from .mihomo import MihomoApi, ProxyNode
from .state import EGRESS_TARGET, ProbeResult


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class TargetResponse:
    healthy: bool
    status_code: int | None
    latency_ms: int | None
    error_type: str | None
    error: str | None
    detail: dict[str, Any]


@dataclass(frozen=True)
class NodeProbeOutcome:
    egress: ProbeResult
    target: ProbeResult | None


def proxy_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def _session(proxy: str) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.proxies.update({"http": proxy, "https": proxy})
    return session


@dataclass(frozen=True)
class CurlResponse:
    status_code: int
    headers: dict[str, str]
    content: bytes
    elapsed_ms: int

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.text)


def _curl_request(
    *,
    proxy: str,
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    form: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: float,
) -> CurlResponse:
    curl = shutil.which("curl")
    if curl is None:
        raise RuntimeError("curl is unavailable")
    with tempfile.TemporaryDirectory(prefix="crawler-gateway-curl-") as directory:
        body_path = f"{directory}/body"
        header_path = f"{directory}/headers"
        command = [
            curl,
            "--http1.1",
            "--silent",
            "--show-error",
            "--location",
            "--noproxy",
            "",
            "--proxy",
            proxy,
            "--max-time",
            str(max(1, int(round(timeout)))),
            "--request",
            method,
            "--dump-header",
            header_path,
            "--output",
            body_path,
            "--write-out",
            "%{http_code}",
        ]
        for name, value in (headers or {}).items():
            command.extend(("--header", f"{name}: {value}"))
        if json_body is not None:
            command.extend(
                (
                    "--header",
                    "Content-Type: application/json",
                    "--data-binary",
                    json.dumps(json_body, ensure_ascii=False),
                )
            )
        elif form is not None:
            for name, value in form.items():
                command.extend(("--data-urlencode", f"{name}={value}"))
        command.append(url)
        started = time.monotonic()
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=max(2, timeout + 2),
        )
        elapsed_ms = round((time.monotonic() - started) * 1000)
        if completed.returncode != 0:
            raise RuntimeError(
                (completed.stderr or f"curl exited with code {completed.returncode}").strip()
            )
        try:
            status_code = int(completed.stdout.strip()[-3:])
        except ValueError as exc:
            raise RuntimeError("curl did not report an HTTP status") from exc
        content = open(body_path, "rb").read()
        raw_headers = open(header_path, encoding="iso-8859-1").read().splitlines()
        parsed_headers: dict[str, str] = {}
        for line in raw_headers:
            name, separator, value = line.partition(":")
            if separator:
                parsed_headers[name.strip()] = value.strip()
        return CurlResponse(status_code, parsed_headers, content, elapsed_ms)


def _extract_ip(response: requests.Response) -> str:
    candidates: list[Any] = []
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        for key in ("ip", "query", "address", "origin"):
            if key in payload:
                candidates.append(payload[key])
    candidates.append(response.text.strip())
    for line in response.text.splitlines():
        if line.startswith("ip="):
            candidates.append(line.partition("=")[2].strip())
    for candidate in candidates:
        value = str(candidate or "").strip().split(",", 1)[0].strip()
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            continue
    raise ValueError("response did not contain a valid public IP address")


def detect_egress_ip(proxy: str, urls: tuple[str, ...], timeout: float) -> tuple[str | None, dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    deadline = time.monotonic() + max(0.1, timeout)
    for index, url in enumerate(urls):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        remaining_urls = len(urls) - index
        attempt_timeout = max(0.1, remaining / remaining_urls)
        started = time.monotonic()
        try:
            response = _curl_request(
                proxy=proxy,
                method="GET",
                url=url,
                headers={"Accept": "application/json,text/plain,*/*"},
                timeout=attempt_timeout,
            )
            latency_ms = response.elapsed_ms
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code}")
            value = _extract_ip(response)
            attempts.append({"url": url, "status": response.status_code, "latency_ms": latency_ms, "ip": value})
            return value, {"attempts": attempts}
        except Exception as exc:  # noqa: BLE001
            attempts.append(
                {
                    "url": url,
                    "latency_ms": round((time.monotonic() - started) * 1000),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
    return None, {"attempts": attempts}


def _json_path(payload: Any, path: str) -> tuple[bool, Any]:
    current = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return False, None
    return True, current


def _probe_target_once(proxy: str, target: TargetConfig, timeout: float) -> TargetResponse:
    started = time.monotonic()
    try:
        response = _curl_request(
            proxy=proxy,
            method=target.method,
            url=target.url,
            headers=target.headers,
            form=target.form,
            json_body=target.json_body,
            timeout=timeout,
        )
        latency_ms = response.elapsed_ms
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return TargetResponse(
            healthy=False,
            status_code=None,
            latency_ms=round((time.monotonic() - started) * 1000),
            error_type=type(exc).__name__,
            error=str(exc),
            detail={},
        )
    detail: dict[str, Any] = {
        "content_type": response.headers.get("Content-Type", response.headers.get("content-type", "")),
        "response_bytes": len(response.content),
    }
    if response.status_code not in target.expected_statuses:
        return TargetResponse(
            healthy=False,
            status_code=response.status_code,
            latency_ms=latency_ms,
            error_type="UnexpectedStatus",
            error=f"expected {list(target.expected_statuses)}, got HTTP {response.status_code}",
            detail=detail,
        )
    if target.json_checks:
        try:
            payload = response.json()
        except ValueError as exc:
            return TargetResponse(
                healthy=False,
                status_code=response.status_code,
                latency_ms=latency_ms,
                error_type="InvalidJson",
                error=str(exc),
                detail=detail,
            )
        failures: list[str] = []
        for check in target.json_checks:
            found, value = _json_path(payload, check.path)
            if check.present is True and (not found or value in (None, "", [], {})):
                failures.append(f"{check.path} is missing or empty")
            elif check.present is False and found:
                failures.append(f"{check.path} is unexpectedly present")
            if check.has_equals and (not found or str(value) != str(check.equals)):
                failures.append(f"{check.path} expected {check.equals!r}, got {value!r}")
        if failures:
            detail["json_check_failures"] = failures
            return TargetResponse(
                healthy=False,
                status_code=response.status_code,
                latency_ms=latency_ms,
                error_type="JsonCheckFailed",
                error="; ".join(failures),
                detail=detail,
            )
    return TargetResponse(
        healthy=True,
        status_code=response.status_code,
        latency_ms=latency_ms,
        error_type=None,
        error=None,
        detail=detail,
    )


def probe_target(
    proxy: str,
    target: TargetConfig,
    timeout: float,
    *,
    attempts: int = 1,
    retry_delay: float = 1.0,
) -> TargetResponse:
    result: TargetResponse | None = None
    for attempt in range(1, max(1, attempts) + 1):
        result = _probe_target_once(proxy, target, timeout)
        retriable = result.status_code is None or result.status_code in {500, 502, 503, 504}
        if result.healthy or not retriable or attempt >= attempts:
            detail = dict(result.detail)
            detail["attempts"] = attempt
            return TargetResponse(
                healthy=result.healthy,
                status_code=result.status_code,
                latency_ms=result.latency_ms,
                error_type=result.error_type,
                error=result.error,
                detail=detail,
            )
        time.sleep(max(0.0, retry_delay))
    assert result is not None
    return result


def probe_selected_node(
    *,
    api: MihomoApi,
    config: AppConfig,
    group: str,
    port: int,
    node: ProxyNode,
    target_name: str,
) -> NodeProbeOutcome:
    target = config.targets[target_name]
    checked_at = utc_now()
    try:
        api.select(group, node.name)
        time.sleep(config.gateway.settle_seconds)
        selected = api.group(group).get("now")
        if selected != node.name:
            raise RuntimeError(f"group selected {selected!r}, expected {node.name!r}")
    except Exception as exc:  # noqa: BLE001
        return NodeProbeOutcome(
            egress=ProbeResult(
                node_key=node.key,
                provider=node.provider,
                proxy_name=node.name,
                target=EGRESS_TARGET,
                checked_at=checked_at,
                healthy=False,
                egress_ip=None,
                latency_ms=None,
                status_code=None,
                error_type=type(exc).__name__,
                error=str(exc),
                detail={"stage": "select"},
            ),
            target=None,
        )

    local_proxy = proxy_url(config.gateway.listen, port)
    target_response = probe_target(
        local_proxy,
        target,
        config.gateway.probe_timeout_seconds,
        attempts=config.gateway.target_probe_attempts,
        retry_delay=config.gateway.target_probe_retry_seconds,
    )
    if target_response.healthy:
        egress_ip, egress_detail = detect_egress_ip(
            local_proxy,
            config.ip_check_urls,
            config.gateway.probe_timeout_seconds,
        )
    else:
        egress_ip = None
        egress_detail = {
            "skipped": "target_not_healthy",
            "target_status_code": target_response.status_code,
            "target_error_type": target_response.error_type,
        }
    if not target_response.healthy:
        egress_result = ProbeResult(
            node_key=node.key,
            provider=node.provider,
            proxy_name=node.name,
            target=EGRESS_TARGET,
            checked_at=checked_at,
            healthy=False,
            egress_ip=None,
            latency_ms=None,
            status_code=None,
            error_type="EgressCheckSkipped",
            error="public IP check skipped because the target probe failed",
            detail={"stage": "egress", "egress": egress_detail},
        )
    elif egress_ip is None:
        egress_result = ProbeResult(
            node_key=node.key,
            provider=node.provider,
            proxy_name=node.name,
            target=EGRESS_TARGET,
            checked_at=checked_at,
            healthy=False,
            egress_ip=None,
            latency_ms=None,
            status_code=None,
            error_type="EgressIpUnavailable",
            error="all public IP checks failed",
            detail={"stage": "egress", "egress": egress_detail},
        )
    else:
        successful_attempt = next(
            (attempt for attempt in egress_detail.get("attempts", ()) if attempt.get("ip") == egress_ip),
            {},
        )
        egress_result = ProbeResult(
            node_key=node.key,
            provider=node.provider,
            proxy_name=node.name,
            target=EGRESS_TARGET,
            checked_at=checked_at,
            healthy=True,
            egress_ip=egress_ip,
            latency_ms=successful_attempt.get("latency_ms"),
            status_code=successful_attempt.get("status"),
            error_type=None,
            error=None,
            detail={"stage": "egress", "egress": egress_detail},
        )
    detail = dict(target_response.detail)
    detail["stage"] = "target"
    detail["egress"] = egress_detail
    return NodeProbeOutcome(
        egress=egress_result,
        target=ProbeResult(
            node_key=node.key,
            provider=node.provider,
            proxy_name=node.name,
            target=target_name,
            checked_at=utc_now(),
            healthy=target_response.healthy,
            egress_ip=egress_ip,
            latency_ms=target_response.latency_ms,
            status_code=target_response.status_code,
            error_type=target_response.error_type,
            error=target_response.error,
            detail=detail,
        ),
    )


def probe_existing_lane(
    *,
    config: AppConfig,
    port: int,
    target_name: str,
) -> tuple[str | None, TargetResponse, dict[str, Any]]:
    local_proxy = proxy_url(config.gateway.listen, port)
    target_response = probe_target(
        local_proxy,
        config.targets[target_name],
        config.gateway.probe_timeout_seconds,
        attempts=config.gateway.target_probe_attempts,
        retry_delay=config.gateway.target_probe_retry_seconds,
    )
    egress_ip, egress_detail = detect_egress_ip(
        local_proxy,
        config.ip_check_urls,
        config.gateway.probe_timeout_seconds,
    )
    return egress_ip, target_response, egress_detail
