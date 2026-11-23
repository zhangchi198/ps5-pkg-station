# Third-party software

PS5 PKG Station is a front end. The packages are built by **PS5 UltraPack**
(<https://github.com/knutwurst/ps5-ultrapack>, GPL-3.0). It is not part of this repository:
the Docker build downloads UltraPack's source at the pinned release tag and the native tools from
its official Linux x64 release, checks both against pinned SHA-256 sums, and installs them to
`/opt/ultrapack`, together with UltraPack's own `LICENSE` and `NOTICE`. That release bundles further tools under their own licenses (7-Zip,
LibOrbisPkg / LibProsperoPkg, MkPFS, ampr-emu and others), which are listed in
`/opt/ultrapack/NOTICE` and `/opt/ultrapack/backend/native/` inside the image.

Prebuilt images on ghcr.io therefore contain UltraPack's GPL-3.0 programs unmodified; their
source is available at the URL above, at the tag named by `ULTRAPACK_VERSION` in the Dockerfile.

Python packages installed into the image: FastAPI, Uvicorn, cryptography, lz4, psutil, rarfile,
py7zr, Pillow, backports.zstd (each under its own license).
