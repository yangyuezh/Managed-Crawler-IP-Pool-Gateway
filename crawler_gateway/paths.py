from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectPaths:
    root: Path

    @classmethod
    def discover(cls) -> "ProjectPaths":
        return cls(Path(__file__).resolve().parents[1])

    @property
    def private_dir(self) -> Path:
        return self.root / "private"

    @property
    def config_path(self) -> Path:
        return self.private_dir / "gateway.yaml"

    @property
    def example_config_path(self) -> Path:
        return self.root / "config" / "gateway.example.yaml"

    @property
    def secret_path(self) -> Path:
        return self.private_dir / "controller-secret"

    @property
    def runtime_dir(self) -> Path:
        return self.root / "runtime"

    @property
    def runtime_config_path(self) -> Path:
        return self.runtime_dir / "mihomo.yaml"

    @property
    def pid_path(self) -> Path:
        return self.runtime_dir / "mihomo.pid"

    @property
    def state_path(self) -> Path:
        return self.runtime_dir / "gateway.sqlite"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def mihomo_log_path(self) -> Path:
        return self.logs_dir / "mihomo.log"

    @property
    def xray_dir(self) -> Path:
        return self.runtime_dir / "xray"

    @property
    def xray_marker_path(self) -> Path:
        return self.xray_dir / "enabled.json"

    def ensure_directories(self) -> None:
        for path in (self.private_dir, self.runtime_dir, self.logs_dir, self.xray_dir):
            path.mkdir(parents=True, exist_ok=True)
        for path in (self.private_dir, self.runtime_dir, self.xray_dir):
            try:
                path.chmod(0o700)
            except OSError:
                pass
