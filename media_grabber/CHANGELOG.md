# Changelog

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
