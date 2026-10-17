# Stephen's Home Assistant Apps

[![Add repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fsjeapes%2Fmedia_grabber)

| App | Description |
| --- | --- |
| [Media Grabber](media_grabber/DOCS.md) | BBC iPlayer (get_iplayer) + YouTube (yt-dlp) downloader to a NAS, with NAS health and a split-tunnel UK VPN |

## Development

```sh
cd media_grabber
pip install flask waitress pytest
python -m pytest -q tests
```
