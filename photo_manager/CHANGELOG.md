# Changelog

## 0.4.0

- **Find photos not in Google Photos**: a guided Google Takeout flow. Scan the
  Takeout .zip files (or extracted folders) once, then check all photos or one
  folder, see what is missing in a thumbnail gallery, tick folders and photos
  and upload them (developing them first when needed).
- **Gallery tab**: browse all photos, or filter to recently developed, developed
  or not developed; folder and name search; full-size viewer that flips between
  the developed JPEG and the camera preview.
- **darktable settings** on the Develop tab: look presets (filmic, sigmoid,
  classic, minimal), JPEG quality, maximum size, colour space, rendering intent,
  high-quality resampling and an optional XMP template, plus **Try it on one
  photo** to compare a preview with the camera's picture. Applies to future
  photos only; existing JPEGs are never replaced.

## 0.3.0

- **Thumbnails** in the Quarantine tab: the copy that stays is shown larger, and
  every duplicate has a small picture (tap to enlarge; a “Thumbnails” tickbox
  turns them off). Pictures come from the preview built into each CR2 file, are
  shrunk and cached in the add-on's private storage, and respect the camera's
  rotation.
- **Google Photos tab**: connect once, then developed JPEGs can be added to
  Google Photos (no albums, add-only access, each JPEG sent once, nothing ever
  deleted). *Automatic upload* is off by default; **Upload now** always works.
  New entity `sensor.photo_manager_google_photos`. New options
  `google_client_id` and `google_client_secret`.
- **Develop now** no longer needs the automatic overnight switch — that switch
  now only controls the overnight run.
- Fixed a rare case where the overnight run could start twice in one night.

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
