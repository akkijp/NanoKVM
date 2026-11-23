"""Tests for kvmapp/system/update-nanokvm.py. Run: python3 -m unittest discover -s scripts/tests"""
import base64
import hashlib
import http.server
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import tarfile
import tempfile
import threading
import types
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "kvmapp/system/update-nanokvm.py"
OK_SCRIPT = b"#!/bin/sh\necho \"$0 $1\" >> \"$(dirname \"$0\")/../../../calls.log\"\nexit 0\n"
FAIL_SCRIPT = b"#!/bin/sh\nexit 1\n"


def load():
    spec = importlib.util.spec_from_file_location("update_nanokvm", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_script(path: pathlib.Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    path.chmod(0o755)


class UpdateScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.u = load()
        self.u.APP_DIR = str(self.base / "root/kvmapp")
        self.u.BACKUP_DIR = str(self.base / "root/old")
        self.u.WORK_PARENT = str(self.base / "root")
        self.u.LOCK_FILE = str(self.base / "update.lock")
        etc = self.base / "etc"
        self.u.STOP_SCRIPT = str(etc / "init.d/S95nanokvm")
        write_script(pathlib.Path(self.u.STOP_SCRIPT), b"#!/bin/sh\nexit 0\n")
        self.u.STAGE_DIR = str(self.base / "root/kvmapp.new")
        self.u.stopped = lambda: True
        self.u.started = lambda: True
        self.u.migration_reboots = lambda: False
        self.u.time = types.SimpleNamespace(sleep=lambda s: None, monotonic=__import__("time").monotonic)
        app = pathlib.Path(self.u.APP_DIR)
        app.mkdir(parents=True)
        (app / "version").write_text("2.4.3\n")
        write_script(app / "system/init.d/S95nanokvm", OK_SCRIPT)

    def tearDown(self):
        self.tmp.cleanup()

    def entries(self, version, inner_version=None, init=OK_SCRIPT, skip=()):
        root = f"nanokvm_{version}"
        files = {
            "version": f"{inner_version or version}\n".encode(),
            "server/NanoKVM-Server": b"x",
            "kvm_system/kvm_system": b"x",
            "system/init.d/S95nanokvm": init,
            "system/update-nanokvm.py": b"x",
            "kvm_new_app": b"",
        }
        for required in self.u.REQUIRED_FILES:
            files.setdefault(required, b"x")
        return [(root, None)] + [(f"{root}/{k}", v) for k, v in files.items() if k not in skip]

    def package(self, version, entries=None, name=None):
        if entries is None:
            entries = self.entries(version)
        path = self.base / (name or f"nanokvm_{version}.tar.gz")
        with tarfile.open(path, "w:gz") as tar:
            for member_name, data in entries:
                info = tarfile.TarInfo(member_name)
                if data is None:
                    info.type = tarfile.DIRTYPE
                elif data == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = "/etc/passwd"
                    data = None
                else:
                    info.size = len(data)
                    info.mode = 0o755
                tar.addfile(info, io.BytesIO(data) if data is not None else None)
        digest = hashlib.sha512(path.read_bytes()).digest()
        return str(path), base64.b64encode(digest).decode()

    def run_update(self, **kw):
        args = types.SimpleNamespace(file=None, sha512=None, version=None, dry_run=False)
        args.__dict__.update(kw)
        return self.u.run(args)

    def installed_version(self):
        return (pathlib.Path(self.u.APP_DIR) / "version").read_text().strip()

    def assert_unchanged(self):
        self.assertEqual(self.installed_version(), "2.4.3")

    def assert_rejected(self, message, **kw):
        with self.assertRaisesRegex(self.u.UpdateError, message):
            self.run_update(**kw)
        self.assert_unchanged()

    def assert_no_work_dirs(self):
        leftovers = [p for p in os.listdir(self.u.WORK_PARENT) if p.startswith(".kvm-update-")]
        self.assertEqual(leftovers, [])

    # --- local file -----------------------------------------------------------

    def test_installs_four_part_version_and_keeps_backup(self):
        path, digest = self.package("2.5.4.1")
        self.assertEqual(self.run_update(file=path, sha512=digest), "2.5.4.1")
        self.assertEqual(self.installed_version(), "2.5.4.1")
        self.assertEqual((pathlib.Path(self.u.BACKUP_DIR) / "version").read_text().strip(), "2.4.3")
        calls = (self.base / "root/calls.log").read_text()
        self.assertIn("root/kvmapp/system/init.d/S95nanokvm restart", calls)
        self.assert_no_work_dirs()

    def test_accepts_hex_digest(self):
        path, digest = self.package("2.5.4.1")
        self.run_update(file=path, sha512=base64.b64decode(digest).hex())
        self.assertEqual(self.installed_version(), "2.5.4.1")

    def test_dry_run_does_not_install(self):
        path, digest = self.package("2.5.4.1")
        self.run_update(file=path, sha512=digest, dry_run=True)
        self.assert_unchanged()
        self.assert_no_work_dirs()

    def test_installs_the_verified_copy(self):
        path, digest = self.package("2.5.4.1")
        original_copy = self.u.copy_limited

        def copy_then_tamper(source, dest, limit):
            original_copy(source, dest, limit)
            pathlib.Path(source).write_bytes(b"replaced after the copy")

        self.u.copy_limited = copy_then_tamper
        self.run_update(file=path, sha512=digest)
        self.assertEqual(self.installed_version(), "2.5.4.1")

    def test_requires_digest(self):
        path, _ = self.package("2.5.4.1")
        self.assert_rejected("requires --sha512", file=path)

    def test_rejects_wrong_digest(self):
        path, _ = self.package("2.5.4.1")
        self.assert_rejected("does not match", file=path, sha512=base64.b64encode(b"\0" * 64).decode())

    def test_rejects_path_traversal(self):
        root = "nanokvm_2.5.4.1"
        path, digest = self.package("2.5.4.1", [(root, None), (f"{root}/../x", b"x")])
        self.assert_rejected("outside", file=path, sha512=digest)

    def test_rejects_symlink(self):
        root = "nanokvm_2.5.4.1"
        path, digest = self.package("2.5.4.1", [(root, None), (f"{root}/link", "symlink")])
        self.assert_rejected("unsupported", file=path, sha512=digest)

    def test_rejects_version_mismatch(self):
        path, digest = self.package("2.5.4.1", self.entries("2.5.4.1", inner_version="2.5.4"))
        self.assert_rejected("version file", file=path, sha512=digest)

    def test_rejects_missing_migration_marker(self):
        path, digest = self.package("2.5.4.1", self.entries("2.5.4.1", skip=("kvm_new_app",)))
        self.assert_rejected("kvm_new_app", file=path, sha512=digest)

    def test_rejects_missing_updater(self):
        path, digest = self.package("2.5.4.1", self.entries("2.5.4.1", skip=("system/update-nanokvm.py",)))
        self.assert_rejected("update-nanokvm.py", file=path, sha512=digest)

    def test_rejects_bad_file_name(self):
        path, digest = self.package("2.5.4.1", name="other.tar.gz")
        self.assert_rejected("must be named", file=path, sha512=digest)

    # --- service handling -----------------------------------------------------

    def calls(self):
        log = self.base / "root/calls.log"
        return log.read_text() if log.exists() else ""

    def main(self, argv):
        import sys
        saved = sys.argv
        sys.argv = ["update-nanokvm.py"] + argv
        try:
            return self.u.main()
        finally:
            sys.argv = saved

    def test_stop_failure_changes_nothing(self):
        write_script(pathlib.Path(self.u.STOP_SCRIPT), FAIL_SCRIPT)
        self.u.running = lambda name: True
        self.u.stopped = lambda: False
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("nothing was changed", file=path, sha512=digest)
        self.assertFalse(os.path.exists(self.u.STAGE_DIR))

    def test_stop_must_succeed_even_if_daemons_are_gone(self):
        # e.g. another service operation holds the lock and is about to start the app again.
        write_script(pathlib.Path(self.u.STOP_SCRIPT), FAIL_SCRIPT)
        self.u.running = lambda name: True
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("nothing was changed", file=path, sha512=digest)

    def test_stop_that_returns_early_is_waited_for(self):
        # Older init scripts return 0 before the daemons have exited.
        self.u.running = lambda name: True
        self.u.stopped = lambda: False
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("nothing was changed", file=path, sha512=digest)

    def test_partial_stop_brings_old_version_back(self):
        log = self.base / "root/stop.log"
        write_script(pathlib.Path(self.u.STOP_SCRIPT), f"#!/bin/sh\necho $1 >> {log}\n".encode())
        self.u.running = lambda name: name != "NanoKVM-Server"
        self.u.stopped = lambda: False
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("nothing was changed", file=path, sha512=digest)
        self.assertEqual(log.read_text().split(), ["stop", "stop", "stop", "restart"])

    def test_staging_failure_happens_before_stop(self):
        def broken(source_dir):
            raise OSError("chmod failed")
        self.u.prepare = broken
        path, digest = self.package("2.5.4.1")
        with self.assertRaisesRegex(OSError, "chmod failed"):
            self.run_update(file=path, sha512=digest)
        self.assert_unchanged()
        self.assertEqual(self.calls(), "")

    def test_failed_move_puts_old_app_back_and_starts_it(self):
        real_rename = os.rename

        def rename(src, dst):
            if src == self.u.STAGE_DIR:
                raise OSError("no space left")
            real_rename(src, dst)

        self.u.os = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
        self.u.os.rename = rename
        self.u.os.path = os.path
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("previous version was started again", file=path, sha512=digest)
        self.assertIn("restart", self.calls())
        self.assertFalse(os.path.exists(self.u.STAGE_DIR))

    def test_failed_backup_removal_keeps_old_app(self):
        pathlib.Path(self.u.BACKUP_DIR).mkdir()
        real_rmtree = self.u.shutil.rmtree

        def rmtree(path, *a, **kw):
            if path == self.u.BACKUP_DIR:
                raise OSError("read-only")
            real_rmtree(path, *a, **kw)

        self.u.shutil = types.SimpleNamespace(**{k: getattr(shutil, k) for k in dir(shutil) if not k.startswith("__")})
        self.u.shutil.rmtree = rmtree
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("previous version was started again", file=path, sha512=digest)
        self.assertIn("restart", self.calls())

    def test_failed_move_and_failed_restart_is_reported(self):
        real_rename = os.rename

        def rename(src, dst):
            if src == self.u.STAGE_DIR:
                raise OSError("no space left")
            real_rename(src, dst)

        self.u.os = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
        self.u.os.rename = rename
        self.u.os.path = os.path
        self.u.started = lambda: False
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("did not start again either", file=path, sha512=digest)

    def test_interrupted_earlier_switch_is_refused(self):
        app, old = pathlib.Path(self.u.APP_DIR), pathlib.Path(self.u.BACKUP_DIR)
        os.rename(app, old)  # power lost between the two renames of an earlier run
        path, digest = self.package("2.5.4.1")
        with self.assertRaisesRegex(self.u.UpdateError, "interrupted"):
            self.run_update(file=path, sha512=digest)
        self.assertEqual((old / "version").read_text().strip(), "2.4.3")
        self.assertEqual(self.calls(), "")

    def test_driver_reboot_is_not_reported_as_failure(self):
        self.u.migration_reboots = lambda: True
        self.u.started = lambda: False  # the daemons never come up in this boot
        path, digest = self.package("2.5.4.1")
        self.assertEqual(self.run_update(file=path, sha512=digest), "2.5.4.1")
        self.assertIn("restart", self.calls())

    def test_migration_reboot_detection_matches_kvm_system(self):
        import hashlib
        source = (SCRIPT.parents[2] / "support/sg2002/kvm_system/main/lib/system_init/system_init.cpp").read_text()
        self.assertIn(self.u.CURRENT_DRIVER_MD5, source)
        self.assertIn(self.u.LEGACY_DRIVER_MD5, source)
        u = load()
        driver, hdmi = self.base / "driver.ko", self.base / "hdmi_version"
        u.DRIVER, u.HDMI_VERSION = str(driver), str(hdmi)
        driver.write_bytes(b"driver")
        digest = hashlib.md5(b"driver").hexdigest()
        u.CURRENT_DRIVER_MD5 = digest
        self.assertFalse(u.migration_reboots())
        u.CURRENT_DRIVER_MD5, u.LEGACY_DRIVER_MD5 = "0" * 32, digest
        self.assertFalse(u.migration_reboots())  # legacy driver is fine without hdmi_version
        hdmi.write_text("u")
        self.assertTrue(u.migration_reboots())  # but not for HDMI version 2
        driver.unlink()
        self.assertTrue(u.migration_reboots())

    def test_new_version_that_does_not_stay_up_is_reported(self):
        self.u.started = lambda: False
        path, digest = self.package("2.5.4.1")
        with self.assertRaisesRegex(self.u.UpdateError, "did not start.*microSD"):
            self.run_update(file=path, sha512=digest)
        self.assertEqual((pathlib.Path(self.u.BACKUP_DIR) / "version").read_text().strip(), "2.4.3")

    def test_main_returns_failure_exit_code(self):
        path, digest = self.package("2.5.4.1", self.entries("2.5.4.1", init=FAIL_SCRIPT))
        self.assertEqual(self.main(["--file", path, "--sha512", digest]), 1)

    def test_success_leaves_no_staging(self):
        path, digest = self.package("2.5.4.1")
        self.run_update(file=path, sha512=digest)
        self.assertFalse(os.path.exists(self.u.STAGE_DIR))

    def test_concurrent_run_is_refused(self):
        import fcntl
        path, digest = self.package("2.5.4.1")
        with open(self.u.LOCK_FILE, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assert_rejected("already running", file=path, sha512=digest, dry_run=True)

    def test_leftover_work_dirs_are_removed(self):
        stale = pathlib.Path(self.u.WORK_PARENT) / ".kvm-update-stale"
        stale.mkdir()
        path, digest = self.package("2.5.4.1")
        self.run_update(file=path, sha512=digest, dry_run=True)
        self.assertFalse(stale.exists())

    # --- picoclaw init script -------------------------------------------------

    def test_picoclaw_script_refuses_to_start(self):
        script = SCRIPT.parent / "init.d/S96picoclaw"
        env = dict(os.environ, HOME=str(self.base), PICOCLAW_BIN_PATH=str(self.base / "none"),
                   PICOCLAW_HOME=str(self.base / "picoclaw"),
                   PICOCLAW_PID_FILE=str(self.base / "picoclaw/.picoclaw.pid"))
        for action in ("start", "restart", "onboard"):
            result = subprocess.run(["sh", str(script), action], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, action)
            self.assertIn("disabled", result.stderr)

    # --- download -------------------------------------------------------------

    def serve_releases(self, version, package_path, sha512):
        """Serve GitHub-like release URLs that redirect to another path, as GitHub does."""
        manifest = json.dumps({"version": version, "name": f"nanokvm_{version}.tar.gz", "sha512": sha512}).encode()
        package = pathlib.Path(package_path).read_bytes()
        requested = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                requested.append(path)
                redirects = {
                    "/rel/latest/download/latest.json": "/assets/latest.json",
                    f"/rel/download/{version}/latest.json": "/assets/latest.json",
                    f"/rel/download/{version}/nanokvm_{version}.tar.gz": "/assets/package",
                }
                bodies = {"/assets/latest.json": manifest, "/assets/package": package}
                if path in redirects:
                    self.send_response(302)
                    self.send_header("Location", redirects[path])
                    self.end_headers()
                elif path in bodies:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(bodies[path])))
                    self.end_headers()
                    self.wfile.write(bodies[path])
                else:
                    self.send_error(404)

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.u.RELEASES = f"http://127.0.0.1:{server.server_port}/rel"
        return requested

    def test_downloads_latest_release(self):
        path, digest = self.package("2.5.4.1")
        requested = self.serve_releases("2.5.4.1", path, digest)
        self.assertEqual(self.run_update(dry_run=True), "2.5.4.1")
        self.assertIn("/rel/latest/download/latest.json", requested)
        self.assertIn("/rel/download/2.5.4.1/nanokvm_2.5.4.1.tar.gz", requested)

    def test_downloads_pinned_version(self):
        path, digest = self.package("2.5.4.1")
        requested = self.serve_releases("2.5.4.1", path, digest)
        self.assertEqual(self.run_update(version="2.5.4.1", dry_run=True), "2.5.4.1")
        self.assertIn("/rel/download/2.5.4.1/latest.json", requested)

    def test_download_with_wrong_pinned_digest_is_refused(self):
        path, digest = self.package("2.5.4.1")
        self.serve_releases("2.5.4.1", path, digest)
        self.assert_rejected("does not match", sha512=base64.b64encode(b"\1" * 64).decode(), dry_run=True)

    def test_default_source_is_the_fork(self):
        self.assertEqual(load().RELEASES, "https://github.com/akkijp/NanoKVM/releases")
        self.assertNotIn("sipeed", SCRIPT.read_text())


if __name__ == "__main__":
    unittest.main()


S95 = SCRIPT.parent / "init.d/S95nanokvm"


def s95_function(name: str) -> str:
    text = S95.read_text()
    start = text.index(f"{name}() {{")
    return text[start:text.index("\n}\n", start) + 3]


class MicrophoneTest(unittest.TestCase):
    """disable_audio_capture from S95nanokvm against a fake sysfs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        sound = self.base / "sys/class/sound"
        for card, device in (("0", "adc"), ("1", "dac")):
            (sound / f"card{card}").mkdir(parents=True)
            (self.base / f"sys/devices/{device}").mkdir(parents=True)
            os.symlink(self.base / f"sys/devices/{device}", sound / f"card{card}/device")
            (self.base / f"drv{card}").mkdir()
            (self.base / f"drv{card}/unbind").write_text("")
            os.symlink(self.base / f"drv{card}", self.base / f"sys/devices/{device}/driver")
        (sound / "pcmC0D0c").write_text("")
        (sound / "pcmC1D0p").write_text("")
        (self.base / "run").mkdir()
        (self.base / "etc/kvm").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def run_disable(self, unbind_removes=True):
        body = (s95_function("capture_cards") + s95_function("disable_audio_capture"))
        for path in ("/sys/class/sound", "/run/", "/etc/kvm"):
            body = body.replace(path, f"{self.base}{path}")
        script = body + "\ndisable_audio_capture\n"
        if unbind_removes:
            # The kernel drops the card once its driver is unbound.
            script = script.replace(
                'basename "$dev" > "$dev/driver/unbind" 2>/dev/null',
                'basename "$dev" > "$dev/driver/unbind" 2>/dev/null && rm -f "$(dirname "$(dirname "$dev")")/class/sound/pcmC${card}D0c"')
        result = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
        return result, (self.base / "run/nanokvm-microphone").read_text().strip()

    def test_capture_card_is_unbound(self):
        result, status = self.run_disable()
        self.assertEqual((self.base / "drv0/unbind").read_text().strip(), "adc")
        self.assertEqual((self.base / "drv1/unbind").read_text(), "")  # playback card untouched
        self.assertEqual(status, "off")

    def test_failure_is_recorded(self):
        result, status = self.run_disable(unbind_removes=False)
        self.assertTrue(status.startswith("FAILED"), status)
        self.assertIn("could not be disabled", result.stderr)

    def test_enable_file_keeps_it_and_reports_state(self):
        (self.base / "etc/kvm/enable-microphone").write_text("")
        result, status = self.run_disable()
        self.assertEqual((self.base / "drv0/unbind").read_text(), "")
        self.assertEqual(status, "on")

    def test_start_and_restart_both_run_the_boot_steps(self):
        start_services = s95_function("start_services")
        first_lines = start_services.splitlines()[1:6]
        self.assertTrue(any("disable_audio_capture" in line for line in first_lines))
        self.assertTrue(any("disable_picoclaw" in line for line in first_lines))
        text = S95.read_text()
        for action in ("start)", "restart)"):
            branch = text[text.index(f"  {action}"):]
            branch = branch[:branch.index(";;")]
            self.assertIn("start_services", branch, action)
