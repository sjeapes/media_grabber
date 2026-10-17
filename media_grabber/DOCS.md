# Media Grabber

Download BBC iPlayer / BBC Sounds programmes (get_iplayer) and YouTube or other
non-DRM video (yt-dlp) straight to your NAS, from a panel in the Home Assistant
sidebar.

## Before you start

1. **Add your NAS** in *Settings → System → Storage → Add network storage*:
   name `nas`, usage **Media**. It appears in the add-on as `/media/nas`.
   If you use another name, change `nas_path` below.
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

## NAS safety

If `/media/nas` isn't a network mount (for example the NAS is offline and the
folder is empty), downloads are paused so they can't fill the Home Assistant
disk. The **Storage** tab shows mount, free space and a write test.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `nas_path` | `/media/nas` | Where the NAS is mounted |
| `iplayer_folder` / `youtube_folder` | `iPlayer` / `YouTube` | Sub-folders on the NAS |
| `require_network_mount` | `true` | Refuse to write unless `nas_path` is a CIFS/NFS mount |
| `low_space_gb` | `50` | Warn below this much free space |
| `tv_quality` | `fhd,hd,sd` | get_iplayer `--tv-quality` |
| `youtube_format` | ≤1080p | yt-dlp `-f` format |
| `max_concurrent_downloads` | `1` | Parallel downloads |
| `pvr_interval_hours` | `4` | How often series recordings are checked |
| `iplayer_requires_uk_vpn` | `true` | Turn off only if HA itself is in the UK |
| `vpn_for_youtube` | `false` | Also send yt-dlp through the tunnel |
| `auto_update_tools` | `true` | Update yt-dlp and get_iplayer on each start |

## Home Assistant entities and events

- `binary_sensor.media_grabber_nas_problem`
- `sensor.media_grabber_nas_free` (GB)
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
