"""Add-on options and filesystem locations."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULTS = {
    "nas_path": "/media/nas",
    "iplayer_folder": "iPlayer",
    "youtube_folder": "YouTube",
    "require_network_mount": True,
    "low_space_gb": 50,
    "tv_quality": "fhd,hd,sd",
    "youtube_format": "bv*[height<=1080]+ba/b[height<=1080]",
    "max_concurrent_downloads": 1,
    "pvr_interval_hours": 4,
    "iplayer_requires_uk_vpn": True,
    "vpn_for_youtube": False,
    "auto_update_tools": True,
}


@dataclass
class Config:
    options: dict
    data_dir: Path
    addon_config_dir: Path
    get_iplayer: str = "get_iplayer"
    yt_dlp: str = "yt-dlp"
    wireproxy: str = "wireproxy"
    proxy_port: int = 18118
    listen_port: int = 8099
    allowed_clients: set = field(default_factory=lambda: {"172.30.32.2", "127.0.0.1", "::1"})
    supervisor_token: str | None = None
    ha_api: str = "http://supervisor/core/api"

    # --- derived -------------------------------------------------------
    @property
    def nas_path(self) -> Path:
        return Path(self.options["nas_path"])

    @property
    def iplayer_dir(self) -> Path:
        return self.nas_path / self.options["iplayer_folder"]

    @property
    def youtube_dir(self) -> Path:
        return self.nas_path / self.options["youtube_folder"]

    @property
    def gip_profile(self) -> Path:
        return self.data_dir / "get_iplayer"

    @property
    def wg_conf(self) -> Path:
        return self.data_dir / "wg0.conf"

    @property
    def proxy_url(self) -> str:
        return f"http://127.0.0.1:{self.proxy_port}"

    def opt(self, key):
        return self.options.get(key, DEFAULTS.get(key))


def load() -> Config:
    data_dir = Path(os.environ.get("MG_DATA_DIR", "/data"))
    options_file = Path(os.environ.get("MG_OPTIONS", data_dir / "options.json"))
    options = dict(DEFAULTS)
    if options_file.exists():
        options.update(json.loads(options_file.read_text()))
    cfg = Config(
        options=options,
        data_dir=data_dir,
        addon_config_dir=Path(os.environ.get("MG_ADDON_CONFIG", "/config")),
        get_iplayer=os.environ.get("MG_GET_IPLAYER", "get_iplayer"),
        yt_dlp=os.environ.get("MG_YT_DLP", "yt-dlp"),
        wireproxy=os.environ.get("MG_WIREPROXY", "wireproxy"),
        listen_port=int(os.environ.get("MG_PORT", "8099")),
        supervisor_token=os.environ.get("SUPERVISOR_TOKEN"),
    )
    extra = os.environ.get("MG_ALLOWED_CLIENTS")
    if extra:
        cfg.allowed_clients |= {c.strip() for c in extra.split(",") if c.strip()}
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg.gip_profile.mkdir(parents=True, exist_ok=True)
    return cfg
