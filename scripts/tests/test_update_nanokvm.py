"""Tests for kvmapp/system/update-nanokvm.py. Run: python3 -m unittest discover -s scripts/tests"""
import base64
import hashlib
import http.server
import importlib.util
import io
import json
import os
import pathlib
import sys
import shutil
import subprocess
import tarfile
import tempfile
import threading
import types
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "kvmapp/system/update-nanokvm.py"
# Loading the updater must not write kvmapp/system/__pycache__ (package.sh ships kvmapp/).
sys.dont_write_bytecode = True
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
        self.u.WORK_PARENT = str(self.base / "root/.kvm-update")
        self.u.LOCK_FILE = str(self.base / "update.lock")
        etc = self.base / "etc"
        self.u.STOP_SCRIPT = str(etc / "init.d/S95nanokvm")
        write_script(pathlib.Path(self.u.STOP_SCRIPT),
                     f"#!/bin/sh\necho \"etc-S95 $1\" >> {self.base}/root/calls.log\nexit 0\n".encode())
        self.u.STAGE_DIR = str(self.base / "root/kvmapp.new")
        self.u.SERVICE_LOG = str(self.base / "root/.kvm-update/service.log")
        self.u.MIC_STATUS = str(self.base / "mic-status")
        self.u.RESTART_PENDING = str(self.base / "restart-pending")
        self.u.MOUNTS = str(self.base / "mounts")
        pathlib.Path(self.u.MOUNTS).write_text("")
        self.u.stopped = lambda: True
        self.u.started = lambda: True
        self.u.running = lambda name: False
        self.real_migration_reboots = self.u.migration_reboots
        self.u.migration_reboots = lambda: False
        self.u.check_same_filesystem = lambda: None
        self.u.time = types.SimpleNamespace(sleep=lambda s: None, monotonic=__import__("time").monotonic)
        app = pathlib.Path(self.u.APP_DIR)
        app.mkdir(parents=True)
        (app / "version").write_text("2.4.3\n")
        for core in ("server/NanoKVM-Server", "kvm_system/kvm_system"):
            (app / core).parent.mkdir(parents=True, exist_ok=True)
            (app / core).write_text("old")
        write_script(app / "system/init.d/S95nanokvm", OK_SCRIPT)

    def complete_app(self, path, version="2.4.2"):
        path = pathlib.Path(path)
        path.mkdir(parents=True, exist_ok=True)
        (path / "version").write_text(version + "\n")
        for core in ("server/NanoKVM-Server", "kvm_system/kvm_system"):
            (path / core).parent.mkdir(parents=True, exist_ok=True)
            (path / core).write_text("x")

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
        path.parent.mkdir(parents=True, exist_ok=True)
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
        self.assertEqual([n for n in os.listdir(self.u.WORK_PARENT) if n.startswith("run-")], [])

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
        self.assert_rejected("previous version was started again", file=path, sha512=digest)
        self.assertEqual(log.read_text().split(), ["stop", "stop", "stop", "restart"])

    def test_partial_stop_that_does_not_recover_is_reported(self):
        self.u.running = lambda name: name != "NanoKVM-Server"
        self.u.stopped = lambda: False
        self.u.started = lambda: False
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("did not come back up", file=path, sha512=digest)

    def test_partial_stop_whose_restart_script_fails_is_reported(self):
        write_script(pathlib.Path(self.u.STOP_SCRIPT), FAIL_SCRIPT)
        self.u.running = lambda name: name != "NanoKVM-Server"
        self.u.stopped = lambda: False
        path, digest = self.package("2.5.4.1")
        # The daemons are up again, but the init script failed: both facts are reported.
        self.assert_rejected("running again, but its init script reported a failure", file=path, sha512=digest)
        self.u.started = lambda: False
        self.assert_rejected("did not come back up", file=path, sha512=digest)

    def test_stale_stage_is_removed_before_unpacking(self):
        stage = pathlib.Path(self.u.STAGE_DIR)
        stage.mkdir()
        (stage / "leftover").write_text("x")
        (stage / self.u.STAGE_MARK_NAME).write_text("")
        path, digest = self.package("2.5.4.1")
        self.run_update(file=path, sha512=digest)
        self.assertEqual(self.installed_version(), "2.5.4.1")
        self.assertFalse(stage.exists())
        self.assertFalse((pathlib.Path(self.u.APP_DIR) / self.u.STAGE_MARK_NAME).exists())

    def test_mark_left_in_kvmapp_does_not_vouch_for_a_new_stage(self):
        # Power was lost after the switch, before the mark was tidied up; then an admin made /kvmapp.new.
        (pathlib.Path(self.u.APP_DIR) / self.u.STAGE_MARK_NAME).write_text("")
        stage = pathlib.Path(self.u.STAGE_DIR)
        stage.mkdir()
        (stage / "admin-file").write_text("x")
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("not left by this updater", file=path, sha512=digest)
        self.assertTrue((stage / "admin-file").exists())

    def test_foreign_stage_is_not_removed(self):
        stage = pathlib.Path(self.u.STAGE_DIR)
        stage.mkdir()
        (stage / "keep").write_text("x")  # not made by the updater: no mark
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("not left by this updater", file=path, sha512=digest)
        self.assertTrue((stage / "keep").exists())

    def test_input_inside_stage_is_not_removed(self):
        stage = pathlib.Path(self.u.STAGE_DIR)
        stage.mkdir()
        (stage / self.u.STAGE_MARK_NAME).write_text("")
        path, digest = self.package("2.5.4.1", name="root/kvmapp.new/nanokvm_2.5.4.1.tar.gz")
        self.assert_rejected("not left by this updater", file=path, sha512=digest)
        self.assertTrue(pathlib.Path(path).exists())

    def test_daemons_do_not_inherit_the_log(self):
        # The new version's restart starts the daemons; whatever they print must not land in the log.
        path, digest = self.package("2.5.4.1", self.entries(
            "2.5.4.1", init=b"#!/bin/sh\necho daemon-output\nexit 0\n"))
        self.run_update(file=path, sha512=digest)
        log = pathlib.Path(self.u.SERVICE_LOG)
        self.assertNotIn("daemon-output", log.read_text() if log.exists() else "")

    def test_init_script_output_goes_to_the_log_not_the_terminal(self):
        write_script(pathlib.Path(self.u.STOP_SCRIPT), b"#!/bin/sh\necho stopping-OK\n")
        path, digest = self.package("2.5.4.1")
        saved = os.dup(1)
        os.close(1)  # the SSH session is gone
        try:
            self.run_update(file=path, sha512=digest)
        finally:
            os.dup2(saved, 1)
            os.close(saved)
        self.assertEqual(self.installed_version(), "2.5.4.1")
        self.assertIn("stopping-OK", pathlib.Path(self.u.SERVICE_LOG).read_text())

    def test_stale_stage_is_kept_after_interrupted_switch(self):
        stage, app, old = pathlib.Path(self.u.STAGE_DIR), pathlib.Path(self.u.APP_DIR), pathlib.Path(self.u.BACKUP_DIR)
        stage.mkdir()
        os.rename(app, old)
        path, digest = self.package("2.5.4.1")
        with self.assertRaisesRegex(self.u.UpdateError, "interrupted"):
            self.run_update(file=path, sha512=digest)
        self.assertTrue(stage.exists())

    def test_ctrl_c_or_hangup_during_stop_does_not_leave_kvm_down(self):
        for sig in ("INT", "HUP"):
            with self.subTest(signal=sig):
                self.tearDown()
                self.setUp()
                # The stop script signals the updater, as Ctrl-C or a dropped SSH session would.
                write_script(pathlib.Path(self.u.STOP_SCRIPT), f"#!/bin/sh\nkill -{sig} {os.getpid()}\nexit 0\n".encode())
                path, digest = self.package("2.5.4.1")
                self.run_update(file=path, sha512=digest)
                self.assertEqual(self.installed_version(), "2.5.4.1")
                self.assertIn("restart", self.calls())

    def test_signal_handlers_are_restored(self):
        import signal
        before = signal.getsignal(signal.SIGINT)
        path, digest = self.package("2.5.4.1")
        self.run_update(file=path, sha512=digest)
        self.assertIs(signal.getsignal(signal.SIGINT), before)

    def test_lock_file_is_private(self):
        pathlib.Path(self.u.LOCK_FILE).write_text("")
        os.chmod(self.u.LOCK_FILE, 0o644)
        path, digest = self.package("2.5.4.1")
        self.run_update(file=path, sha512=digest, dry_run=True)
        self.assertEqual(os.stat(self.u.LOCK_FILE).st_mode & 0o777, 0o600)

    def test_staging_failure_happens_before_stop(self):
        def broken(source_dir):
            raise OSError("chmod failed")
        self.u.prepare = broken
        path, digest = self.package("2.5.4.1")
        with self.assertRaisesRegex(OSError, "chmod failed"):
            self.run_update(file=path, sha512=digest)
        self.assert_unchanged()
        self.assertEqual(self.calls(), "")  # not even a stop

    def test_backup_with_a_mount_inside_is_not_deleted(self):
        old = pathlib.Path(self.u.BACKUP_DIR)
        self.complete_app(old)
        (old / "disk").mkdir()
        (old / "disk/important").write_text("x")
        pathlib.Path(self.u.MOUNTS).write_text(f"/dev/sda1 {os.path.realpath(old)}/disk ext4 rw 0 0\n")
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("has a mount in it", file=path, sha512=digest)
        self.assertTrue((old / "disk/important").exists())
        self.assertEqual(self.calls(), "")

    def test_backup_that_is_a_link_is_not_followed(self):
        target = self.base / "elsewhere"
        self.complete_app(target)
        os.symlink(target, self.u.BACKUP_DIR)
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("is a link", file=path, sha512=digest)
        self.assertTrue((target / "version").exists())

    def test_failing_to_remove_the_stage_mark_still_starts_the_new_version(self):
        real_remove = os.remove

        def remove(path, *a, **kw):
            if path.endswith(self.u.STAGE_MARK_NAME):
                raise OSError("read-only")
            real_remove(path, *a, **kw)

        self.u.os = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
        self.u.os.remove = remove
        self.u.os.path = os.path
        path, digest = self.package("2.5.4.1")
        self.run_update(file=path, sha512=digest)
        self.assertTrue(self.calls().splitlines()[-1].endswith("S95nanokvm restart"))

    def test_previous_version_back_with_microphone_on_is_described(self):
        real_rename = os.rename

        def rename(src, dst):
            if src == self.u.STAGE_DIR:
                raise OSError("no space left")
            real_rename(src, dst)

        self.u.os = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
        self.u.os.rename = rename
        self.u.os.path = os.path
        # The previous (fork) version runs again, but its microphone could not be disabled.
        write_script(pathlib.Path(self.u.APP_DIR) / "system/init.d/S95nanokvm",
                     f"#!/bin/sh\necho 'FAILED: capture still present on card 0' > {self.base}/mic-status\nexit 1\n".encode())
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("running again, but its microphone could not be disabled", file=path, sha512=digest)

    def test_web_update_waiting_for_restart_blocks_install(self):
        self.complete_app(self.u.BACKUP_DIR)  # the version known to work
        pathlib.Path(self.u.RESTART_PENDING).write_text('{"from": "2.4.2", "to": "2.4.3", "phase": "installed"}')
        path, digest = self.package("2.5.4.1")
        with self.assertRaisesRegex(self.u.UpdateError, "not been seen running"):
            self.run_update(file=path, sha512=digest)
        self.assertEqual((pathlib.Path(self.u.BACKUP_DIR) / "version").read_text().strip(), "2.4.2")
        self.assertEqual(self.calls(), "")

    def test_failed_start_keeps_the_record_and_blocks_a_second_run(self):
        self.u.started = lambda: False
        path, digest = self.package("2.5.4.1")
        with self.assertRaisesRegex(self.u.UpdateError, "did not start"):
            self.run_update(file=path, sha512=digest)
        self.assertTrue(os.path.exists(self.u.RESTART_PENDING))
        # Running the same command again must keep /root/old (the version that worked).
        with self.assertRaisesRegex(self.u.UpdateError, "not been seen running"):
            self.run_update(file=path, sha512=digest)
        self.assertEqual((pathlib.Path(self.u.BACKUP_DIR) / "version").read_text().strip(), "2.4.3")

    def test_record_written_half_way_asks_for_manual_recovery(self):
        pathlib.Path(self.u.RESTART_PENDING).write_text('{"from": "2.4.2", "to": "2.5.4.1", "phase": "switching"}')
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("stopped while replacing", file=path, sha512=digest)
        pathlib.Path(self.u.RESTART_PENDING).write_text("not json")  # unknown state counts as half-way
        self.assert_rejected("stopped while replacing", file=path, sha512=digest)

    def test_record_is_cleared_when_the_previous_version_runs_again(self):
        # Power was lost after the record was written; the upstream init script, which does not
        # know the record, started the previous version again.
        pathlib.Path(self.u.RESTART_PENDING).write_text('{"from": "2.4.3", "to": "2.5.4.1", "phase": "switching"}')
        self.u.running = lambda name: True
        path, digest = self.package("2.5.4.1")
        self.run_update(file=path, sha512=digest)
        self.assertEqual(self.installed_version(), "2.5.4.1")

    def test_record_is_not_cleared_while_the_daemons_are_down(self):
        pathlib.Path(self.u.RESTART_PENDING).write_text('{"from": "2.4.3", "to": "2.5.4.1", "phase": "installed"}')
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("not been seen running", file=path, sha512=digest)
        self.assertTrue(os.path.exists(self.u.RESTART_PENDING))

    def test_record_phases_around_the_switch(self):
        phases = []
        real_switch = self.u.switch

        def switch():
            phases.append(json.loads(pathlib.Path(self.u.RESTART_PENDING).read_text()))
            real_switch()

        self.u.switch = switch
        self.u.started = lambda: False
        path, digest = self.package("2.5.4.1")
        with self.assertRaises(self.u.UpdateError):
            self.run_update(file=path, sha512=digest)
        self.assertEqual(phases, [{"from": "2.4.3", "to": "2.5.4.1", "phase": "switching"}])
        after = json.loads(pathlib.Path(self.u.RESTART_PENDING).read_text())
        self.assertEqual(after["phase"], "installed")

    def test_failed_switch_restarts_even_if_the_record_cannot_be_removed(self):
        real_rename, real_remove = os.rename, os.remove

        def rename(src, dst):
            if src == self.u.STAGE_DIR:
                raise OSError("no space left")
            real_rename(src, dst)

        def remove(path, *a, **kw):
            if path == self.u.RESTART_PENDING:
                raise OSError("read-only file system")
            real_remove(path, *a, **kw)

        self.u.os = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
        self.u.os.rename, self.u.os.remove, self.u.os.path = rename, remove, os.path
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("could not be removed", file=path, sha512=digest)
        self.assertTrue(self.calls().splitlines()[-1].endswith("S95nanokvm restart"))

    def test_success_clears_the_record(self):
        path, digest = self.package("2.5.4.1")
        self.run_update(file=path, sha512=digest)
        self.assertFalse(os.path.exists(self.u.RESTART_PENDING))

    def test_record_is_written_before_the_switch(self):
        seen = []
        real_switch = self.u.switch
        self.u.switch = lambda: (seen.append(os.path.exists(self.u.RESTART_PENDING)), real_switch())[1]
        path, digest = self.package("2.5.4.1")
        self.run_update(file=path, sha512=digest)
        self.assertEqual(seen, [True])

    def test_running_kvm_with_failed_init_script_is_not_called_a_failed_start(self):
        path, digest = self.package("2.5.4.1", self.entries("2.5.4.1", init=FAIL_SCRIPT))
        with self.assertRaisesRegex(self.u.UpdateError, "installed and running, but its init script"):
            self.run_update(file=path, sha512=digest)

    def test_microphone_left_on_is_reported(self):
        # The new S95 records that the microphone could not be disabled, but the KVM runs.
        status = self.base / "mic-status"
        path, digest = self.package("2.5.4.1", self.entries(
            "2.5.4.1", init=f"#!/bin/sh\necho 'FAILED: capture still present on card 0' > {status}\nexit 1\n".encode()))
        with self.assertRaisesRegex(self.u.UpdateError, "microphone could not be disabled"):
            self.run_update(file=path, sha512=digest)
        self.assertEqual(self.installed_version(), "2.5.4.1")

    def test_input_inside_work_directory_is_refused(self):
        work = pathlib.Path(self.u.WORK_PARENT) / "run-earlier"
        work.mkdir(parents=True)
        path, digest = self.package("2.5.4.1", name="root/.kvm-update/run-earlier/nanokvm_2.5.4.1.tar.gz")
        self.assert_rejected("copy it elsewhere", file=path, sha512=digest, dry_run=True)
        self.assertTrue(pathlib.Path(path).exists())

    def test_stop_comes_before_restart(self):
        path, digest = self.package("2.5.4.1")
        self.run_update(file=path, sha512=digest)
        lines = self.calls().splitlines()
        self.assertEqual(lines[0], "etc-S95 stop")
        self.assertTrue(lines[-1].endswith("S95nanokvm restart"), lines)

    def test_app_being_moved_by_web_update_is_refused_before_stop(self):
        (pathlib.Path(self.u.APP_DIR) / "server/NanoKVM-Server").unlink()  # half-moved to /root/old
        path, digest = self.package("2.5.4.1")
        with self.assertRaisesRegex(self.u.UpdateError, "another update is running"):
            self.run_update(file=path, sha512=digest)
        self.assertEqual(self.calls(), "")

    def test_backup_with_only_its_version_file_left_is_kept(self):
        # An earlier rmtree of /root/old was cut short after the binaries, before the version file.
        old = pathlib.Path(self.u.BACKUP_DIR)
        old.mkdir()
        (old / "version").write_text("2.4.2\n")
        (old / "kvm_system").mkdir()
        (old / "kvm_system/kvm_system").write_text("x")
        path, digest = self.package("2.5.4.1")
        with self.assertRaisesRegex(self.u.UpdateError, "another update is running"):
            self.run_update(file=path, sha512=digest)
        self.assertTrue((old / "kvm_system/kvm_system").exists())
        self.assertEqual(self.calls(), "")

    def test_stage_with_a_mount_inside_is_not_deleted(self):
        stage = pathlib.Path(self.u.STAGE_DIR)
        (stage / "disk").mkdir(parents=True)
        (stage / self.u.STAGE_MARK_NAME).write_text("")
        (stage / "disk/evidence").write_text("x")
        pathlib.Path(self.u.MOUNTS).write_text(f"/dev/sdb1 {os.path.realpath(stage)}/disk ext4 rw 0 0\n")
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("has a mount in it", file=path, sha512=digest)
        self.assertTrue((stage / "disk/evidence").exists())

    def test_half_moved_backup_is_kept(self):
        (pathlib.Path(self.u.BACKUP_DIR) / "server").mkdir(parents=True)  # no version: still being filled
        path, digest = self.package("2.5.4.1")
        with self.assertRaisesRegex(self.u.UpdateError, "another update is running"):
            self.run_update(file=path, sha512=digest)
        self.assertTrue((pathlib.Path(self.u.BACKUP_DIR) / "server").exists())

    def test_web_update_found_after_stop_aborts_and_restarts(self):
        # The web update was moving files when the stop ran.
        app = pathlib.Path(self.u.APP_DIR)
        write_script(pathlib.Path(self.u.STOP_SCRIPT),
                     f"#!/bin/sh\necho \"etc-S95 $1\" >> {self.base}/root/calls.log\n"
                     f"[ $1 = stop ] && rm -f {app}/kvm_system/kvm_system\nexit 0\n".encode())
        path, digest = self.package("2.5.4.1")
        with self.assertRaisesRegex(self.u.UpdateError, "another update is running"):
            self.run_update(file=path, sha512=digest)
        self.assertEqual(self.calls().split(), ["etc-S95", "stop", "etc-S95", "restart"])
        self.assertFalse(os.path.exists(self.u.STAGE_DIR))

    def test_init_scripts_get_default_signal_handling(self):
        # The updater ignores SIGTERM; the daemons it starts must not inherit that.
        marker = self.base / "survived"
        path, digest = self.package("2.5.4.1", self.entries(
            "2.5.4.1", init=f"#!/bin/sh\nkill -TERM $$\necho yes > {marker}\n".encode()))
        with self.assertRaises(self.u.UpdateError):
            self.run_update(file=path, sha512=digest)  # the restart is killed by SIGTERM, so it fails
        self.assertFalse(marker.exists(), "the init script ran with SIGTERM ignored")

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
        self.complete_app(self.u.BACKUP_DIR)  # a complete earlier backup
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
        class Rebooted(Exception):
            pass

        def killed_by_reboot(seconds):
            raise Rebooted()

        self.u.migration_reboots = lambda: True
        self.u.started = lambda: False  # the daemons never come up in this boot
        self.u.time = types.SimpleNamespace(sleep=killed_by_reboot, monotonic=__import__("time").monotonic)
        path, digest = self.package("2.5.4.1")
        with self.assertRaises(Rebooted):
            self.run_update(file=path, sha512=digest)
        self.assertIn("restart", self.calls())
        self.assertEqual(self.installed_version(), "2.5.4.1")

    def test_driver_reboot_that_fails_first_is_reported(self):
        self.u.migration_reboots = lambda: True
        path, digest = self.package("2.5.4.1", self.entries("2.5.4.1", init=FAIL_SCRIPT))
        with self.assertRaisesRegex(self.u.UpdateError, "failed before rebooting"):
            self.run_update(file=path, sha512=digest)

    def test_driver_reboot_that_never_happens_is_reported(self):
        self.u.migration_reboots = lambda: True
        self.u.started = lambda: False
        path, digest = self.package("2.5.4.1")
        with self.assertRaisesRegex(self.u.UpdateError, "neither rebooted nor started"):
            self.run_update(file=path, sha512=digest)

    def test_not_enough_space_is_refused_before_stop(self):
        self.u.MIN_FREE_RESERVE = 1 << 62
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("not enough space", file=path, sha512=digest)
        self.assertEqual(self.calls(), "")

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
        hdmi.write_text("")
        with self.assertRaisesRegex(u.UpdateError, "is empty"):
            u.migration_reboots()  # new_app_init fails on an empty hdmi_version
        hdmi.unlink()
        driver.unlink()
        with self.assertRaisesRegex(u.UpdateError, "cannot read"):
            u.migration_reboots()  # new_app_init fails when md5sum of the driver fails

    def test_missing_driver_is_refused_before_stop(self):
        self.u.migration_reboots = self.real_migration_reboots
        self.u.DRIVER = str(self.base / "missing.ko")
        self.u.HDMI_VERSION = str(self.base / "no-hdmi-version")
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("first start would fail", file=path, sha512=digest)
        self.assertEqual(self.calls(), "")

    def test_new_version_that_does_not_stay_up_is_reported(self):
        self.u.started = lambda: False
        path, digest = self.package("2.5.4.1")
        with self.assertRaisesRegex(self.u.UpdateError, "did not start.*microSD"):
            self.run_update(file=path, sha512=digest)
        self.assertEqual((pathlib.Path(self.u.BACKUP_DIR) / "version").read_text().strip(), "2.4.3")

    def test_main_returns_failure_exit_code(self):
        self.u.started = lambda: False
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
        stale = pathlib.Path(self.u.WORK_PARENT) / "run-stale"
        stale.mkdir(parents=True)
        path, digest = self.package("2.5.4.1")
        self.run_update(file=path, sha512=digest, dry_run=True)
        self.assertFalse(stale.exists())

    def test_cleanup_leaves_other_files_in_root_alone(self):
        kept = pathlib.Path(self.u.WORK_PARENT).parent / ".kvm-update-backup"
        kept.mkdir()
        path, digest = self.package("2.5.4.1", name=".kvm-update-backup/nanokvm_2.5.4.1.tar.gz")
        self.run_update(file=path, sha512=digest, dry_run=True)
        self.assertTrue(pathlib.Path(path).exists())

    def test_backup_on_other_filesystem_is_refused_before_stop(self):
        u = load()
        u.APP_DIR, u.BACKUP_DIR, u.STAGE_DIR = self.u.APP_DIR, "/dev/shm/old", self.u.STAGE_DIR
        if not os.path.isdir("/dev/shm") or os.stat("/dev/shm").st_dev == os.stat(self.u.APP_DIR).st_dev:
            self.skipTest("needs /dev/shm on its own filesystem")
        with self.assertRaisesRegex(u.UpdateError, "same filesystem"):
            u.check_same_filesystem()

    def test_same_filesystem_passes(self):
        u = load()
        u.APP_DIR, u.BACKUP_DIR, u.STAGE_DIR = self.u.APP_DIR, self.u.BACKUP_DIR, self.u.STAGE_DIR
        u.check_same_filesystem()

    # --- release check ----------------------------------------------------------

    def test_release_layout_check_matches_the_device(self):
        checker = SCRIPT.parents[2] / "scripts/verify-package-layout.py"
        good, _ = self.package("2.5.4.1")
        result = subprocess.run(["python3", str(checker), good], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.base / "bad").mkdir()
        bad, _ = self.package("2.5.4.1", self.entries("2.5.4.1", skip=("kvm_new_app",)),
                              name="bad/nanokvm_2.5.4.1.tar.gz")
        result = subprocess.run(["python3", str(checker), bad], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("kvm_new_app", result.stderr)

    def test_release_layout_check_writes_no_bytecode(self):
        # A writable copy without any cache, so a regression would show up here.
        copy = self.base / "repo"
        (copy / "scripts").mkdir(parents=True)
        (copy / "kvmapp/system").mkdir(parents=True)
        shutil.copy(SCRIPT.parents[2] / "scripts/verify-package-layout.py", copy / "scripts")
        shutil.copy(SCRIPT, copy / "kvmapp/system")
        good, _ = self.package("2.5.4.1")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
        subprocess.run(["python3", str(copy / "scripts/verify-package-layout.py"), good], env=env, check=True,
                       capture_output=True)
        self.assertEqual(list(copy.rglob("__pycache__")), [])

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

    def serve_releases(self, version, package_path, sha512, size_error=0, unpacked_error=0):
        """Serve GitHub-like release URLs that redirect to another path, as GitHub does."""
        package = pathlib.Path(package_path).read_bytes()
        with tarfile.open(package_path, "r:gz") as tar:
            unpacked = sum(m.size for m in tar if m.isreg())
        manifest = json.dumps({"manifest_version": 2, "version": version, "name": f"nanokvm_{version}.tar.gz",
                               "sha512": sha512, "size_bytes": len(package) + size_error,
                               "unpacked_size_bytes": unpacked + unpacked_error}).encode()
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

    def test_download_larger_than_manifest_is_refused(self):
        path, digest = self.package("2.5.4.1")
        self.serve_releases("2.5.4.1", path, digest, size_error=-1)
        self.assert_rejected("larger than", dry_run=True)

    def test_download_smaller_than_manifest_is_refused(self):
        path, digest = self.package("2.5.4.1")
        self.serve_releases("2.5.4.1", path, digest, size_error=+1)
        self.assert_rejected("latest.json says", dry_run=True)

    def test_unpacked_size_must_match_manifest(self):
        path, digest = self.package("2.5.4.1")
        self.serve_releases("2.5.4.1", path, digest, unpacked_error=1)
        self.assert_rejected("unpacks to", dry_run=True)

    def test_default_source_is_the_fork(self):
        self.assertEqual(load().RELEASES, "https://github.com/akkijp/NanoKVM/releases")
        self.assertNotIn("sipeed", SCRIPT.read_text())



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
        self.last_status = result.returncode
        return result, (self.base / "run/nanokvm-microphone").read_text().strip()

    def test_capture_card_is_unbound(self):
        result, status = self.run_disable()
        self.assertEqual((self.base / "drv0/unbind").read_text().strip(), "adc")
        self.assertEqual((self.base / "drv1/unbind").read_text(), "")  # playback card untouched
        self.assertEqual(status, "off")
        self.assertEqual(self.last_status, 0)

    def test_failure_is_recorded_and_returned(self):
        result, status = self.run_disable(unbind_removes=False)
        self.assertTrue(status.startswith("FAILED"), status)
        self.assertIn("could not be disabled", result.stderr)
        self.assertEqual(self.last_status, 1)

    def test_enable_file_keeps_it_and_reports_state(self):
        (self.base / "etc/kvm/enable-microphone").write_text("")
        result, status = self.run_disable()
        self.assertEqual((self.base / "drv0/unbind").read_text(), "")
        self.assertEqual(status, "on")

    def run_s95(self, action, mic_fails=False, picoclaw_fails=False, pidof_after_start=False):
        """Run the real S95nanokvm branch with everything that touches the device stubbed out,
        recording the order of the fork's boot steps and the daemon starts."""
        log = self.base / "order.log"
        text = S95.read_text()
        text = text.replace("exec 9>/run/nanokvm-service.lock || exit 1", "exec 9>/dev/null")
        text = text.replace("flock -n 9 ||", "true ||")
        text = text.replace("/tmp/kvm_system/kvm_system 9>&- &", "record daemon-kvm_system")
        text = text.replace("/tmp/server/NanoKVM-Server 9>&- &", "record daemon-NanoKVM-Server; : > " + str(self.base / "started"))
        # Every write the script makes goes under the temp dir, never to the host.
        fake = self.base / "fs"
        for prefix in ("/boot/", "/root/", "/device_key", "/mnt/", "/kvmapp", "/sys/", "/etc/", "/run/"):
            text = text.replace(prefix, f"{fake}{prefix}")
        for d in ("boot", "root", "mnt/data", "kvmapp/server/web", "sys", "etc/init.d", "run"):
            (fake / d).mkdir(parents=True, exist_ok=True)
        stubs = f"""
record() {{ echo "$1" >> {log}; }}
disable_audio_capture() {{ record mic; return {1 if mic_fails else 0}; }}
disable_picoclaw() {{ record picoclaw; return {1 if picoclaw_fails else 0}; }}
pidof() {{ [ -f {self.base}/started ] && return {0 if pidof_after_start else 1}; return 1; }}
killall() {{ :; }}
iptables() {{ return 0; }}
rm() {{ case "$*" in *update-restart-pending*) command rm "$@" ;; esac; }}
cp() {{ :; }}
mv() {{ :; }}
awk() {{ :; }}
sync() {{ :; }}
reboot() {{ record reboot; }}
sleep() {{ :; }}
"""
        head, case = text.split('case "$1" in', 1)
        script = head + stubs + 'case "$1" in' + case
        result = subprocess.run(["sh", "-c", script, "S95nanokvm", action], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1 if (mic_fails or picoclaw_fails) else 0, result.stderr)
        if action == "start":  # written into the fake /boot, not the host's (macOS sh prints "-n" literally)
            self.assertTrue((fake / "boot/hostname.prefix").read_text().strip().endswith("kvm"))
        return log.read_text().split() if log.exists() else []

    def test_start_and_restart_both_run_the_boot_steps_before_the_daemons(self):
        for action in ("start", "restart"):
            with self.subTest(action=action):
                (self.base / "order.log").unlink(missing_ok=True)
                order = self.run_s95(action)
                self.assertIn("mic", order)
                self.assertIn("picoclaw", order)
                self.assertIn("daemon-NanoKVM-Server", order)
                first_daemon = min(order.index("daemon-kvm_system"), order.index("daemon-NanoKVM-Server"))
                self.assertLess(order.index("mic"), first_daemon)
                self.assertLess(order.index("picoclaw"), first_daemon)

    def test_record_is_cleared_only_when_both_daemons_stay_up(self):
        for daemons_up, cleared in ((True, True), (False, False)):
            with self.subTest(daemons_up=daemons_up):
                fake = self.base / "fs"
                record = fake / "etc/kvm/update-restart-pending"
                record.parent.mkdir(parents=True, exist_ok=True)
                record.write_text("")
                self.run_s95("start", pidof_after_start=daemons_up)
                for _ in range(100):  # the check runs in the background
                    if record.exists() != cleared:
                        break
                    __import__("time").sleep(0.05)
                self.assertEqual(record.exists(), not cleared)

    def test_picoclaw_failure_fails_the_start(self):
        for action in ("start", "restart"):
            with self.subTest(action=action):
                (self.base / "order.log").unlink(missing_ok=True)
                order = self.run_s95(action, picoclaw_fails=True)  # asserts exit status 1
                self.assertIn("daemon-NanoKVM-Server", order)

    def test_disable_picoclaw_reports_a_failed_disable(self):
        etc = self.base / "etc/init.d"
        etc.mkdir(parents=True)
        (etc / "S96picoclaw").write_text("#!/bin/sh\n")
        body = s95_function("wait_for_exit") + s95_function("disable_picoclaw")
        body = body.replace("/etc/init.d", str(etc)).replace("/kvmapp/", f"{self.base}/kvmapp/")
        script = "pidof() { return 1; }\nmv() { return 1; }\n" + body + "\ndisable_picoclaw\n"
        result = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Failed to disable", result.stderr)

    def test_start_while_running_still_reports_the_microphone(self):
        log = self.base / "order.log"
        text = S95.read_text()
        start_services = s95_function("start_services")
        script = f"""
disable_audio_capture() {{ return 1; }}
disable_picoclaw() {{ :; }}
pidof() {{ return 0; }}
{start_services}
start_services
"""
        result = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)

    def test_microphone_failure_fails_the_start_but_the_kvm_runs(self):
        for action in ("start", "restart"):
            with self.subTest(action=action):
                (self.base / "order.log").unlink(missing_ok=True)
                order = self.run_s95(action, mic_fails=True)  # asserts exit status 1
                self.assertIn("daemon-NanoKVM-Server", order)
                self.assertIn("daemon-kvm_system", order)


if __name__ == "__main__":
    unittest.main()
