from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any


def fault_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def write_fault(path: Path, reason: str, progress: dict[str, Any] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "NSFC 结项详情爬虫故障",
        "",
        f"发现时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')}",
        f"故障原因：{reason}",
    ]
    if progress and progress.get("available"):
        lines.extend(
            [
                f"严格连续完成：{progress.get('continuous_done')} / {progress.get('base_rows')}",
                f"完成比例：{progress.get('percent')}%",
                f"剩余数量：{progress.get('remaining')}",
                f"详情文件：{progress.get('detail_success_files')}",
                f"下一个断点：{progress.get('next')}",
                f"活动错误：{progress.get('active_detail_errors')}",
            ]
        )
    lines.extend(["", "现有成功数据不会被删除。请先检查节点或目标接口，再继续启动。", ""])
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    os.replace(temporary, path)
    path.chmod(0o600)


def clear_fault(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
