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
  update-nanokvm.py --rollback
      Put back the previous version and the boot files it replaced.

Before installing, the app (/kvmapp -> /root/old) and the boot files that the
new version's first start rewrites (/etc/init.d, this updater, the HDMI
driver -> /root/old-etc) are saved. The new version must come up (both
daemons running, web port answering) or everything is put back. When its
first start reboots the device, /etc/kvm/update-pending makes the next boot
run --confirm-boot, which does the same check and rolls back on failure.
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
import socket
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

# Boot files the migration of a new app version rewrites
# (support/sg2002/kvm_system/main/lib/system_init/system_init.cpp).
ETC_BACKUP_DIR = "/root/old-etc"
ETC_INIT_DIR = "/etc/init.d"
ETC_FILES = ("/etc/kvm/update-nanokvm.py", "/mnt/system/ko/soph_mipi_rx.ko")
PENDING_FILE = "/etc/kvm/update-pending"

DAEMONS = ("NanoKVM-Server", "kvm_system")
WEB_PORTS = (80, 443)
HEALTH_TIMEOUT = 90
HEALTH_STABLE = 10
REBOOT = ["reboot"]

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


def web_answers() -> bool:
    for port in WEB_PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return True
        except OSError:
            pass
    return False


def healthy() -> bool:
    """Both daemons stay up and the web port answers, within HEALTH_TIMEOUT."""
    deadline = time.monotonic() + HEALTH_TIMEOUT
    while time.monotonic() < deadline:
        if all(running(d) for d in DAEMONS) and web_answers():
            time.sleep(HEALTH_STABLE)
            if all(running(d) for d in DAEMONS):
                return True
        time.sleep(2)
    return False


def copy_entry(source: str, dest: str) -> None:
    if os.path.isdir(source):
        shutil.copytree(source, dest, symlinks=True)
    else:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(source, dest, follow_symlinks=False)


def backup_etc() -> None:
    """Save the boot files the new version's migration will rewrite."""
    if os.path.exists(ETC_BACKUP_DIR):
        shutil.rmtree(ETC_BACKUP_DIR)
    os.makedirs(ETC_BACKUP_DIR, 0o700)
    copy_entry(ETC_INIT_DIR, os.path.join(ETC_BACKUP_DIR, "init.d"))
    for path in ETC_FILES:
        if os.path.exists(path):
            copy_entry(path, os.path.join(ETC_BACKUP_DIR, "files", path.lstrip("/")))


def files_differ(a: str, b: str) -> bool:
    if os.path.exists(a) != os.path.exists(b):
        return True
    return os.path.exists(a) and sha512_of(a) != sha512_of(b)


def restore_etc() -> bool:
    """Put the saved boot files back. Returns True when a reboot is needed (driver changed)."""
    reboot = any(
        files_differ(path, os.path.join(ETC_BACKUP_DIR, "files", path.lstrip("/")))
        for path in ETC_FILES
        if path.endswith(".ko")
    )
    saved = os.path.join(ETC_BACKUP_DIR, "init.d")
    if not os.path.isdir(saved):
        raise UpdateError(f"no saved boot files in {ETC_BACKUP_DIR}")
    for name in os.listdir(ETC_INIT_DIR):
        os.remove(os.path.join(ETC_INIT_DIR, name))
    for name in os.listdir(saved):
        shutil.copy2(os.path.join(saved, name), os.path.join(ETC_INIT_DIR, name), follow_symlinks=False)
    for path in ETC_FILES:
        backup = os.path.join(ETC_BACKUP_DIR, "files", path.lstrip("/"))
        if os.path.exists(backup):
            copy_entry(backup, path + ".restore")
            os.replace(path + ".restore", path)
        elif os.path.exists(path):
            os.remove(path)
    return reboot


def set_pending(version: str) -> None:
    os.makedirs(os.path.dirname(PENDING_FILE), exist_ok=True)
    with open(PENDING_FILE, "w") as f:
        f.write(version + "\n")
    os.sync()


def clear_pending() -> None:
    if os.path.exists(PENDING_FILE):
        os.remove(PENDING_FILE)
        os.sync()


def rollback(discard: str) -> None:
    """Put the saved app and boot files back and start the previous version."""
    if not os.path.isdir(BACKUP_DIR):
        raise UpdateError(f"no previous version in {BACKUP_DIR}")
    service(app_script(), "stop")
    if os.path.exists(APP_DIR):
        os.rename(APP_DIR, discard)
    os.rename(BACKUP_DIR, APP_DIR)
    needs_reboot = restore_etc()
    clear_pending()
    os.sync()
    if needs_reboot:
        # The previous HDMI driver only loads at boot.
        print("rolled back; rebooting to load the previous driver")
        subprocess.run(REBOOT)
        return
    if service(app_script(), "restart") != 0 or not healthy():
        raise UpdateError("rolled back, but the previous version did not come up either")


def prepare(source_dir: str) -> None:
    """Set permissions in the staging copy, before anything is stopped."""
    for root, dirs, files in os.walk(source_dir):
        os.chmod(root, 0o755)
        for name in files:
            os.chmod(os.path.join(root, name), 0o755)


def stop_old() -> None:
    if service(STOP_SCRIPT, "stop") == 0:
        return
    # A partial stop can leave the web server down. Bring the old version back up.
    if not all(running(d) for d in DAEMONS):
        if service(STOP_SCRIPT, "restart") != 0 or not healthy():
            raise UpdateError("could not stop the nanokvm service, and it did not come back up")
    raise UpdateError("could not stop the nanokvm service; nothing was changed")


def install(source_dir: str, version: str, work: str) -> None:
    prepare(source_dir)
    backup_etc()
    stop_old()

    if os.path.exists(BACKUP_DIR):
        shutil.rmtree(BACKUP_DIR)
    os.rename(APP_DIR, BACKUP_DIR)
    try:
        shutil.move(source_dir, APP_DIR)
        # If the first start reboots the device, the next boot checks it (--confirm-boot).
        set_pending(version)
        ok = service(app_script(), "restart") == 0 and healthy()
    except Exception as e:
        print(f"install failed: {e}", file=sys.stderr)
        ok = False
    if ok:
        clear_pending()
        print(f"Previous version kept in {BACKUP_DIR}")
        return
    rollback(os.path.join(work, "failed"))
    raise UpdateError("the new version did not come up; rolled back to the previous version")


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
    # A reboot during an earlier update can leave its work directory behind.
    for name in os.listdir(WORK_PARENT):
        if name.startswith(".kvm-update-"):
            shutil.rmtree(os.path.join(WORK_PARENT, name), ignore_errors=True)
    work = tempfile.mkdtemp(prefix=".kvm-update-", dir=WORK_PARENT)
    try:
        if args.rollback:
            rollback(os.path.join(work, "discarded"))
            return "previous version"
        if args.confirm_boot:
            return confirm_boot(work)
        return run_locked(args, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        lock.close()


def confirm_boot(work: str) -> str:
    """Called at boot by S95nanokvm while an update is pending."""
    if not os.path.exists(PENDING_FILE):
        return "nothing pending"
    if healthy():
        clear_pending()
        return "update confirmed"
    rollback(os.path.join(work, "failed"))
    raise UpdateError("the new version did not come up after reboot; rolled back")


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
    install(source_dir, version, work)
    return version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--file", help="local nanokvm_<version>.tar.gz")
    parser.add_argument("--sha512", help="expected SHA-512 (base64 or hex)")
    parser.add_argument("--version", help="release to download, e.g. 2.5.4.1")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rollback", action="store_true", help="put back the previous version")
    parser.add_argument("--confirm-boot", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    try:
        result = run(args)
    except Exception as e:
        print(f"update failed\n{e}", file=sys.stderr)
        return 1
    if args.rollback or args.confirm_boot:
        print(result)
    elif not args.dry_run:
        print(f"Successfully updated to version: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
