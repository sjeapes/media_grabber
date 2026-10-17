#!/bin/sh
# Install/refresh get_iplayer from its latest GitHub release tag.
set -eu
TAG=$(curl -fsSLI -o /dev/null -w '%{url_effective}' https://github.com/get-iplayer/get_iplayer/releases/latest | sed 's#.*/##')
case "$TAG" in v*) ;; *) echo "Could not resolve get_iplayer release tag (got '$TAG')" >&2; exit 1 ;; esac
TMP=$(mktemp)
curl -fsSL "https://raw.githubusercontent.com/get-iplayer/get_iplayer/${TAG}/get_iplayer" -o "$TMP"
head -1 "$TMP" | grep -q perl || { echo "Downloaded get_iplayer does not look like a Perl script" >&2; exit 1; }
install -m 0755 "$TMP" /usr/local/bin/get_iplayer
rm -f "$TMP"
echo "get_iplayer ${TAG} installed"
