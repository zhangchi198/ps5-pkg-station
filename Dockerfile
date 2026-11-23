# PS5 PKG Station - web UI + scheduler around PS5 UltraPack's command-line builder.
# UltraPack (GPL-3.0, https://github.com/knutwurst/ps5-ultrapack) is downloaded from its official
# release at build time and checked against a pinned SHA-256; nothing of it is stored in this repo.
FROM ubuntu:24.04

ARG ULTRAPACK_VERSION=2.7.1
ARG ULTRAPACK_SHA256=f47045c40e32cb4860d1f62e13696964728be2bb28bda895db9035100df07591

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PS5PKG_PROFILE=docker PS5PKG_STATE=/config/state PS5PKG_LOGS=/config/logs \
    HOME=/tmp DOTNET_CLI_HOME=/tmp DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    TZ=Etc/UTC

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv ca-certificates tzdata procps tini libicu74 libssl3 libstdc++6 zlib1g \
    && rm -rf /var/lib/apt/lists/*

# versions pinned to UltraPack's own requirements-build.txt
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir \
        cryptography==50.0.1 lz4==4.4.5 psutil==7.2.2 rarfile==4.5 py7zr==1.1.3 pillow==12.3.0 backports.zstd==1.8.0 \
        fastapi==0.115.6 uvicorn==0.32.1

# UltraPack's Linux x64 release: its Python backend plus the native tools (ffpfsc-pkg-tool, 7-Zip)
COPY docker/fetch_ultrapack.py /tmp/fetch_ultrapack.py
RUN python3 /tmp/fetch_ultrapack.py "$ULTRAPACK_VERSION" "$ULTRAPACK_SHA256" && rm /tmp/fetch_ultrapack.py

COPY app /app
COPY LICENSE NOTICE.md /app/
RUN python3 -m compileall -q /app >/dev/null || true

EXPOSE 8765
VOLUME ["/config", "/work"]
HEALTHCHECK --interval=60s --timeout=5s CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=4)"
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/opt/venv/bin/python", "-m", "uvicorn", "--app-dir", "/app/web", "server:app", "--host", "0.0.0.0", "--port", "8765"]
