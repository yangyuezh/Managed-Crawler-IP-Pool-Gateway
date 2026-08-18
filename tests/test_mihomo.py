from pathlib import Path

import yaml

from crawler_gateway.config import AppConfig, GatewaySettings, ProviderConfig
from crawler_gateway.mihomo import (
    FAIL_CLOSED_PROXY,
    MihomoApi,
    _provider_payload,
    render_config,
)
from crawler_gateway.paths import ProjectPaths


def test_provider_payload_keeps_nonempty_cache_when_remote_refresh_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    destination = tmp_path / "providers" / "remote.yaml"
    destination.parent.mkdir()
    destination.write_text(
        "proxies:\n  - name: cached\n    type: direct\n",
        encoding="utf-8",
    )
    provider = ProviderConfig(
        name="remote",
        type="http",
        url="https://subscription.example.test/list",
    )
    config = type("Config", (), {})()
    monkeypatch.setattr(
        "crawler_gateway.mihomo.materialize_provider",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    payload = _provider_payload(provider, config, tmp_path)

    assert payload["path"] == "./providers/remote.yaml"


def test_releasing_a_lane_fails_closed() -> None:
    api = object.__new__(MihomoApi)
    selected: list[tuple[str, str]] = []
    api.select = lambda group, proxy: selected.append((group, proxy))

    api.release("CRAWLER-WORK-01")

    assert selected == [("CRAWLER-WORK-01", FAIL_CLOSED_PROXY)]


def test_rendered_lane_groups_default_to_fail_closed(tmp_path: Path) -> None:
    provider_path = tmp_path / "provider.yaml"
    provider_path.write_text(
        "proxies:\n  - name: fixture\n    type: direct\n",
        encoding="utf-8",
    )
    config = AppConfig(
        path=tmp_path / "gateway.yaml",
        gateway=GatewaySettings(
            backend="mihomo",
            work_lanes=2,
            probe_lanes=1,
        ),
        ip_check_urls=("https://example.test/ip",),
        providers=(
            ProviderConfig(name="fixture", type="file", path=str(provider_path)),
        ),
        targets={},
    )
    paths = ProjectPaths(tmp_path)

    output = render_config(config, paths, "x" * 32)
    payload = yaml.safe_load(output.read_text(encoding="utf-8"))

    assert payload["proxy-groups"]
    assert all(
        group["proxies"][0] == FAIL_CLOSED_PROXY
        for group in payload["proxy-groups"]
    )
