FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tini tzdata \
      darktable \
 && rm -rf /var/lib/apt/lists/* \
 && darktable-cli --version

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

COPY rootfs/ /

WORKDIR /app
EXPOSE 8099
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "-m", "photomanager"]
