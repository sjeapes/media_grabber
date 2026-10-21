"""Add-on options and filesystem locations."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
import json

DEFAULTS = {
    "low_space_gb": 20,
}


@dataclass
class Config:
    options: dict
    data_dir: Path
    listen_port: int = 8099
    allowed_clients: set = field(default_factory=lambda: {"172.30.32.2", "127.0.0.1", "::1"})
    supervisor_token: str | None = None
    ha_api: str = "http://supervisor/core/api"
    media_root: Path = Path("/media")
    share_root: Path = Path("/share")
    # Set by Storage.resolve(): the root of the selected NAS (None = nothing usable yet)
    base_path: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "photos.db"

    def opt(self, key):
        return self.options.get(key, DEFAULTS.get(key))


def load() -> Config:
    data_dir = Path(os.environ.get("PM_DATA_DIR", "/data"))
    options_file = Path(os.environ.get("PM_OPTIONS", data_dir / "options.json"))
    options = dict(DEFAULTS)
    if options_file.exists():
        options.update(json.loads(options_file.read_text()))
    cfg = Config(
        options=options,
        data_dir=data_dir,
        listen_port=int(os.environ.get("PM_PORT", "8099")),
        supervisor_token=os.environ.get("SUPERVISOR_TOKEN"),
        media_root=Path(os.environ.get("PM_MEDIA_ROOT", "/media")),
        share_root=Path(os.environ.get("PM_SHARE_ROOT", "/share")),
    )
    extra = os.environ.get("PM_ALLOWED_CLIENTS")
    if extra:
        cfg.allowed_clients |= {c.strip() for c in extra.split(",") if c.strip()}
    data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
