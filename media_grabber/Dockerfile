FROM python:3.12-slim-bookworm

ARG BUILD_ARCH=amd64

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1

# get_iplayer runtime (Perl + modules), ffmpeg/AtomicParsley for muxing/tagging, unzip for deno
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl unzip tini \
      ffmpeg atomicparsley \
      perl libwww-perl liblwp-protocol-https-perl libmojolicious-perl \
      libxml-libxml-perl libcgi-pm-perl libhtml-parser-perl \
      chromium fonts-liberation fonts-noto-core \
 && rm -rf /var/lib/apt/lists/* \
 && test -x /usr/bin/chromium

# Arch-specific binary names
RUN case "${BUILD_ARCH}" in \
      amd64)   echo "amd64 x86_64"   > /tmp/arch ;; \
      aarch64) echo "arm64 aarch64"  > /tmp/arch ;; \
      *) echo "unsupported arch ${BUILD_ARCH}" && exit 1 ;; \
    esac

# wireproxy: userspace WireGuard exposed as a local HTTP proxy (split tunnel, no NET_ADMIN)
RUN GOARCH=$(cut -d' ' -f1 /tmp/arch) \
 && for repo in whyvl/wireproxy pufferffish/wireproxy; do \
      curl -fsSL "https://github.com/${repo}/releases/latest/download/wireproxy_linux_${GOARCH}.tar.gz" -o /tmp/wp.tgz && break; \
    done \
 && tar -xzf /tmp/wp.tgz -C /usr/local/bin wireproxy && rm /tmp/wp.tgz \
 && chmod +x /usr/local/bin/wireproxy && test -x /usr/local/bin/wireproxy

# deno: JavaScript runtime yt-dlp needs for YouTube
RUN UARCH=$(cut -d' ' -f2 /tmp/arch) \
 && curl -fsSL -o /tmp/deno.zip "https://github.com/denoland/deno/releases/latest/download/deno-${UARCH}-unknown-linux-gnu.zip" \
 && unzip -q /tmp/deno.zip -d /usr/local/bin && rm /tmp/deno.zip \
 && deno --version

# get_iplayer (latest release; refreshed again at start-up when auto_update_tools is on)
COPY rootfs/app/update_get_iplayer.sh /usr/local/bin/update_get_iplayer.sh
RUN chmod +x /usr/local/bin/update_get_iplayer.sh \
 && /usr/local/bin/update_get_iplayer.sh \
 && get_iplayer --help >/dev/null

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

COPY rootfs/ /

WORKDIR /app
EXPOSE 8099
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "-m", "mediagrabber"]
