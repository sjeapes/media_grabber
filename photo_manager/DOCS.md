# Photo Manager

Finds the Canon CR2 photos on your NAS and tells you what you have: how many,
which years and cameras, which folders, and which files are exact duplicates.
It is the first step towards developing the RAW files overnight, picking the
keepers on your phone and uploading them to Google Photos.

**This version (0.1.0) is read-only.** It never changes, moves or deletes
anything on your NAS.

## Before you start

1. **Add your NAS** in *Settings → System → Storage → Add network storage*
   (usage **Media** or **Share**, any name). Photo Manager finds it
   automatically — nothing to type in. If you add more than one, choose which
   to use on the add-on's **Storage** tab.
2. Open **Photo Manager** from the sidebar, go to **Storage → Photo folder →
   Choose…** and open the folder that holds your CR2 files. Sub-folders are
   included automatically.
3. On the **Library** tab press **Scan now**.

## What the scan reports

- Number of CR2 photos, total size, earliest and latest capture date
- Photos per year, per camera and per folder
- How many photos have a matching camera JPEG next to them (same file name)
- **Exact duplicates**: files that are byte-for-byte identical, and how much
  space the extra copies use. Nothing is deleted.

Capture time and camera come from each file's EXIF data. A file that can't be
read, or has no capture time, is listed as "Unknown" by year.

A re-scan only re-reads files that are new or changed, so it is much quicker
than the first one. To find duplicates, only files of identical size are read
in full; every other file is just looked at briefly.

## Folders

| Folder | Default | Used for |
| --- | --- | --- |
| Photo folder | *you choose* | Your CR2 files (read-only) |
| Developed photos | `PhotoManager/Processed` | JPEGs made from your RAW files (next version) |
| Quarantine | `PhotoManager/Quarantine` | Rejected photos, moved here and never deleted (later version) |

Folders are saved relative to the NAS, so they still work if the NAS is
re-mounted under a different name. The three folders can't be the same or
inside each other. Nothing is created on the NAS in this version.

## Safety

- The photo folder is only ever opened for reading.
- If the NAS isn't mounted, nothing runs — Photo Manager never falls back to
  Home Assistant's own disk.
- The scan database lives in the add-on's private data folder, not on your NAS.

## Home Assistant entities

| Entity | Shows |
| --- | --- |
| `binary_sensor.photo_manager_storage_problem` | On if the NAS or photo folder isn't usable (reasons in attributes) |
| `sensor.photo_manager_status` | `idle`, `walking`, `reading` or `hashing` while a scan runs |
| `sensor.photo_manager_photos` | Number of CR2 photos from the last scan, with size, date range, cameras and duplicate totals as attributes |

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `low_space_gb` | `20` | Warn below this much free space on the NAS |
