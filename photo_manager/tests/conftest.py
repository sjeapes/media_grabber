import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
STUBS = HERE / "stubs"
sys.path.insert(0, str(HERE.parent / "rootfs" / "app"))
sys.path.insert(0, str(HERE))

from photomanager import config  # noqa: E402
from photomanager.web import Services, create_app  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    media = tmp_path / "media"
    nas = media / "NAS"
    nas.mkdir(parents=True)
    (tmp_path / "share").mkdir()
    data = tmp_path / "data"
    data.mkdir()
    mounts = tmp_path / "mounts"
    mounts.write_text(f"overlay / overlay rw 0 0\n/dev/sda8 {media} ext4 rw 0 0\n"
                      f"//192.168.1.175/public {nas} cifs rw 0 0\n")
    (data / "options.json").write_text(json.dumps({"low_space_gb": 1}))
    monkeypatch.setenv("PM_DATA_DIR", str(data))
    monkeypatch.setenv("PM_MEDIA_ROOT", str(media))
    monkeypatch.setenv("PM_SHARE_ROOT", str(tmp_path / "share"))
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setenv("PM_DARKTABLE", str(STUBS / "darktable-cli"))
    monkeypatch.setenv("PM_STUB_LOG", str(tmp_path / "stub.log"))
    cfg = config.load()
    svc = Services(cfg, mounts_file=str(mounts))
    app = create_app(svc)
    app.testing = True
    client = app.test_client()
    client.environ_base["REMOTE_ADDR"] = "172.30.32.2"
    yield {"svc": svc, "c": client, "nas": nas, "mounts": mounts, "cfg": cfg, "tmp": tmp_path,
           "media": media, "photos": nas / "Pictures" / "Canon", "proc": svc.processor,
           "q": svc.quarantine, "stub_log": tmp_path / "stub.log"}


