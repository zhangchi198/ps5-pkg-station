"""Install PS5 UltraPack into /opt/ultrapack: its source tree at the release tag (backend/cli.py imports
top-level modules such as ultra_core and i18n), plus the native Linux x64 tools (ffpfsc-pkg-tool, 7-Zip,
...) from the official release build. Both downloads are checked against pinned SHA-256 sums."""
import hashlib, io, os, shutil, sys, tarfile, urllib.request

version, release_sha, source_sha = sys.argv[1], sys.argv[2].lower(), sys.argv[3].lower()
REPO = "https://github.com/knutwurst/ps5-ultrapack"


def fetch(url, want, what):
    data = urllib.request.urlopen(url, timeout=600).read()
    got = hashlib.sha256(data).hexdigest()
    if got != want:
        sys.exit(f"UltraPack {version} {what}: checksum mismatch (got {got}, expected {want})")
    return tarfile.open(fileobj=io.BytesIO(data))


fetch(f"{REPO}/archive/refs/tags/v{version}.tar.gz", source_sha, "source").extractall("/tmp/src", filter="tar")
shutil.move(f"/tmp/src/ps5-ultrapack-{version}", "/opt/ultrapack")
for junk in ("docs", "images", "assets/screenshots"):
    shutil.rmtree(f"/opt/ultrapack/{junk}", ignore_errors=True)

fetch(f"{REPO}/releases/download/v{version}/PS5-UltraPack-{version}-linux-x64.tar.gz", release_sha,
      "linux release").extractall("/tmp/rel", filter="tar")
native = "/opt/ultrapack/backend/native"
shutil.rmtree(native, ignore_errors=True)
shutil.move("/tmp/rel/PS5 UltraPack/_internal/backend/native", native)
for tool in ("7zz", "ffpfsc-pkg-tool"):
    os.chmod(f"{native}/{tool}", 0o755)
shutil.rmtree("/tmp/src")
shutil.rmtree("/tmp/rel")
print(f"UltraPack {version} installed: source tree + linux x64 native tools (sha256 ok)")
