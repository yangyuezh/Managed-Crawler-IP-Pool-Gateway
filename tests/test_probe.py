import subprocess

import pytest

from crawler_gateway.mihomo import ProxyNode
from crawler_gateway.probe import (
    CurlResponse,
    TargetResponse,
    _extract_ip,
    _json_path,
    detect_egress_ip,
    probe_selected_node,
    probe_target,
)
from crawler_gateway.state import EGRESS_TARGET


class FakeResponse:
    def __init__(self, payload, text=""):
        self.payload = payload
        self.text = text

    def json(self):
        return self.payload


def test_extract_ip_from_json() -> None:
    assert _extract_ip(FakeResponse({"ip": "203.0.113.9"})) == "203.0.113.9"


def test_extract_ip_from_cloudflare_trace() -> None:
    response = FakeResponse(None, "fl=1\nip=203.0.113.10\ncolo=LAX\n")
    assert _extract_ip(response) == "203.0.113.10"


def test_json_path() -> None:
    found, value = _json_path({"data": {"name": "project"}}, "data.name")
    assert found is True
    assert value == "project"
    assert _json_path({"data": {}}, "data.name") == (False, None)


def test_egress_checks_share_one_total_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[float] = []

    def curl_request(**kwargs):
        observed.append(kwargs["timeout"])
        raise RuntimeError("failed")

    monkeypatch.setattr("crawler_gateway.probe._curl_request", curl_request)
    value, detail = detect_egress_ip("http://127.0.0.1:1", ("a", "b"), 10)
    assert value is None
    assert len(detail["attempts"]) == 2
    assert observed[0] == pytest.approx(5, abs=0.2)
    assert observed[1] == pytest.approx(10, abs=0.2)


def test_node_probe_skips_public_ip_lookup_after_target_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = ProxyNode("p\x1fnode", "p", "node", "vless", None, {})

    class Api:
        def select(self, _group, _name):
            return None

        def group(self, _group):
            return {"now": "node"}

    config = type(
        "Config",
        (),
        {
            "gateway": type(
                "Gateway",
                (),
                {
                    "settle_seconds": 0,
                    "listen": "127.0.0.1",
                    "probe_timeout_seconds": 12,
                    "target_probe_attempts": 2,
                    "target_probe_retry_seconds": 0,
                },
            )(),
            "ip_check_urls": ("https://ip.test",),
            "targets": {"target": object()},
        },
    )()
    monkeypatch.setattr(
        "crawler_gateway.probe.detect_egress_ip",
        lambda *_args, **_kwargs: pytest.fail(
            "a failed target must not trigger public IP lookup"
        ),
    )
    monkeypatch.setattr(
        "crawler_gateway.probe.probe_target",
        lambda *_args, **_kwargs: TargetResponse(
            False,
            404,
            40,
            "UnexpectedStatus",
            "expected 200",
            {},
        ),
    )

    outcome = probe_selected_node(
        api=Api(),
        config=config,
        group="probe",
        port=17991,
        node=node,
        target_name="target",
    )

    assert outcome.egress.target == EGRESS_TARGET
    assert outcome.egress.healthy is False
    assert outcome.egress.error_type == "EgressCheckSkipped"
    assert outcome.egress.egress_ip is None
    assert outcome.target is not None
    assert outcome.target.healthy is False
    assert outcome.target.status_code == 404


def test_node_probe_keeps_target_success_when_egress_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = ProxyNode("p\x1fnode", "p", "node", "vless", None, {})

    class Api:
        def select(self, _group, _name):
            return None

        def group(self, _group):
            return {"now": "node"}

    config = type(
        "Config",
        (),
        {
            "gateway": type(
                "Gateway",
                (),
                {
                    "settle_seconds": 0,
                    "listen": "127.0.0.1",
                    "probe_timeout_seconds": 12,
                    "target_probe_attempts": 2,
                    "target_probe_retry_seconds": 0,
                },
            )(),
            "ip_check_urls": ("https://ip.test",),
            "targets": {"target": object()},
        },
    )()
    monkeypatch.setattr(
        "crawler_gateway.probe.detect_egress_ip",
        lambda *_args, **_kwargs: (None, {"attempts": []}),
    )
    monkeypatch.setattr(
        "crawler_gateway.probe.probe_target",
        lambda *_args, **_kwargs: TargetResponse(
            True, 200, 30, None, None, {}
        ),
    )

    outcome = probe_selected_node(
        api=Api(),
        config=config,
        group="probe",
        port=17991,
        node=node,
        target_name="target",
    )

    assert outcome.egress.healthy is False
    assert outcome.target is not None
    assert outcome.target.healthy is True
    assert outcome.target.status_code == 200


def test_curl_request_forces_the_configured_proxy(monkeypatch, tmp_path) -> None:
    from crawler_gateway.probe import _curl_request

    captured: list[str] = []

    def run(command, **_kwargs):
        captured.extend(command)
        body_path = command[command.index("--output") + 1]
        header_path = command[command.index("--dump-header") + 1]
        open(body_path, "wb").write(b'{"ip":"203.0.113.9"}')
        open(header_path, "w", encoding="iso-8859-1").write(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout="200", stderr="")

    monkeypatch.setattr("crawler_gateway.probe.shutil.which", lambda _name: "/usr/bin/curl")
    monkeypatch.setattr("crawler_gateway.probe.subprocess.run", run)
    response = _curl_request(
        proxy="http://127.0.0.1:17991",
        method="GET",
        url="https://ip.example",
        timeout=5,
    )

    assert response.status_code == 200
    assert captured[captured.index("--noproxy") + 1] == ""
    assert captured[captured.index("--proxy") + 1] == "http://127.0.0.1:17991"
    assert "--http1.1" in captured


def test_target_probe_retries_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def request(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary transport failure")
        return CurlResponse(200, {"Content-Type": "application/json"}, b'{"code":200}', 20)

    target = type(
        "Target",
        (),
        {
            "method": "POST",
            "url": "https://example.test/detail",
            "headers": {},
            "form": {},
            "json_body": None,
            "expected_statuses": (200,),
            "json_checks": (),
        },
    )()
    monkeypatch.setattr("crawler_gateway.probe._curl_request", request)
    monkeypatch.setattr("crawler_gateway.probe.time.sleep", lambda _seconds: None)
    result = probe_target("http://127.0.0.1:1", target, 5, attempts=2)
    assert result.healthy is True
    assert result.detail["attempts"] == 2


def test_target_probe_does_not_retry_explicit_404(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def request(**_kwargs):
        nonlocal calls
        calls += 1
        return CurlResponse(404, {"Content-Type": "text/html"}, b"not found", 10)

    target = type(
        "Target",
        (),
        {
            "method": "POST",
            "url": "https://example.test/detail",
            "headers": {},
            "form": {},
            "json_body": None,
            "expected_statuses": (200,),
            "json_checks": (),
        },
    )()
    monkeypatch.setattr("crawler_gateway.probe._curl_request", request)
    result = probe_target("http://127.0.0.1:1", target, 5, attempts=2)
    assert result.status_code == 404
    assert calls == 1
