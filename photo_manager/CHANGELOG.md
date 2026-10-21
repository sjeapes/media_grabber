# Changelog

## 0.2.0

- **Develop tab**: converts CR2 files to JPEG with darktable (automatic
  settings). **Off by default** — switch it on with the toggle. When on, it
  runs by itself overnight (window set by `develop_start_hour` /
  `develop_end_hour`, default 01:00–06:00) and can also be started by hand.
- Where to save the JPEGs: a **separate folder** (default) or **next to each
  RAW file**. Files are named `<name>_developed.jpg`; an existing file is
  never overwritten and your CR2 files are never touched.
- **Develop only one folder** (to test on a few photos first) and an option to
  **skip photos that already have a JPEG**.
- Identical duplicate RAWs are developed once. Failed photos are retried up to
  three times and can be retried from the tab. Developing stops if the NAS runs
  low on space.
- **Quarantine tab**: you tick which copy (or copies) of a duplicate group to
  move to quarantine. A CRC32 checksum check compares them with the copy that
  stays; the move only happens if they all match (re-checked on the server).
  Nothing is deleted, and files can be restored.
- New entities: `sensor.photo_manager_developed`, `sensor.photo_manager_quarantine`,
  event `photo_manager_develop_finished`.
- New options: `develop_start_hour`, `develop_end_hour`, `develop_min_free_gb`,
  `jpeg_quality`.

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
