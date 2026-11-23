"""Tests for kvmapp/system/update-nanokvm.py. Run: python3 -m unittest discover -s scripts/tests"""
import base64
import hashlib
import http.server
import importlib.util
import io
import json
import os
import pathlib
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
        self.u.ETC_INIT_DIR = str(etc / "init.d")
        self.u.STOP_SCRIPT = str(etc / "init.d/S95nanokvm")
        write_script(pathlib.Path(self.u.STOP_SCRIPT), b"#!/bin/sh\nexit 0\n")
        (etc / "kvm").mkdir(parents=True)
        (etc / "kvm/update-nanokvm.py").write_text("old updater")
        (self.base / "ko").mkdir()
        (self.base / "ko/soph_mipi_rx.ko").write_text("old driver")
        self.u.ETC_FILES = (str(etc / "kvm/update-nanokvm.py"), str(self.base / "ko/soph_mipi_rx.ko"))
        self.u.ETC_BACKUP_DIR = str(self.base / "root/old-etc")
        self.u.PENDING_FILE = str(etc / "kvm/update-pending")
        self.u.REBOOT = ["sh", "-c", f"echo reboot >> {self.base}/root/calls.log"]
        self.u.STAGE_DIR = str(self.base / "root/kvmapp.new")
        self.u.HOOK_SCRIPT = str(etc / "init.d/S94nanokvm-update")
        self.u.CONFIRM_SCRIPT = str(etc / "kvm/update-confirm.py")
        self.u.SERVER_CONFIG = str(etc / "kvm/server.yaml")
        self.u.stopped = lambda: True
        self.u.time = types.SimpleNamespace(sleep=lambda s: None, monotonic=__import__("time").monotonic)
        self.health = [True]
        self.u.healthy = lambda: self.health.pop(0) if len(self.health) > 1 else self.health[0]
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
        args = types.SimpleNamespace(file=None, sha512=None, version=None, dry_run=False,
                                     rollback=False, confirm_boot=False)
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

    def etc_updater(self):
        return pathlib.Path(self.u.ETC_FILES[0]).read_text()

    def install_ok(self):
        path, digest = self.package("2.5.4.1")
        self.run_update(file=path, sha512=digest)

    def test_stop_failure_changes_nothing(self):
        write_script(pathlib.Path(self.u.STOP_SCRIPT), FAIL_SCRIPT)
        self.u.running = lambda name: True
        self.u.stopped = lambda: False
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("nothing was changed", file=path, sha512=digest)
        self.assertFalse(os.path.exists(self.u.STAGE_DIR))
        self.assertFalse(os.path.exists(self.u.PENDING_FILE))

    def test_stop_that_returns_early_is_waited_for(self):
        # Older init scripts return 0 before the daemons have exited.
        self.u.stopped = lambda: False
        self.u.running = lambda name: True
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("nothing was changed", file=path, sha512=digest)

    def test_partial_stop_brings_old_version_back(self):
        log = self.base / "root/stop.log"
        write_script(pathlib.Path(self.u.STOP_SCRIPT),
                     f"#!/bin/sh\necho $1 >> {log}\n[ $1 = restart ]\n".encode())
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

    # A new version whose first start rewrites /etc (as the migration does) and then fails.
    BREAKS_ETC = (b"#!/bin/sh\nd=$(dirname \"$0\")/../../../..\n"
                  b"echo new updater > $d/etc/kvm/update-nanokvm.py\necho new > $d/etc/init.d/S95nanokvm\nexit 1\n")

    def test_start_failure_rolls_back_app_and_boot_files(self):
        path, digest = self.package("2.5.4.1", self.entries("2.5.4.1", init=self.BREAKS_ETC))
        self.assert_rejected("rolled back", file=path, sha512=digest)
        self.assertEqual(self.etc_updater(), "old updater")
        self.assertEqual(pathlib.Path(self.u.STOP_SCRIPT).read_bytes(), b"#!/bin/sh\nexit 0\n")
        self.assertIn("restart", self.calls())
        self.assertFalse(os.path.exists(self.u.PENDING_FILE))
        self.assertFalse(os.path.exists(self.u.ETC_BACKUP_DIR))

    def test_unhealthy_start_rolls_back(self):
        self.health = [False, True]
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("did not come up", file=path, sha512=digest)

    def test_main_returns_failure_exit_code(self):
        path, digest = self.package("2.5.4.1", self.entries("2.5.4.1", init=FAIL_SCRIPT))
        self.assertEqual(self.main(["--file", path, "--sha512", digest]), 1)

    def main(self, argv):
        import sys
        saved = sys.argv
        sys.argv = ["update-nanokvm.py"] + argv
        try:
            return self.u.main()
        finally:
            sys.argv = saved

    def test_rollback_does_not_combine_with_other_options(self):
        self.install_ok()
        with self.assertRaises(SystemExit):
            self.main(["--rollback", "--dry-run"])
        self.assertEqual(self.installed_version(), "2.5.4.1")

    def test_success_installs_recovery_hook_and_saves_boot_files(self):
        self.install_ok()
        self.assertFalse(os.path.exists(self.u.PENDING_FILE))
        self.assertFalse(os.path.exists(self.u.STAGE_DIR))
        saved = pathlib.Path(self.u.ETC_BACKUP_DIR)
        self.assertTrue((saved / "init.d/S95nanokvm").exists())
        self.assertEqual((saved / "app-version").read_text().strip(), "2.4.3")
        self.assertTrue(os.access(self.u.HOOK_SCRIPT, os.X_OK))
        self.assertEqual(pathlib.Path(self.u.CONFIRM_SCRIPT).read_bytes(), SCRIPT.read_bytes())

    def test_manual_rollback(self):
        self.install_ok()
        pathlib.Path(self.u.ETC_FILES[0]).write_text("new updater")
        self.run_update(rollback=True)
        self.assert_unchanged()
        self.assertEqual(self.etc_updater(), "old updater")

    def test_rollback_keeps_services_added_after_the_update(self):
        self.install_ok()
        added = pathlib.Path(self.u.ETC_INIT_DIR) / "S98tailscaled"
        added.write_text("tailscale enabled later")
        self.run_update(rollback=True)
        self.assertEqual(added.read_text(), "tailscale enabled later")

    def test_rollback_refuses_backups_from_different_installs(self):
        self.install_ok()
        # The web updater then installs 2.5.4.2 and replaces /root/old with 2.5.4.1.
        (pathlib.Path(self.u.BACKUP_DIR) / "version").write_text("2.5.4.1\n")
        (pathlib.Path(self.u.APP_DIR) / "version").write_text("2.5.4.2\n")
        with self.assertRaisesRegex(self.u.UpdateError, "not from the same install"):
            self.run_update(rollback=True)
        self.assertEqual(self.installed_version(), "2.5.4.2")
        self.assertEqual(self.etc_updater(), "old updater")

    def test_rollback_does_nothing_if_stop_fails(self):
        self.install_ok()
        self.u.stopped = lambda: False
        with self.assertRaisesRegex(self.u.UpdateError, "nothing was changed"):
            self.run_update(rollback=True)
        self.assertEqual(self.installed_version(), "2.5.4.1")
        self.assertTrue(os.path.isdir(self.u.BACKUP_DIR))

    def test_rollback_reboots_when_driver_changed(self):
        self.install_ok()
        pathlib.Path(self.u.ETC_FILES[1]).write_text("new driver")
        self.run_update(rollback=True)
        self.assertEqual(pathlib.Path(self.u.ETC_FILES[1]).read_text(), "old driver")
        self.assertIn("reboot", self.calls())

    def test_confirm_boot_keeps_healthy_update(self):
        self.install_ok()
        self.u.set_pending("2.5.4.1")
        self.assertEqual(self.run_update(confirm_boot=True), "update confirmed")
        self.assertEqual(self.installed_version(), "2.5.4.1")
        self.assertFalse(os.path.exists(self.u.PENDING_FILE))

    def test_confirm_boot_rolls_back_unhealthy_update(self):
        self.install_ok()
        self.u.set_pending("2.5.4.1")
        self.health = [False, True]
        with self.assertRaisesRegex(self.u.UpdateError, "after reboot"):
            self.run_update(confirm_boot=True)
        self.assert_unchanged()
        self.assertFalse(os.path.exists(self.u.PENDING_FILE))

    def test_confirm_boot_after_incomplete_switch(self):
        # Power was lost between the renames; the hook put /root/old back.
        self.u.set_pending("2.5.4.1")
        self.assertIn("did not complete", self.run_update(confirm_boot=True))
        self.assert_unchanged()
        self.assertFalse(os.path.exists(self.u.PENDING_FILE))

    def test_confirm_boot_without_pending_does_nothing(self):
        self.assertEqual(self.run_update(confirm_boot=True), "nothing pending")
        self.assert_unchanged()

    def run_hook(self):
        root = self.base
        hook = self.u.HOOK.replace("/kvmapp", f"{root}/root/kvmapp").replace("/root/old", f"{root}/root/old") \
            .replace("/etc/kvm", f"{root}/etc/kvm").replace("python3 ", "echo confirm >> " + f"{root}/root/calls.log; true ")
        return subprocess.run(["sh", "-c", hook, "hook", "start"], capture_output=True, text=True)

    def test_boot_hook_restores_app_after_interrupted_switch(self):
        app, old = pathlib.Path(self.u.APP_DIR), pathlib.Path(self.u.BACKUP_DIR)
        os.rename(app, old)  # power lost right after the first rename
        self.run_hook()
        self.assert_unchanged()
        self.assertFalse(old.exists())

    def test_boot_hook_starts_confirmation_when_pending(self):
        self.u.set_pending("2.5.4.1")
        pathlib.Path(self.u.CONFIRM_SCRIPT).write_text("")
        self.run_hook()
        self.assertIn("confirm", self.calls())

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

    # --- health check ---------------------------------------------------------

    def test_web_ports_follow_server_config(self):
        pathlib.Path(self.u.SERVER_CONFIG).write_text("proto: http\nport:\n  http: 8080\n  https: 8443\nlogger:\n  level: info\n")
        self.assertEqual(self.u.web_ports(), {"http": 8080, "https": 8443})

    def test_web_answers_on_configured_port(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_port
        pathlib.Path(self.u.SERVER_CONFIG).write_text(f"port:\n  http: {port}\n  https: 1\n")
        self.assertTrue(self.u.web_answers())
        pathlib.Path(self.u.SERVER_CONFIG).write_text("port:\n  http: 1\n  https: 1\n")
        self.assertFalse(self.u.web_answers())

    # --- picoclaw init script -------------------------------------------------

    def test_picoclaw_script_refuses_to_start(self):
        script = SCRIPT.parent / "init.d/S96picoclaw"
        env = dict(os.environ, HOME=str(self.base), PICOCLAW_BIN_PATH=str(self.base / "none"))
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
