"""Publish NAS / VPN / queue state to Home Assistant as entities and events."""
from __future__ import annotations

import json
import logging
import urllib.request

log = logging.getLogger("mediagrabber.ha")
DEVICE = "Media Grabber"


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
        except Exception as exc:  # noqa: BLE001 - HA being down must never break downloads
            if not self._warned:
                log.warning("Could not update Home Assistant (%s); will keep trying quietly", exc)
                self._warned = True

    def state(self, entity_id: str, state, **attrs):
        self._post(f"/states/{entity_id}", {"state": state, "attributes": attrs})

    def event(self, event_type: str, data: dict):
        self._post(f"/events/{event_type}", data)

    def publish(self, nas: dict, vpn: dict, counts: dict, last: dict | None):
        disk = nas.get("disk") or {}
        self.state("binary_sensor.media_grabber_nas_problem", "off" if nas["healthy"] else "on",
                   friendly_name=f"{DEVICE} storage problem", device_class="problem",
                   problems=nas["problems"], warnings=nas.get("warnings", []),
                   location=nas.get("name"), mode=nas.get("mode"), path=nas.get("path"),
                   choice=nas.get("choice"), needs_choice=nas.get("needs_choice"),
                   network_storage_found=nas.get("candidate_count"),
                   fstype=nas.get("fstype"), source=nas.get("source"),
                   write_ms=(nas.get("probe") or {}).get("ms"), icon="mdi:nas")
        self.state("sensor.media_grabber_nas_free", disk.get("free_gb", "unavailable"),
                   friendly_name=f"{DEVICE} storage free", unit_of_measurement="GB",
                   device_class="data_size", state_class="measurement",
                   location=nas.get("name"), mode=nas.get("mode"),
                   total_gb=disk.get("total_gb"), used_pct=disk.get("used_pct"),
                   low_space=nas.get("low_space"), icon="mdi:harddisk")
        exit_ = vpn.get("exit") or {}
        self.state("binary_sensor.media_grabber_uk_vpn", "on" if vpn.get("uk") else "off",
                   friendly_name=f"{DEVICE} UK VPN", device_class="connectivity",
                   configured=vpn.get("configured"), exit_country=exit_.get("country"),
                   exit_city=exit_.get("city"), provider=exit_.get("org"), icon="mdi:vpn")
        active = counts.get("running", 0) + counts.get("queued", 0)
        self.state("sensor.media_grabber_queue", active, friendly_name=f"{DEVICE} queue",
                   unit_of_measurement="jobs", running=counts.get("running", 0),
                   queued=counts.get("queued", 0), failed=counts.get("failed", 0),
                   icon="mdi:download")
        if last:
            self.state("sensor.media_grabber_last_download", last["title"][:250],
                       friendly_name=f"{DEVICE} last download", status=last["status"],
                       kind=last["kind"], finished=last.get("finished"), icon="mdi:television-play")
