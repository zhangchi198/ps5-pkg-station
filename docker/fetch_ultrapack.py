"""Download PS5 UltraPack's official Linux x64 release, check its SHA-256, install its backend to /opt/ultrapack."""
import hashlib, io, os, shutil, sys, tarfile, urllib.request

version, sha256 = sys.argv[1], sys.argv[2].lower()
url = (f"https://github.com/knutwurst/ps5-ultrapack/releases/download/v{version}/"
       f"PS5-UltraPack-{version}-linux-x64.tar.gz")
data = urllib.request.urlopen(url, timeout=600).read()
got = hashlib.sha256(data).hexdigest()
if got != sha256:
    sys.exit(f"UltraPack {version}: checksum mismatch (got {got}, expected {sha256})")
tarfile.open(fileobj=io.BytesIO(data)).extractall("/tmp/up", filter="tar")
root = "/tmp/up/PS5 UltraPack"
os.makedirs("/opt/ultrapack")
shutil.move(root + "/_internal/backend", "/opt/ultrapack/backend")
for f in ("LICENSE", "NOTICE"):
    shutil.copy(os.path.join(root, f), os.path.join("/opt/ultrapack", f))
for tool in ("7zz", "ffpfsc-pkg-tool"):
    os.chmod(f"/opt/ultrapack/backend/native/{tool}", 0o755)
shutil.rmtree("/tmp/up")
print(f"UltraPack {version} installed ({len(data) >> 20} MiB, sha256 ok)")
