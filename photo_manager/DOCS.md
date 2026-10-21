# Photo Manager

Finds the Canon CR2 photos on your NAS and tells you what you have: how many,
which years and cameras, which folders, and which files are exact duplicates.
It also develops the RAW files into JPEGs overnight with darktable, and lets you
move duplicate copies to a quarantine folder after a checksum check. It can also add the developed JPEGs to Google Photos.

**Your existing photos are never changed or deleted.** Developing is switched
**off** until you turn it on, and the only things Photo Manager ever adds to
your NAS are new JPEG files and the quarantine folder.

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

## Developing (Develop tab)

1. Optionally turn on **Automatic overnight developing**. It is off by default;
   while off, nothing runs by itself. **Develop now** (step 5) always works,
   whatever this switch says.
2. Choose **where to save the JPEGs**:
   - *In a separate folder* (default): your photo folder stays exactly as it is.
   - *Next to each RAW file*: adds `IMG_1234_developed.jpg` beside
     `IMG_1234.CR2`. Existing files are never changed or replaced.
3. To **test on a few photos**, under *Which photos* choose a single folder
   (inside your photo folder). It applies to manual and overnight runs; press
   **All photos** to go back.
4. Optionally tick **Skip photos that already have a JPEG** (a JPEG with the
   same name in the same folder, such as the camera's own).
5. Press **Develop now**, or switch on overnight developing and leave it to the window.

How it behaves:

- darktable runs on a private copy of each RAW file, so it can't write into
  your photo folder. Settings are automatic; custom styles can come later.
- A JPEG is checked, then saved under a temporary name and renamed. If a file
  with that name already exists, it is left alone and the photo is skipped.
- Identical duplicate RAWs are developed once.
- A photo that fails is tried again on later runs (three attempts, then it is
  left alone until you press **Retry failed**).
- It stops when the overnight window ends (resumes next night), when you turn
  it off or press Stop, or when the NAS has less than `develop_min_free_gb` free.
- Capture time and camera are kept in the JPEG.

## Quarantine (Quarantine tab)

Duplicate groups (byte-identical files) are listed with a checkbox on every
copy. Tick the copies to move; at least one must stay unticked (**Keep first,
tick the rest** is a shortcut). Press **Check checksums** to read the files
again and compare their CRC32 with the copy that stays — each shows ✓ same or
✗ different. **Move to quarantine** only unlocks when everything matches, and
the server re-checks before moving, so nothing different can be moved. Any
change to the ticks asks for a new check.

Each copy has a small thumbnail (tap to enlarge), and the copy that stays is shown
larger. Untick **Thumbnails** at the top to hide them. Pictures come from the preview
inside each CR2 file.

Moved files keep their folder structure under the quarantine folder, and
**Restore** puts them back. Photo Manager never deletes anything: empty the
quarantine folder yourself when you're sure, then press **Clear emptied**.
Moves are paused while a scan or developing run is in progress.

## Google Photos (Google Photos tab)

Adds the JPEGs Photo Manager developed to your Google Photos — no albums. It has
**add-only** permission: it can't see, change or delete anything in your library.

One-off setup (Google requires you to create your own key for this):

1. In the [Google Cloud console](https://console.cloud.google.com/) create a project
   and enable the **Photos Library API**.
2. *APIs & Services → OAuth consent screen*: choose **External**, add yourself as a
   test user, then press **Publish app**. (Skip publishing and Google ends the
   sign-in after 7 days. The “unverified app” warning is fine for personal use.)
3. *Credentials → Create credentials → OAuth client ID*, type **Desktop app**.
4. Put the client ID and secret in this add-on's **Configuration**
   (`google_client_id`, `google_client_secret`), save, and restart the add-on.
5. On the **Google Photos** tab press **Connect Google Photos**, approve in Google,
   and paste the address of the page you land on (it starts with
   `http://localhost` and won't load — that's expected).

Then **Upload now** sends every developed JPEG not yet sent, and **Automatic
upload** (off by default) does it by itself after each develop run. Each JPEG is
sent once; failures are retried up to three times. JPEGs that were already on your
NAS before Photo Manager got there are never uploaded.

Google only lets apps see photos they uploaded themselves, so Photo Manager can't
tell whether a photo is already in your library from elsewhere.

## Folders

| Folder | Default | Used for |
| --- | --- | --- |
| Photo folder | *you choose* | Your CR2 files |
| Developed photos | `PhotoManager/Processed` | JPEGs made from your RAW files (not used when saving next to the RAW files) |
| Quarantine | `PhotoManager/Quarantine` | Duplicate copies you move here, never deleted |

Folders are saved relative to the NAS, so they still work if the NAS is
re-mounted under a different name. The three folders can't be the same or
inside each other. The developed and quarantine folders are created when first needed.

## Safety

- Scanning only reads. Developing adds JPEG files and never overwrites; quarantine moves
  files only after a checksum match.
- If the NAS isn't mounted, nothing runs — Photo Manager never falls back to
  Home Assistant's own disk.
- The scan database lives in the add-on's private data folder, not on your NAS.

## Home Assistant entities

| Entity | Shows |
| --- | --- |
| `binary_sensor.photo_manager_storage_problem` | On if the NAS or photo folder isn't usable (reasons in attributes) |
| `sensor.photo_manager_status` | `idle`, `walking`, `reading`, `hashing` or `developing` |
| `sensor.photo_manager_developed` | Photos developed, with to-develop, failed, last run and settings as attributes |
| `sensor.photo_manager_quarantine` | Files in quarantine, with folder and size |
| `sensor.photo_manager_google_photos` | Photos uploaded to Google Photos, with waiting, failed and connection attributes |
| event `photo_manager_develop_finished` | Fired when a develop run ends (developed, failed, why it stopped) |
| `sensor.photo_manager_photos` | Number of CR2 photos from the last scan, with size, date range, cameras and duplicate totals as attributes |

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `low_space_gb` | `20` | Warn below this much free space on the NAS |
| `develop_start_hour` | `1` | Overnight developing starts at this hour (local time) |
| `develop_end_hour` | `6` | …and pauses at this hour (same as start = all day) |
| `develop_min_free_gb` | `5` | Stop developing below this much free space on the NAS |
| `jpeg_quality` | `92` | JPEG quality (50–100) |
| `google_client_id` / `google_client_secret` | *empty* | Your Google OAuth client, needed only for Google Photos upload |
