from pathlib import Path

from crawler_gateway.faults import clear_fault, write_fault


def test_fault_file_contains_current_strict_progress(tmp_path: Path) -> None:
    path = tmp_path / "fault.txt"
    write_fault(
        path,
        "no healthy nodes",
        {
            "available": True,
            "continuous_done": 10,
            "base_rows": 100,
            "percent": 10.0,
            "remaining": 90,
            "detail_success_files": 12,
            "next": {"position": 11, "record_id": "abc"},
            "active_detail_errors": 0,
        },
    )
    text = path.read_text(encoding="utf-8")
    assert "严格连续完成：10 / 100" in text
    assert "剩余数量：90" in text
    assert clear_fault(path) is True
    assert clear_fault(path) is False
