import json
from pathlib import Path

from crawler_gateway.events import RotatingEventSink


def test_rotating_event_sink_writes_structured_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    sink = RotatingEventSink(path, max_bytes=1024, backup_count=1)

    sink({"event": "cycle_complete", "nodes": 10})
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["event"] == "cycle_complete"
    assert payload["nodes"] == 10
    assert payload["logged_at"].endswith("+00:00")
