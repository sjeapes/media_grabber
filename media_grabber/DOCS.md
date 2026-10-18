# Media Grabber

Download BBC iPlayer / BBC Sounds programmes (get_iplayer) and YouTube or other
non-DRM video (yt-dlp) straight to your NAS, from a panel in the Home Assistant
sidebar.

## Before you start

1. **Add your NAS** in *Settings → System → Storage → Add network storage*
   (usage **Media** or **Share**, any name). Media Grabber finds it
   automatically — nothing to type in. If you add more than one, choose which
   to use on the add-on's **Storage** tab. You can also choose **Home
   Assistant storage** (`/media/MediaGrabber` on HA's own disk).
2. **Add a UK WireGuard config** on the add-on's **VPN** tab (iPlayer only
   works from UK IP addresses). For Proton VPN: account.protonvpn.com →
   Downloads → WireGuard configuration → GNU/Linux → a UK server, NetShield
   off. Alternatively drop the file in the add-on config folder as
   `wireguard.conf`.

## How the VPN works (split tunnel)

The tunnel runs in userspace (wireproxy) inside this add-on and is exposed as a
proxy on `127.0.0.1` only. Only get_iplayer is pointed at it — Home Assistant,
other add-ons, the web UI and NAS traffic all use your normal connection. No
extra privileges are needed. If the tunnel is down, or its exit isn't in the
UK, iPlayer jobs are refused rather than leaking out on your home IP.

## Storage safety

- Media Grabber never falls back from a missing NAS to Home Assistant's disk
  on its own. If the chosen NAS isn't mounted, downloads pause (and any running
  download is stopped) until it's back or you pick another location.
- Home Assistant storage is opt-in, and downloads there stop when HA's disk
  has less than `local_min_free_gb` free, so Home Assistant itself keeps
  working.
- The **Storage** tab shows the location, free space and a write test.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `iplayer_folder` / `youtube_folder` | `iPlayer` / `YouTube` | Sub-folders on the NAS |
| `low_space_gb` | `50` | Warn below this much free space |
| `local_min_free_gb` | `8` | With Home Assistant storage selected, stop downloads below this much free space on HA's disk |
| `tv_quality` | `fhd,hd,sd` | get_iplayer `--tv-quality` |
| `youtube_format` | ≤1080p | yt-dlp `-f` format |
| `max_concurrent_downloads` | `1` | Parallel downloads |
| `pvr_interval_hours` | `4` | How often series recordings are checked |
| `iplayer_requires_uk_vpn` | `true` | Turn off only if HA itself is in the UK |
| `vpn_for_youtube` | `false` | Also send yt-dlp through the tunnel |
| `auto_update_tools` | `true` | Update yt-dlp and get_iplayer on each start |

## Home Assistant entities and events

- `binary_sensor.media_grabber_nas_problem` — storage problem; attributes include `location`, `mode` (`nas`/`local`), `needs_choice`, `network_storage_found`
- `sensor.media_grabber_nas_free` (GB free at the current location)
- `binary_sensor.media_grabber_uk_vpn`
- `sensor.media_grabber_queue`
- `sensor.media_grabber_last_download`
- Event `media_grabber_finished` with `title`, `status`, `kind`, `error`, `files`

Example: notify when a download fails —

```yaml
triggers:
  - trigger: event
    event_type: media_grabber_finished
    event_data: { status: failed }
actions:
  - action: notify.mobile_app_pixel_8a
    data:
      title: Media Grabber
      message: "{{ trigger.event.data.title }} failed: {{ trigger.event.data.error }}"
```

## Not supported

ITVX, Channel 4, My5, STV and the paid streaming services use DRM-protected
streams; this add-on does not download them.

BBC content is for personal use and requires a TV Licence.
