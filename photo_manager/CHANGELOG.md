# Changelog

## 0.1.0

- First release: read-only scan of the CR2 photos in a folder you choose.
- The NAS is found automatically from Home Assistant's network storage
  (Settings → System → Storage); pick one on the Storage tab if there are
  several.
- Folder picker in the app for the photo folder (and, for later versions, the
  developed-photos and quarantine folders).
- Library report: photo count and size, date range, photos per year, camera and
  folder, camera-JPEG pairs, and exact duplicates.
- Entities: `binary_sensor.photo_manager_storage_problem`,
  `sensor.photo_manager_status`, `sensor.photo_manager_photos`.
