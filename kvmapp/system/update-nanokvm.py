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
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

RELEASES = "https://github.com/akkijp/NanoKVM/releases"
APP_DIR = "/kvmapp"
BACKUP_DIR = "/root/old"
# Work directories live only under this directory, so cleaning up leftovers never touches
# anything else in /root.
WORK_PARENT = "/root/.kvm-update"
SERVICE_LOG = "/root/.kvm-update/service.log"
LOCK_FILE = "/run/nanokvm-update.lock"
# The installed init script stops the running app. The new app is started with
# its own script, so its first start already runs the fork's boot steps.
STOP_SCRIPT = "/etc/init.d/S95nanokvm"
APP_INIT_SCRIPT = "system/init.d/S95nanokvm"
STAGE_DIR = "/kvmapp.new"
# Created inside the unpacked tree before it becomes STAGE_DIR, so the mark always moves with
# the directory; a /kvmapp.new without it was not made by this script.
STAGE_MARK_NAME = ".staged-by-update-nanokvm"

DAEMONS = ("NanoKVM-Server", "kvm_system")
MIC_STATUS = "/run/nanokvm-microphone"  # written by S95nanokvm
# Written before /kvmapp is switched (by this script and by the web updater) and removed once
# the new version has been seen running (S95nanokvm, or this script). While it exists, /root/old
# is the only version known to work. JSON {"from", "to", "phase"}; phase "switching" while
# /kvmapp is replaced, "installed" once complete. Same format as server/service/application/pending.go.
RESTART_PENDING = "/etc/kvm/update-restart-pending"
STOP_TIMEOUT = 20
START_TIMEOUT = 60
START_STABLE = 15
REBOOT_WAIT = 120

MAX_PACKAGE_SIZE = 1 << 30
MAX_EXPANDED_SIZE = 2 << 30
MAX_ENTRIES = 100_000
MAX_MANIFEST_SIZE = 64 << 10
# Free space left after writing, as the web updater keeps (storage.go reserveBytes).
MIN_FREE_RESERVE = 128 << 20
FREE_RESERVE_PERCENT = 5

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
    # Copied unconditionally by that migration (system_init.cpp new_app_init).
    "system/init.d/S00kmod",
    "system/init.d/S01fs",
    "system/init.d/S03usbdev",
    "system/init.d/S15kvmhwd",
    "system/init.d/S30eth",
    "system/init.d/S30wifi",
    "system/init.d/S50sshd",
    "system/init.d/S98tailscaled",
    "system/ko/soph_mipi_rx.ko",
    # The web UI needs its page.
    "server/web/index.html",
) + tuple(f"server/dl_lib/{lib}" for lib in (
    # Runtime libraries of the server (libkvm.so and what it loads). package.sh ships every one of
    # them; keep in sync with server/dl_lib/ and archive.go (a test checks both).
    "libaaccomm2.so",
    "libaacdec2.so",
    "libaacenc2.so",
    "libaacsbrdec2.so",
    "libaacsbrenc2.so",
    "libae.so",
    "libaf.so",
    "libawb.so",
    "libcli.so",
    "libcvi_RES1.so",
    "libcvi_VoiceEngine.so",
    "libcvi_audio.so",
    "libcvi_bin.so",
    "libcvi_bin_isp.so",
    "libcvi_ispd2.so",
    "libcvi_ive.so",
    "libcvi_ssp.so",
    "libcvi_vqe.so",
    "libdnvqe.so",
    "libini.so",
    "libisp.so",
    "libisp_algo.so",
    "libjson-c.so.5",
    "libkvm.so",
    "libkvm_mmf.so",
    "libmipi_tx.so",
    "libmisc.so",
    "libopencv_core.so.409",
    "libopencv_highgui.so.409",
    "libopencv_imgcodecs.so.409",
    "libopencv_imgproc.so.409",
    "libosdc.so",
    "libraw_dump.so",
    "libsys.so",
    "libtinyalsa.so",
    "libvdec.so",
    "libvenc.so",
    "libvpu.so",
))

# The migration replaces the HDMI driver and reboots unless the installed one is
# current. Keep in sync with new_app_init() in
# support/sg2002/kvm_system/main/lib/system_init/system_init.cpp.
DRIVER = "/mnt/system/ko/soph_mipi_rx.ko"
HDMI_VERSION = "/etc/kvm/hdmi_version"
CURRENT_DRIVER_MD5 = "69be7eeded3777f750480a5dd5a1aa26"
LEGACY_DRIVER_MD5 = "086ed01749188975afaa40fb569374f8"


class UpdateError(Exception):
    pass


def say(message: str, err: bool = False) -> None:
    """Print, but never fail: once the SSH session is gone the terminal raises EIO/EPIPE,
    and an update must not stop half-way because of a message."""
    try:
        print(message, file=sys.stderr if err else sys.stdout, flush=True)
    except OSError:
        pass


class Uninterruptible:
    """From stopping the old version until the restart is requested, Ctrl-C and a dropped SSH
    session (SIGINT, SIGHUP, SIGPIPE, SIGTERM) must not end the process: the KVM would stay down."""

    SIGNALS = (signal.SIGINT, signal.SIGHUP, signal.SIGPIPE, signal.SIGTERM)

    def __enter__(self):
        self.saved = {sig: signal.signal(sig, signal.SIG_IGN) for sig in self.SIGNALS}
        return self

    def __exit__(self, *exc):
        for sig, handler in self.saved.items():
            signal.signal(sig, handler)
        return False


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
    say(f"Downloading {url}")
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
    if manifest.get("manifest_version") != 2:
        raise UpdateError("invalid latest.json: manifest_version must be 2")
    for key, limit in (("size_bytes", MAX_PACKAGE_SIZE), ("unpacked_size_bytes", MAX_EXPANDED_SIZE)):
        value = manifest.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= limit:
            raise UpdateError(f"invalid latest.json: {key}")
    return manifest


def ensure_space(path: str, needed: int) -> None:
    st = os.statvfs(path)
    total, available = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
    reserve = max(MIN_FREE_RESERVE, total * FREE_RESERVE_PERCENT // 100)
    if available < needed + reserve:
        raise UpdateError(
            f"not enough space on {path}: need {(needed + reserve) >> 20} MiB including the reserve, "
            f"{available >> 20} MiB available"
        )


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


def check_members(archive: tarfile.TarFile, root: str) -> tuple:
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
    return members, expanded


def extract(package: str, version: str, work: str, unpacked_size: int = None) -> str:
    root = f"nanokvm_{version}"
    dest = os.path.join(work, "extract")
    os.mkdir(dest)
    with tarfile.open(package, "r:gz") as archive:
        members, expanded = check_members(archive, root)
        if unpacked_size is not None and expanded != unpacked_size:
            raise UpdateError(f"package unpacks to {expanded} bytes, latest.json says {unpacked_size}")
        ensure_space(dest, expanded)
        # The staged copy may land on another filesystem than the work directory.
        ensure_space(os.path.dirname(STAGE_DIR) or "/", expanded)
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


def default_signals() -> None:
    """Ignored signals are inherited across exec. The init scripts and the daemons they start must
    get the defaults back, or e.g. an old kvm_system could no longer be stopped with SIGTERM."""
    for sig in Uninterruptible.SIGNALS:
        signal.signal(sig, signal.SIG_DFL)


def service(script: str, action: str) -> int:
    """Run an init script detached from the terminal, so a closed SSH session cannot make it fail
    (S95nanokvm ends with echo "OK"). A stop is logged to SERVICE_LOG. start/restart launch the
    daemons, which inherit these descriptors, so they get /dev/null as with the web updater
    (restartServices in server/service/application/update.go)."""
    try:
        if action == "stop":
            say(f"{script} {action} (output in {SERVICE_LOG})")
            os.makedirs(os.path.dirname(SERVICE_LOG), 0o700, exist_ok=True)
            with open(SERVICE_LOG, "ab") as log:
                return subprocess.run([script, action], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                      start_new_session=True, preexec_fn=default_signals).returncode
        say(f"{script} {action}")
        return subprocess.run([script, action], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, start_new_session=True,
                              preexec_fn=default_signals).returncode
    except OSError as e:
        say(f"cannot run {script}: {e}", err=True)
        return 127


def app_script() -> str:
    return os.path.join(APP_DIR, APP_INIT_SCRIPT)


def running(name: str) -> bool:
    return subprocess.run(["pidof", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def read_version(app_dir: str) -> str:
    try:
        with open(os.path.join(app_dir, "version")) as f:
            return f.read().strip()
    except OSError:
        return ""


def microphone_status() -> str:
    try:
        with open(MIC_STATUS) as f:
            return f.read().strip()
    except OSError:
        return ""


def restart_previous(script: str) -> str:
    """Start the previous version again after a failed step and describe the outcome.
    Its init script fails when only the microphone could not be disabled, so the daemons are
    checked on their own."""
    status = service(script, "restart")
    if not started():
        return "the previous version did not start again either"
    mic = microphone_status()
    if mic.startswith("FAILED"):
        return f"the previous version is running again, but its microphone could not be disabled ({mic})"
    if status != 0:
        return "the previous version is running again, but its init script reported a failure"
    return "the previous version was started again"


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


def migration_reboots() -> bool:
    """Whether the new version's first start will replace the HDMI driver and reboot.
    Raises when that first start would fail on this device (new_app_init returns
    APP_INIT_FAILED), so nothing is stopped for an update that cannot start."""
    try:
        with open(DRIVER, "rb") as f:
            digest = hashlib.md5(f.read()).hexdigest()
    except OSError as e:
        raise UpdateError(f"the new version's first start would fail: cannot read {DRIVER} ({e})")
    hdmi = -1
    if os.path.exists(HDMI_VERSION):
        try:
            with open(HDMI_VERSION, "rb") as f:
                first = f.read(1)
        except OSError as e:
            raise UpdateError(f"the new version's first start would fail: cannot read {HDMI_VERSION} ({e})")
        if not first:
            raise UpdateError(f"the new version's first start would fail: {HDMI_VERSION} is empty")
        hdmi = {b"c": 1, b"u": 2, b"d": 2}.get(first, -1)
    current = digest == CURRENT_DRIVER_MD5 or (hdmi != 2 and digest == LEGACY_DRIVER_MD5)
    return not current


# Present in every app version; a tree without them is being moved by another update.
APP_CORE_FILES = ("version", "server/NanoKVM-Server", "kvm_system/kvm_system")


def check_not_interrupted() -> None:
    """Refuse to work over an app that another update is moving, or that an earlier switch left
    half-done: /root/old may then be the only copy. (Upstream web updaters do not share our lock,
    so this is checked before stopping and again after.)"""
    record = read_pending()
    if record is not None:
        current = read_version(APP_DIR)
        whole = all(os.path.isfile(os.path.join(APP_DIR, f)) for f in APP_CORE_FILES)
        # Settled when the daemons running are the ones in /kvmapp (not old processes left by a
        # failed restart) and /kvmapp is one of the two versions. In the "switching" phase,
        # also nothing may have been moved to /root/old yet (the web updater moves file by file).
        settled = (whole and current and current in (record.get("from"), record.get("to"))
                   and daemons_run_app(os.stat(RESTART_PENDING).st_mtime)
                   and (record.get("phase") == "installed" or only_dirs(BACKUP_DIR)))
        if settled:
            say(f"{current} is running from {APP_DIR}; clearing the record of the earlier update")
            clear_restart_pending()
        elif record.get("phase") == "installed":
            raise UpdateError(
                f"{record.get('to') or 'a version'} was installed but has not been seen running yet; "
                f"{BACKUP_DIR} is the version known to work. Nothing was changed. Start it first "
                "(/kvmapp/system/init.d/S95nanokvm restart, or retry the restart from the web page), then update"
            )
        else:
            raise UpdateError(
                f"an earlier update stopped while replacing {APP_DIR}; {BACKUP_DIR} is the version known "
                "to work. Nothing was changed. Recover as described in FORK.md"
            )
    missing = [f for f in APP_CORE_FILES if not os.path.isfile(os.path.join(APP_DIR, f))]
    backup_partial = os.path.lexists(BACKUP_DIR) and not only_dirs(BACKUP_DIR) and not all(
        os.path.isfile(os.path.join(BACKUP_DIR, f)) for f in APP_CORE_FILES)
    if missing or backup_partial:
        raise UpdateError(
            f"{APP_DIR} or {BACKUP_DIR} is incomplete (another update is running, or an earlier one "
            f"was interrupted). Nothing was changed; {BACKUP_DIR} and {STAGE_DIR} are kept. "
            "Wait for a running web update to finish, or recover as described in FORK.md"
        )


def stop_old() -> None:
    """Stop the running app: the init script must succeed (it fails while another service
    operation holds its lock, so that is retried) and both daemons must be gone.
    On failure nothing has been changed."""
    for attempt in range(3):
        if attempt:
            time.sleep(5)
        if service(STOP_SCRIPT, "stop") == 0 and stopped():
            return
    # A partial stop can leave the web server down. Bring the old version back up.
    if not all(running(d) for d in DAEMONS):
        outcome = restart_previous(STOP_SCRIPT)
        if outcome.endswith("either"):
            raise UpdateError(
                "could not stop the nanokvm service, and after the partial stop it did not come back "
                "up. Nothing was installed; restart it with /etc/init.d/S95nanokvm restart or reboot"
            )
        raise UpdateError(f"could not stop the nanokvm service; nothing was installed, and {outcome}")
    raise UpdateError("could not stop the nanokvm service; nothing was changed")


def prepare(source_dir: str) -> None:
    """Set permissions in the staged copy, before anything is stopped."""
    for root, dirs, files in os.walk(source_dir):
        os.chmod(root, 0o755)
        for name in files:
            os.chmod(os.path.join(root, name), 0o755)


def remove_stage_mark() -> None:
    """After the switch the mark is inside /kvmapp; it is harmless there, but tidy it up.
    A failure here must not keep the new version from being started."""
    mark = os.path.join(APP_DIR, STAGE_MARK_NAME)
    try:
        if os.path.exists(mark):
            os.remove(mark)
    except OSError as e:
        say(f"warning: could not remove {mark}: {e}", err=True)


def drop_stage() -> None:
    shutil.rmtree(STAGE_DIR, ignore_errors=True)


def only_dirs(path: str) -> bool:
    """True when path is missing or holds nothing but directories (what the web updater leaves
    behind after moving a backup back file by file)."""
    if not os.path.lexists(path):
        return True
    if os.path.islink(path) or not os.path.isdir(path):
        return False
    for _, _, files in os.walk(path):
        if files:
            return False
    return True


def pids(name: str) -> list:
    result = subprocess.run(["pidof", name], capture_output=True, text=True)
    return result.stdout.split() if result.returncode == 0 else []


PROC = "/proc"


def process_start(pid: str) -> float:
    """Start time of a process, in seconds since the epoch (from /proc/<pid>/stat and btime)."""
    with open(f"{PROC}/{pid}/stat") as f:
        # The command name may contain spaces; the fields after it are fixed.
        fields = f.read().rsplit(")", 1)[1].split()
    ticks = int(fields[19])  # field 22 overall: starttime, in clock ticks since boot
    with open(f"{PROC}/stat") as f:
        btime = next(int(line.split()[1]) for line in f if line.startswith("btime "))
    return btime + ticks / os.sysconf("SC_CLK_TCK")


def daemons_run_app(since: float) -> bool:
    """Both daemons run the binaries in /kvmapp (S95nanokvm starts copies in /tmp, so compare
    contents) and were started after `since`: not old processes left behind by a failed restart,
    even when the old and new binaries are identical."""
    for name, rel in (("NanoKVM-Server", "server/NanoKVM-Server"), ("kvm_system", "kvm_system/kvm_system")):
        found = pids(name)
        if not found:
            return False
        try:
            wanted = sha512_of(os.path.join(APP_DIR, rel))
            for pid in found:
                started_at = process_start(pid)
                # Started after the record, and up for at least START_STABLE seconds (the same
                # check S95nanokvm makes before it clears the record).
                if (sha512_of(f"{PROC}/{pid}/exe") != wanted or started_at <= since
                        or time.time() - started_at < START_STABLE):
                    return False
        except (OSError, ValueError, IndexError, StopIteration):
            return False
    return True


def read_pending():
    """The record, or None. An unreadable record counts as phase "switching" (state unknown)."""
    try:
        with open(RESTART_PENDING) as f:
            data = f.read()
    except FileNotFoundError:
        return None
    except OSError:
        return {"phase": "switching"}
    try:
        record = json.loads(data)
    except ValueError:
        return {"phase": "switching"}
    if not isinstance(record, dict) or record.get("phase") != "installed":
        record = {**(record if isinstance(record, dict) else {}), "phase": "switching"}
    return record


def set_restart_pending(phase: str, old: str, new: str) -> None:
    """Record, durably and atomically, that /kvmapp holds (or is about to hold) a version not yet
    seen running: a temporary file is written in full and synced, renamed over the record, and
    the directory is synced. A failure leaves the previous record (or none), never a partial one."""
    directory = os.path.dirname(RESTART_PENDING)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".update-restart-pending.", dir=directory)
    try:
        try:
            os.fchmod(fd, 0o644)
            os.write(fd, (json.dumps({"from": old, "to": new, "phase": phase}) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, RESTART_PENDING)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def clear_restart_pending() -> None:
    """Remove the record; already gone (S95nanokvm cleared it) counts as done."""
    try:
        os.remove(RESTART_PENDING)
    except FileNotFoundError:
        pass


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


MOUNTS = "/proc/mounts"


def mount_points() -> list:
    try:
        with open(MOUNTS) as f:
            # Spaces in mount points are written as \040.
            return [line.split()[1].replace("\\040", " ") for line in f if len(line.split()) > 1]
    except OSError:
        return []


def check_removable(path: str) -> None:
    """Refuse to delete a tree recursively if that would cross a mount or follow a link."""
    if not os.path.lexists(path):
        return
    real = os.path.realpath(path)
    if os.path.islink(path) or os.path.ismount(path) or any(
            m == real or m.startswith(real + "/") for m in mount_points()):
        raise UpdateError(f"{path} is a link or has a mount in it; nothing was changed")


def check_backup_removable() -> None:
    """The switch deletes the previous /root/old (e.g. a backup disk mounted there must survive)."""
    check_removable(BACKUP_DIR)


def check_same_filesystem() -> None:
    """The switch renames /kvmapp to /root/old and /kvmapp.new to /kvmapp; renames do not
    cross filesystems. The web updater refuses this layout too (ensureInstallFilesystem)."""
    app = os.stat(APP_DIR).st_dev
    for path in (os.path.dirname(BACKUP_DIR), os.path.dirname(STAGE_DIR)):
        if os.stat(path).st_dev != app:
            raise UpdateError(f"{path} is not on the same filesystem as {APP_DIR}; nothing was changed")


def install(source_dir: str, version: str) -> None:
    check_not_interrupted()
    check_same_filesystem()
    check_backup_removable()
    reboots = migration_reboots()
    prepare(source_dir)
    if os.path.lexists(STAGE_DIR):
        raise UpdateError(f"{STAGE_DIR} exists; move it away first")
    with open(os.path.join(source_dir, STAGE_MARK_NAME), "w") as f:
        f.write("staged by update-nanokvm.py\n")
    shutil.move(source_dir, STAGE_DIR)
    with Uninterruptible():
        switch_and_start(version, reboots)


def switch_and_start(version: str, reboots: bool) -> None:
    try:
        stop_old()
    except UpdateError:
        drop_stage()
        raise
    try:
        check_not_interrupted()  # a web update may have been moving files when we stopped it
    except UpdateError:
        drop_stage()
        service(STOP_SCRIPT, "restart")
        raise

    previous = read_version(APP_DIR)
    try:
        set_restart_pending("switching", previous, version)
    except OSError as e:
        drop_stage()
        outcome = restart_previous(STOP_SCRIPT)
        raise UpdateError(f"could not record the pending restart ({e}); nothing was installed, and {outcome}")
    try:
        switch()
    except Exception as e:
        drop_stage()
        # The previous version is back in place. Clearing the record and starting it again are
        # independent: a failure of one must not skip the other.
        note = ""
        try:
            clear_restart_pending()
        except OSError as remove_error:
            note = f" (the record {RESTART_PENDING} could not be removed: {remove_error})"
        outcome = restart_previous(app_script())
        if outcome.endswith("either"):
            raise UpdateError(
                f"could not put the new version in place ({e}), and the previous version did not start "
                f"again either{note}. Recover as described in FORK.md"
            )
        raise UpdateError(f"could not put the new version in place; {outcome}{note}: {e}")
    try:
        set_restart_pending("installed", previous, version)  # complete; only the restart is missing
    except OSError as e:
        say(f"warning: could not mark {version} as installed in {RESTART_PENDING}: {e}", err=True)
    remove_stage_mark()
    os.sync()

    if reboots:
        # The first start replaces the HDMI driver and reboots; this process ends with it.
        say(
            f"{version} is installed. Its first start replaces the HDMI driver and reboots the device. "
            "When it is back, check: cat /kvmapp/version; pidof NanoKVM-Server && pidof kvm_system; "
            "cat /run/nanokvm-microphone (should say off)"
        )
        if service(app_script(), "restart") != 0:
            raise UpdateError(
                f"{version} is installed but its first start failed before rebooting. The previous "
                f"version is in {BACKUP_DIR}; recover as described in FORK.md"
            )
        time.sleep(REBOOT_WAIT)  # still alive afterwards: it did not reboot after all
        if not started():
            raise UpdateError(f"{version} is installed but neither rebooted nor started; see FORK.md")
    else:
        status = service(app_script(), "restart")
        up = started()
        mic = microphone_status()
        if up and mic.startswith("FAILED"):
            raise UpdateError(
                f"{version} is installed and running, but the microphone could not be disabled ({mic}). "
                "See FORK.md (microphone)"
            )
        if not up:
            raise UpdateError(
                f"{version} is installed but did not start. The previous version is in {BACKUP_DIR}; "
                "to recover, write the official NanoKVM image to the microSD card (see FORK.md)"
            )
        if status != 0:
            raise UpdateError(
                f"{version} is installed and running, but its init script reported a failure "
                "(for example picoclaw could not be stopped or disabled; see the system log)"
            )
        # Seen running for START_STABLE: S95nanokvm clears the record too; whoever is first wins.
        clear_restart_pending()
    say(f"Previous version kept in {BACKUP_DIR}")


def acquire_lock():
    fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(fd, 0o600)  # an older file may have been created with the umask
    lock = os.fdopen(fd, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise UpdateError("another update is already running")
    return lock


def run(args) -> str:
    if args.file and os.path.realpath(args.file).startswith(os.path.realpath(WORK_PARENT) + os.sep):
        raise UpdateError(f"{args.file} is inside {WORK_PARENT}, which is cleaned up; copy it elsewhere first")
    lock = acquire_lock()
    # An interrupted earlier run can leave its work directory behind.
    os.makedirs(WORK_PARENT, 0o700, exist_ok=True)
    for name in os.listdir(WORK_PARENT):
        if name.startswith("run-"):
            shutil.rmtree(os.path.join(WORK_PARENT, name), ignore_errors=True)
    work = tempfile.mkdtemp(prefix="run-", dir=WORK_PARENT)
    try:
        return run_locked(args, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        lock.close()


def run_locked(args, work: str) -> str:
    if not args.dry_run and os.path.lexists(STAGE_DIR):
        # Left by a run that stopped before the switch. Keep it if the switch itself was cut short,
        # and never touch a /kvmapp.new this script did not create.
        check_not_interrupted()
        check_removable(STAGE_DIR)
        if (not os.path.isfile(os.path.join(STAGE_DIR, STAGE_MARK_NAME))
                or (args.file and os.path.realpath(args.file).startswith(os.path.realpath(STAGE_DIR) + os.sep))):
            raise UpdateError(f"{STAGE_DIR} exists and was not left by this updater; move it away first")
        shutil.rmtree(STAGE_DIR)
    if args.file:
        if not args.sha512:
            raise UpdateError("--file requires --sha512")
        version = version_from_name(args.file)
        if args.version and args.version != version:
            raise UpdateError("--version does not match the file name")
        expected = parse_digest(args.sha512)
        unpacked_size = None  # no manifest for a local file
        package = os.path.join(work, os.path.basename(args.file))
        ensure_space(work, os.path.getsize(args.file))
        copy_limited(args.file, package, MAX_PACKAGE_SIZE)
    else:
        if args.version and not VERSION_RE.match(args.version):
            raise UpdateError("invalid --version")
        manifest = fetch_manifest(args.version, work)
        version = manifest["version"]
        expected = parse_digest(args.sha512 or manifest["sha512"])
        package = os.path.join(work, manifest["name"])
        size = manifest["size_bytes"]
        unpacked_size = manifest["unpacked_size_bytes"]
        ensure_space(work, size)
        with open(package, "xb") as f:
            fetch(f"{RELEASES}/download/{version}/{manifest['name']}", f, size)
        if os.path.getsize(package) != size:
            raise UpdateError(f"downloaded {os.path.getsize(package)} bytes, latest.json says {size}")

    if sha512_of(package) != expected:
        raise UpdateError("sha512 does not match; package not installed")
    say(f"sha512 verified for {version}")

    source_dir = extract(package, version, work, unpacked_size)
    os.remove(package)  # not needed any more; frees its space before anything is stopped
    if args.dry_run:
        say("Dry run: package is valid, nothing installed")
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
        say(f"update failed\n{e}", err=True)
        return 1
    if not args.dry_run:
        say(f"Successfully updated to version: {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
