#!/usr/bin/env python3
"""Install a NanoKVM application package (akkijp/NanoKVM fork).

The fork replaces upstream's maintenance updater, which downloaded an
unverified latest.zip from the upstream CDN. This version only installs a
package whose SHA-512 matches, and it uses the same package layout the
server's online updater checks (server/service/application/archive.go).

Usage:
  update-nanokvm.py --file nanokvm_2.5.4.1.tar.gz --sha512 <base64 or hex>
      Install a package copied to the device by hand. This is the bootstrap
      path for devices whose built-in updater rejects four-part versions.
  update-nanokvm.py [--version 2.5.4.1] [--sha512 <digest>]
      Download from the fork's GitHub Releases. Without --version, the
      newest release's latest.json is used. --sha512 pins the expected
      digest instead of trusting the one in latest.json.
  Add --dry-run to verify the package without installing it.

It installs the same way as the web updater (server/service/application):
the running app is moved to /root/old and the new one to /kvmapp, then the
app is restarted, and its first start runs the migration that installs the
new boot scripts. On top of that it
  - unpacks, checks and stages the new version (/kvmapp.new) before anything
    is stopped,
  - confirms both daemons are gone before moving files, and puts the old app
    back and starts it if a move fails,
  - reports a new version whose daemons do not stay up (exit status 1).
It does not roll back automatically. The full way back is to write the
official NanoKVM image to the microSD card again (FORK.md).
"""

import argparse
import base64
import binascii
import fcntl
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

RELEASES = "https://github.com/akkijp/NanoKVM/releases"
APP_DIR = "/kvmapp"
BACKUP_DIR = "/root/old"
WORK_PARENT = "/root"
LOCK_FILE = "/run/nanokvm-update.lock"
# The installed init script stops the running app. The new app is started with
# its own script, so its first start already runs the fork's boot steps.
STOP_SCRIPT = "/etc/init.d/S95nanokvm"
APP_INIT_SCRIPT = "system/init.d/S95nanokvm"
STAGE_DIR = "/kvmapp.new"

DAEMONS = ("NanoKVM-Server", "kvm_system")
STOP_TIMEOUT = 20
START_TIMEOUT = 60
START_STABLE = 15

MAX_PACKAGE_SIZE = 1 << 30
MAX_EXPANDED_SIZE = 2 << 30
MAX_ENTRIES = 100_000
MAX_MANIFEST_SIZE = 64 << 10

VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?$")
REQUIRED_FILES = (
    "version",
    "server/NanoKVM-Server",
    "kvm_system/kvm_system",
    "system/init.d/S95nanokvm",
    "system/update-nanokvm.py",
    # Tells kvm_system to run its migration, which installs the new init
    # scripts and this updater into /etc.
    "kvm_new_app",
)


class UpdateError(Exception):
    pass


def parse_digest(value: str) -> bytes:
    value = value.strip()
    if re.fullmatch(r"[0-9a-fA-F]{128}", value):
        return bytes.fromhex(value)
    try:
        digest = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as e:
        raise UpdateError(f"invalid sha512 value: {e}")
    if len(digest) != 64:
        raise UpdateError("invalid sha512 value: not a SHA-512 digest")
    return digest


def fetch(url: str, dest, limit: int) -> None:
    """Stream url into the open file dest, refusing more than limit bytes."""
    print(f"Downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "nanokvm-updater"})
    with urllib.request.urlopen(request, timeout=60) as response:
        if response.status != 200:
            raise UpdateError(f"download failed: HTTP {response.status}")
        written = 0
        while True:
            chunk = response.read(1 << 16)
            if not chunk:
                break
            written += len(chunk)
            if written > limit:
                raise UpdateError(f"download is larger than {limit} bytes")
            dest.write(chunk)


def fetch_manifest(version: str, work: str) -> dict:
    if version:
        url = f"{RELEASES}/download/{version}/latest.json"
    else:
        url = f"{RELEASES}/latest/download/latest.json"
    path = os.path.join(work, "latest.json")
    with open(path, "wb") as f:
        fetch(url, f, MAX_MANIFEST_SIZE)
    with open(path, "rb") as f:
        try:
            manifest = json.load(f)
        except ValueError as e:
            raise UpdateError(f"invalid latest.json: {e}")
    if not isinstance(manifest, dict):
        raise UpdateError("invalid latest.json: not an object")
    found = manifest.get("version")
    if not isinstance(found, str) or not VERSION_RE.match(found):
        raise UpdateError("invalid latest.json: bad version")
    if version and found != version:
        raise UpdateError(f"latest.json is for {found}, expected {version}")
    if manifest.get("name") != f"nanokvm_{found}.tar.gz":
        raise UpdateError("invalid latest.json: name does not match version")
    if not isinstance(manifest.get("sha512"), str):
        raise UpdateError("invalid latest.json: missing sha512")
    return manifest


def sha512_of(path: str) -> bytes:
    h = hashlib.sha512()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.digest()


def copy_limited(source: str, dest: str, limit: int) -> None:
    """Copy source to dest so the bytes we verify are the bytes we unpack."""
    written = 0
    with open(source, "rb") as src, open(dest, "xb") as out:
        for chunk in iter(lambda: src.read(1 << 16), b""):
            written += len(chunk)
            if written > limit:
                raise UpdateError("package is larger than the limit")
            out.write(chunk)


def version_from_name(path: str) -> str:
    m = re.fullmatch(r"nanokvm_([0-9.]+)\.tar\.gz", os.path.basename(path))
    if not m or not VERSION_RE.match(m.group(1)):
        raise UpdateError("package file must be named nanokvm_<version>.tar.gz")
    return m.group(1)


def check_members(archive: tarfile.TarFile, root: str) -> list:
    members, seen, expanded, has_root = [], set(), 0, False
    for member in archive:
        name = member.name
        if not name or name.startswith("/"):
            raise UpdateError(f"invalid archive path {name!r}")
        clean = posixpath.normpath(name)
        if clean in (".", "..") or clean.startswith("../"):
            raise UpdateError(f"invalid archive path {name!r}")
        if clean != root and not clean.startswith(root + "/"):
            raise UpdateError(f"archive entry {name!r} is outside {root}")
        if not (member.isreg() or member.isdir()):
            raise UpdateError(f"unsupported archive entry type for {name!r}")
        if clean in seen:
            raise UpdateError(f"duplicate archive entry {name!r}")
        seen.add(clean)
        if len(seen) > MAX_ENTRIES:
            raise UpdateError("archive has too many entries")
        if member.isreg():
            expanded += member.size
            if expanded > MAX_EXPANDED_SIZE:
                raise UpdateError("archive expands to more than the limit")
        if clean == root and member.isdir():
            has_root = True
        member.name = clean
        members.append(member)
    if not has_root:
        raise UpdateError(f"archive has no {root}/ directory")
    return members


def extract(package: str, version: str, work: str) -> str:
    root = f"nanokvm_{version}"
    dest = os.path.join(work, "extract")
    os.mkdir(dest)
    with tarfile.open(package, "r:gz") as archive:
        members = check_members(archive, root)
        for member in members:
            target = os.path.join(dest, *member.name.split("/"))
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            source = archive.extractfile(member)
            with source, open(target, "xb") as out:
                shutil.copyfileobj(source, out)
    source_dir = os.path.join(dest, root)
    for required in REQUIRED_FILES:
        if not os.path.isfile(os.path.join(source_dir, required)):
            raise UpdateError(f"package is missing {required}")
    with open(os.path.join(source_dir, "version")) as f:
        if f.read().strip() != version:
            raise UpdateError("package version file does not match")
    return source_dir


def service(script: str, action: str) -> int:
    print(f"{script} {action}")
    try:
        return subprocess.run([script, action]).returncode
    except OSError as e:
        print(f"cannot run {script}: {e}", file=sys.stderr)
        return 127


def app_script() -> str:
    return os.path.join(APP_DIR, APP_INIT_SCRIPT)


def running(name: str) -> bool:
    return subprocess.run(["pidof", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def started() -> bool:
    """Both daemons come up within START_TIMEOUT and are still running START_STABLE seconds later."""
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if all(running(d) for d in DAEMONS):
            time.sleep(START_STABLE)
            return all(running(d) for d in DAEMONS)
        time.sleep(1)
    return False


def stopped() -> bool:
    """Wait for both daemons to be gone. Older init scripts return before they exit."""
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        if not any(running(d) for d in DAEMONS):
            return True
        time.sleep(1)
    return False


def stop_old() -> None:
    """Stop the running app and confirm both daemons are gone; retries cover another service
    operation holding the init script's lock. On failure nothing has been changed."""
    for attempt in range(3):
        if attempt:
            time.sleep(5)
        service(STOP_SCRIPT, "stop")
        if stopped():
            return
    # A partial stop can leave the web server down. Bring the old version back up.
    if not all(running(d) for d in DAEMONS):
        service(STOP_SCRIPT, "restart")
    raise UpdateError("could not stop the nanokvm service; nothing was changed")


def prepare(source_dir: str) -> None:
    """Set permissions in the staged copy, before anything is stopped."""
    for root, dirs, files in os.walk(source_dir):
        os.chmod(root, 0o755)
        for name in files:
            os.chmod(os.path.join(root, name), 0o755)


def switch() -> None:
    """Move the old app to BACKUP_DIR and the staged one to APP_DIR. If any step fails,
    the old app is put back in APP_DIR before the error is raised."""
    moved_old = False
    try:
        if os.path.exists(BACKUP_DIR):
            shutil.rmtree(BACKUP_DIR)
        os.rename(APP_DIR, BACKUP_DIR)
        moved_old = True
        os.rename(STAGE_DIR, APP_DIR)
    except Exception:
        if moved_old and not os.path.exists(APP_DIR):
            os.rename(BACKUP_DIR, APP_DIR)
        raise


def install(source_dir: str, version: str) -> None:
    prepare(source_dir)
    if os.path.exists(STAGE_DIR):
        shutil.rmtree(STAGE_DIR)
    shutil.move(source_dir, STAGE_DIR)
    try:
        stop_old()
    except UpdateError:
        shutil.rmtree(STAGE_DIR, ignore_errors=True)
        raise

    try:
        switch()
    except Exception as e:
        shutil.rmtree(STAGE_DIR, ignore_errors=True)
        service(app_script(), "restart")
        raise UpdateError(f"could not put the new version in place; the previous version was started again: {e}")
    os.sync()

    if service(app_script(), "restart") != 0 or not started():
        raise UpdateError(
            f"{version} is installed but did not start. The previous version is in {BACKUP_DIR}; "
            "to recover, write the official NanoKVM image to the microSD card (see FORK.md)"
        )
    print(f"Previous version kept in {BACKUP_DIR}")


def acquire_lock():
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise UpdateError("another update is already running")
    return lock


def run(args) -> str:
    lock = acquire_lock()
    # An interrupted earlier run can leave its work directory behind.
    for name in os.listdir(WORK_PARENT):
        if name.startswith(".kvm-update-"):
            shutil.rmtree(os.path.join(WORK_PARENT, name), ignore_errors=True)
    work = tempfile.mkdtemp(prefix=".kvm-update-", dir=WORK_PARENT)
    try:
        return run_locked(args, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        lock.close()


def run_locked(args, work: str) -> str:
    if args.file:
        if not args.sha512:
            raise UpdateError("--file requires --sha512")
        version = version_from_name(args.file)
        if args.version and args.version != version:
            raise UpdateError("--version does not match the file name")
        expected = parse_digest(args.sha512)
        package = os.path.join(work, os.path.basename(args.file))
        copy_limited(args.file, package, MAX_PACKAGE_SIZE)
    else:
        if args.version and not VERSION_RE.match(args.version):
            raise UpdateError("invalid --version")
        manifest = fetch_manifest(args.version, work)
        version = manifest["version"]
        expected = parse_digest(args.sha512 or manifest["sha512"])
        package = os.path.join(work, manifest["name"])
        with open(package, "xb") as f:
            fetch(f"{RELEASES}/download/{version}/{manifest['name']}", f, MAX_PACKAGE_SIZE)

    if sha512_of(package) != expected:
        raise UpdateError("sha512 does not match; package not installed")
    print(f"sha512 verified for {version}")

    source_dir = extract(package, version, work)
    if args.dry_run:
        print("Dry run: package is valid, nothing installed")
        return version
    install(source_dir, version)
    return version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--file", help="local nanokvm_<version>.tar.gz")
    parser.add_argument("--sha512", help="expected SHA-512 (base64 or hex)")
    parser.add_argument("--version", help="release to download, e.g. 2.5.4.1")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        version = run(args)
    except Exception as e:
        print(f"update failed\n{e}", file=sys.stderr)
        return 1
    if not args.dry_run:
        print(f"Successfully updated to version: {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
