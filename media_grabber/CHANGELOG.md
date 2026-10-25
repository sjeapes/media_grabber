# Changelog

## 0.4.0

- New **Browse** tab: BBC iPlayer in a browser that runs inside the add-on and
  goes through the UK VPN, so your own device doesn't need a VPN. Tap, scroll,
  type, back/forward, address bar (type a programme name to search iPlayer).
- **+ Programme** queues the episode on screen; **+ Series** downloads every
  episode available now and records new ones (name pre-filled, editable).
- The browser only runs while the tab is open and stops after 10 idle minutes.
- Series recordings: adding one checks for episodes straight away; "Check now"
  refreshes the iPlayer list first; a check skipped because the VPN was down is
  retried after 5 minutes instead of 4 hours.

## 0.3.1

- Failed downloads are retried automatically (up to 3 attempts, after 1 and
  5 minutes) — a short VPN or network blip no longer loses an episode.
- Failures are written to the add-on log with the last lines of output, and
  published to `sensor.media_grabber_last_download` (`error`, `attempts`,
  `log_tail`) and `sensor.media_grabber_queue` (`recent`).
- VPN tunnel errors from wireproxy now appear in the add-on log.
- The storage watchdog only stops downloads after ~45 s of continuous trouble,
  not on a single slow check.
- A download interrupted by an add-on restart (e.g. an update) starts again
  automatically instead of being marked failed.
- Fix: a retry that succeeded could be reported as failed because of the
  previous attempt's error lines.

## 0.3.0

- New **Space** tab: read-only scan of a storage location showing a folder
  tree with sizes, largest files, recycle-bin/snapshot folders, and how much
  used space isn't visible from the share. Nothing is ever deleted.
- New `sensor.media_grabber_disk_usage` with the scan summary as attributes.

## 0.2.0

- Download location is detected from Home Assistant's network storage
  (Settings → System → Storage) instead of a fixed `/media/nas` path.
- Several network storages: pick one on the Storage tab. Option to save to
  Home Assistant's own disk (`/media/MediaGrabber`) with a free-space floor
  (`local_min_free_gb`).
- Running downloads stop if their storage disappears or HA's disk gets too full.
- Download commands are built when a job starts, so queued jobs follow the
  selected location.
- `nas_path` and `require_network_mount` options removed.

## 0.1.0

- First release: iPlayer search/download/series recording, yt-dlp links,
  NAS health page, split-tunnel WireGuard (wireproxy) for get_iplayer,
  Home Assistant sensors and `media_grabber_finished` event.
