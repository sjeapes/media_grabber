"""Publish scan and NAS state to Home Assistant as entities."""
from __future__ import annotations

import json
import logging
import urllib.request

log = logging.getLogger("photomanager.ha")
DEVICE = "Photo Manager"


class HAClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.enabled = bool(cfg.supervisor_token)
        self._warned = False

    def _post(self, path: str, body: dict):
        if not self.enabled:
            return
        req = urllib.request.Request(
            f"{self.cfg.ha_api}{path}", data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.cfg.supervisor_token}",
                     "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as exc:  # noqa: BLE001 - HA being down must never break the add-on
            if not self._warned:
                log.warning("Could not update Home Assistant (%s); will keep trying quietly", exc)
                self._warned = True

    def state(self, entity_id: str, state, **attrs):
        self._post(f"/states/{entity_id}", {"state": state, "attributes": attrs})

    def event(self, event_type: str, data: dict):
        self._post(f"/events/{event_type}", data)

    def publish(self, nas: dict, report: dict):
        disk = nas.get("disk") or {}
        self.state("binary_sensor.photo_manager_storage_problem",
                   "off" if nas["healthy"] else "on",
                   friendly_name=f"{DEVICE} storage problem", device_class="problem",
                   problems=nas["problems"], warnings=nas.get("warnings", []),
                   location=nas.get("name"), path=nas.get("path"),
                   needs_choice=nas.get("needs_choice"),
                   network_storage_found=nas.get("candidate_count"), icon="mdi:nas")
        sc = report["state"]
        if sc.get("running"):
            status = sc.get("phase") or "scanning"
        else:
            status = "idle"
        self.state("sensor.photo_manager_status", status,
                   friendly_name=f"{DEVICE} status", icon="mdi:image-search",
                   files_seen=sc.get("files"), current=sc.get("current"),
                   hashed=sc.get("hashed"), to_hash=sc.get("to_hash"),
                   last_scan=sc.get("finished"), scan_error=sc.get("error"),
                   cancelled=sc.get("cancelled"))
        total = report.get("total_files")
        if total is None:
            return
        dups = report.get("duplicates", {})
        self.state("sensor.photo_manager_photos", total,
                   friendly_name=f"{DEVICE} photos", unit_of_measurement="photos",
                   icon="mdi:image-multiple", total_gb=report.get("total_gb"),
                   first=report.get("first"), last=report.get("last"),
                   cameras=report.get("cameras", []), folder_count=report.get("folder_count"),
                   duplicate_groups=dups.get("groups"),
                   duplicate_extra_files=dups.get("extra_files"),
                   duplicate_reclaimable_gb=dups.get("reclaimable_gb"),
                   nas_free_gb=disk.get("free_gb"))
