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
        self.u.SERVICE_LOCK = str(self.base / "service.lock")
        self.u.INSTALL_COUNT = str(self.base / "update-count")
        self.u.INSTALL_COUNT_FAILED = str(self.base / "update-count-failed")
        etc = self.base / "etc"
        self.u.STOP_SCRIPT = str(etc / "init.d/S95nanokvm")
        write_script(pathlib.Path(self.u.STOP_SCRIPT),
                     f"#!/bin/sh\necho \"etc-S95 $1\" >> {self.base}/root/calls.log\nexit 0\n".encode())
        self.u.STAGE_DIR = str(self.base / "root/kvmapp.new")
        self.u.SERVICE_LOG = str(self.base / "root/.kvm-update/service.log")
        self.u.MIC_STATUS = str(self.base / "mic-status")
        self.u.PICOCLAW_STATUS = str(self.base / "picoclaw-status")
        self.u.RESTART_PENDING = str(self.base / "restart-pending")
        self.u.MOUNTS = str(self.base / "mounts")
        pathlib.Path(self.u.MOUNTS).write_text("")
        self.u.stopped = lambda: True
        self.u.started = lambda: True
        self.u.running = lambda name: False
        self.u.daemons_run_app = lambda since: False
        self.real_migration_reboots = self.u.migration_reboots
        self.u.migration_reboots = lambda new_init: ""
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
        init = path / "system/init.d/S95nanokvm"
        if not init.exists():
            write_script(init, OK_SCRIPT)

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
            "server/dl_lib.list": b"libkvm.so\n",
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
        self.u.daemons_run_app = lambda since: True
        path, digest = self.package("2.5.4.1")
        self.run_update(file=path, sha512=digest)
        self.assertEqual(self.installed_version(), "2.5.4.1")

    def test_old_processes_with_new_files_do_not_clear_the_record(self):
        # A web update replaced /kvmapp, its restart failed, the old daemons still run from /tmp.
        (pathlib.Path(self.u.APP_DIR) / "version").write_text("2.5.4.1\n")
        pathlib.Path(self.u.RESTART_PENDING).write_text('{"from": "2.4.3", "to": "2.5.4.1", "phase": "installed"}')
        self.u.running = lambda name: True
        self.u.daemons_run_app = lambda since: False  # the running binaries are not the ones in /kvmapp
        path, digest = self.package("2.5.4.2")
        self.assert_rejected_keeping("not been seen running", file=path, sha512=digest)
        self.assertTrue(os.path.exists(self.u.RESTART_PENDING))

    def test_switching_record_with_files_moved_to_backup_is_kept(self):
        # The web updater had moved part of /kvmapp to /root/old when power was lost.
        pathlib.Path(self.u.RESTART_PENDING).write_text('{"from": "2.4.3", "to": "2.5.4.1", "phase": "switching"}')
        (pathlib.Path(self.u.BACKUP_DIR) / "kvm").mkdir(parents=True)
        (pathlib.Path(self.u.BACKUP_DIR) / "kvm/fps").write_text("30")
        self.u.daemons_run_app = lambda since: True
        path, digest = self.package("2.5.4.1")
        with self.assertRaisesRegex(self.u.UpdateError, "stopped while replacing"):
            self.run_update(file=path, sha512=digest)
        self.assertTrue((pathlib.Path(self.u.BACKUP_DIR) / "kvm/fps").exists())

    def test_backup_of_empty_directories_counts_as_absent(self):
        # What the web updater leaves after moving a backup back file by file.
        (pathlib.Path(self.u.BACKUP_DIR) / "server/dl_lib").mkdir(parents=True)
        path, digest = self.package("2.5.4.1")
        self.run_update(file=path, sha512=digest)
        self.assertEqual(self.installed_version(), "2.5.4.1")

    def test_daemons_run_app_checks_binaries_and_start_time(self):
        u = load()
        u.APP_DIR = self.u.APP_DIR
        proc = self.base / "proc"
        (proc / "1").mkdir(parents=True)
        (proc / "1/exe").write_text("old")  # same content as the /kvmapp binaries
        # starttime (field 22) = 500 ticks after boot; btime = 1000 s
        fields = ["S"] + ["0"] * 18 + ["500"] + ["0"] * 10
        (proc / "1/stat").write_text("1 (kvm system) " + " ".join(fields) + "\n")
        (proc / "stat").write_text("cpu 0\nbtime 1000\n")  # started in 1970: up for a long time
        u.PROC = str(proc)
        u.pids = lambda name: ["1"]
        start = 1000 + 500 / os.sysconf("SC_CLK_TCK")
        self.assertTrue(u.daemons_run_app(start - 1))   # started after the record
        self.assertFalse(u.daemons_run_app(start + 1))  # an old process with identical binaries
        (proc / "1/exe").write_text("other")
        self.assertFalse(u.daemons_run_app(start - 1))
        u.pids = lambda name: []
        self.assertFalse(u.daemons_run_app(start - 1))

    def test_daemons_up_only_briefly_do_not_count(self):
        u = load()
        u.APP_DIR = self.u.APP_DIR
        proc = self.base / "proc"
        (proc / "1").mkdir(parents=True)
        (proc / "1/exe").write_text("old")
        fields = ["S"] + ["0"] * 18 + ["0"] + ["0"] * 10
        (proc / "1/stat").write_text("1 (srv) " + " ".join(fields) + "\n")
        (proc / "stat").write_text(f"btime {int(__import__('time').time()) - 3}\n")  # started 3 s ago
        u.PROC, u.pids = str(proc), (lambda name: ["1"])
        self.assertFalse(u.daemons_run_app(0))

    def assert_rejected_keeping(self, message, **kw):
        with self.assertRaisesRegex(self.u.UpdateError, message):
            self.run_update(**kw)

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

    def test_record_write_failure_leaves_no_partial_record(self):
        pathlib.Path(self.u.RESTART_PENDING).write_text('{"from": "2.4.2", "to": "2.4.3", "phase": "installed"}\n')
        real_write = os.write

        def failing_write(fd, data):
            if b"switching" in bytes(data):
                raise OSError("no space left on device")
            return real_write(fd, data)

        self.u.os = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
        self.u.os.write, self.u.os.path = failing_write, os.path
        with self.assertRaises(OSError):
            self.u.set_restart_pending("switching", "2.4.3", "2.5.4.1")
        self.assertIn('"installed"', pathlib.Path(self.u.RESTART_PENDING).read_text())
        leftovers = [n for n in os.listdir(os.path.dirname(self.u.RESTART_PENDING)) if n.startswith(".update-restart-pending.")]
        self.assertEqual(leftovers, [])

    def test_record_already_cleared_by_s95_is_fine(self):
        self.u.clear_restart_pending()  # nothing there
        path, digest = self.package("2.5.4.1")
        real_clear = self.u.clear_restart_pending

        def cleared_meanwhile():
            if os.path.exists(self.u.RESTART_PENDING):
                os.remove(self.u.RESTART_PENDING)  # S95 got there first
            real_clear()

        self.u.clear_restart_pending = cleared_meanwhile
        self.assertEqual(self.run_update(file=path, sha512=digest), "2.5.4.1")

    def test_files_reach_the_disk_before_installed_is_recorded(self):
        order = []
        real_tree, real_set = self.u.fsync_tree, self.u.set_restart_pending
        self.u.fsync_tree = lambda path: (order.append("sync"), real_tree(path))[1]
        self.u.set_restart_pending = lambda phase, a, b: (order.append(phase), real_set(phase, a, b))[1]
        path, digest = self.package("2.5.4.1")
        self.run_update(file=path, sha512=digest)
        self.assertLess(order.index("sync"), order.index("switching"))  # the stage, before anything stops
        self.assertEqual(order[-1], "installed")

    def test_write_back_error_keeps_switching_and_does_not_start(self):
        real_fsync_path = self.u.fsync_path

        def fsync_path(path):
            if path.endswith("NanoKVM-Server"):
                raise OSError(5, "Input/output error")
            real_fsync_path(path)

        self.u.fsync_path = fsync_path
        path, digest = self.package("2.5.4.1")
        # Caught in the stage: nothing stopped, nothing recorded.
        self.assert_rejected("could not write the new version to the card", file=path, sha512=digest)
        self.assertFalse(os.path.exists(self.u.RESTART_PENDING))
        self.assertEqual(self.calls(), "")

    def test_write_back_error_of_the_switch_keeps_switching(self):
        real_fsync_path = self.u.fsync_path
        stage_done = {"yes": False}
        real_tree = self.u.fsync_tree
        self.u.fsync_tree = lambda path: (real_tree(path), stage_done.update(yes=True))[0]

        def fsync_path(path):
            if stage_done["yes"] and path == os.path.dirname(self.u.APP_DIR) and os.path.exists(self.u.RESTART_PENDING):
                raise OSError(5, "Input/output error")
            real_fsync_path(path)

        self.u.fsync_path = fsync_path
        path, digest = self.package("2.5.4.1")
        with self.assertRaisesRegex(self.u.UpdateError, "writing it to the card failed"):
            self.run_update(file=path, sha512=digest)
        self.assertEqual(json.loads(pathlib.Path(self.u.RESTART_PENDING).read_text())["phase"], "switching")

    def test_unlistable_directory_is_a_write_back_error(self):
        broken = self.base / "tree/sub"
        broken.mkdir(parents=True)
        real_scandir = os.scandir

        def scandir(path="."):
            if str(path) == str(broken):
                raise OSError(5, "Input/output error")
            return real_scandir(path)

        self.u.os = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
        self.u.os.path, self.u.os.scandir = os.path, scandir
        import unittest.mock as mock
        with mock.patch("os.scandir", scandir):
            with self.assertRaises(OSError):
                self.u.fsync_tree(str(self.base / "tree"))

    def test_previous_version_must_be_a_version(self):
        for bad in (b"2.5.4\x00", b"2.5.4 junk", b"v2.5.4", b"2.5"):
            with self.subTest(value=bad):
                (pathlib.Path(self.u.APP_DIR) / "version").write_bytes(bad + b"\n")
                with self.assertRaisesRegex(self.u.UpdateError, "not a version|cannot read"):
                    self.u.read_previous_version()
        for good in (b"2.5.4", b"2.4.3", b"2.5.4.1", b"2.5.0-rc1", b"2.5.4+build.7"):
            (pathlib.Path(self.u.APP_DIR) / "version").write_bytes(good + b"\n")
            self.assertEqual(self.u.read_previous_version(), good.decode())

    def test_trash_lives_in_the_updater_area_and_inputs_there_are_refused(self):
        self.assertTrue(self.u.backup_trash().startswith(self.u.WORK_PARENT + os.sep))
        trash = pathlib.Path(self.u.backup_trash())
        trash.mkdir(parents=True)
        path, digest = self.package("2.5.4.1", name="root/.kvm-update/old.removing/nanokvm_2.5.4.1.tar.gz")
        self.assert_rejected("copy it elsewhere", file=path, sha512=digest)
        self.assertTrue(pathlib.Path(path).exists())

    def test_record_removed_while_it_is_checked_is_not_an_error(self):
        pathlib.Path(self.u.RESTART_PENDING).write_text('{"from": "2.4.3", "to": "2.5.4.1", "phase": "installed"}\n')
        real_read = self.u.read_pending

        def read_then_gone():
            record = real_read()
            if os.path.exists(self.u.RESTART_PENDING):
                os.remove(self.u.RESTART_PENDING)  # S95's check finished meanwhile
            return record

        self.u.read_pending = read_then_gone
        self.u.daemons_run_app = lambda since: True
        path, digest = self.package("2.5.4.1")
        self.assertEqual(self.run_update(file=path, sha512=digest), "2.5.4.1")

    def test_installed_record_that_cannot_be_saved_does_not_start(self):
        real_set = self.u.set_restart_pending

        def set_pending(phase, a, b):
            real_set(phase, a, b)
            if phase == "installed":
                raise OSError(5, "Input/output error")  # e.g. the directory sync after the rename

        self.u.set_restart_pending = set_pending
        path, digest = self.package("2.5.4.1")
        with self.assertRaisesRegex(self.u.UpdateError, "record could not be saved"):
            self.run_update(file=path, sha512=digest)
        self.assertFalse(self.calls().splitlines()[-1].endswith("S95nanokvm restart"))

    def test_empty_arguments_are_errors_not_omissions(self):
        path, digest = self.package("2.5.4.1")
        for kw in ({"file": "", "sha512": digest}, {"file": path, "sha512": ""}, {"version": ""},
                   {"sha512": "", "dry_run": True}):
            with self.subTest(kw=kw):
                self.assert_rejected("empty value", **kw)

    def test_app_without_a_version_file_can_be_updated(self):
        (pathlib.Path(self.u.APP_DIR) / "version").unlink()  # a very old device
        path, digest = self.package("2.5.4.1")
        self.assertEqual(self.run_update(file=path, sha512=digest), "2.5.4.1")

    def test_unreadable_version_file_is_still_refused(self):
        version = pathlib.Path(self.u.APP_DIR) / "version"
        version.unlink()
        version.mkdir()  # exists, but cannot be read as a file
        path, digest = self.package("2.5.4.1")
        with self.assertRaisesRegex(self.u.UpdateError, "cannot read"):
            self.run_update(file=path, sha512=digest)

    def test_record_of_a_device_without_a_version_file_can_settle(self):
        (pathlib.Path(self.u.APP_DIR) / "version").unlink()
        pathlib.Path(self.u.RESTART_PENDING).write_text('{"from": "", "to": "2.5.4.1", "phase": "switching"}\n')
        self.u.daemons_run_app = lambda since: True
        path, digest = self.package("2.5.4.1")
        self.assertEqual(self.run_update(file=path, sha512=digest), "2.5.4.1")

    def test_empty_version_file_does_not_settle_a_record(self):
        (pathlib.Path(self.u.APP_DIR) / "version").write_text("")
        pathlib.Path(self.u.RESTART_PENDING).write_text('{"from": "", "to": "2.5.4.1", "phase": "installed"}\n')
        self.u.daemons_run_app = lambda since: True
        path, digest = self.package("2.5.4.1")
        self.assert_rejected_keeping("not been seen running", file=path, sha512=digest)

    def test_record_is_not_settled_while_picoclaw_is_on(self):
        pathlib.Path(self.u.RESTART_PENDING).write_text('{"from": "2.4.2", "to": "2.4.3", "phase": "installed"}\n')
        pathlib.Path(self.u.PICOCLAW_STATUS).write_text("FAILED: picoclaw could not be stopped\n")
        self.u.daemons_run_app = lambda since: True
        path, digest = self.package("2.5.4.1")
        self.assert_rejected_keeping("not been seen running", file=path, sha512=digest)
        self.assertTrue(os.path.exists(self.u.RESTART_PENDING))

    def test_started_needs_the_same_processes_for_the_whole_wait(self):
        u = load()
        u.time = types.SimpleNamespace(sleep=lambda s: None, monotonic=__import__("time").monotonic)
        answers = iter([["10"], ["20"], ["11"], ["20"]])  # NanoKVM-Server was restarted meanwhile
        u.pids = lambda name: next(answers)
        self.assertFalse(u.started())
        steady = {"NanoKVM-Server": ["10"], "kvm_system": ["20"]}
        u.pids = lambda name: steady[name]
        self.assertTrue(u.started())

    def test_installed_record_must_be_complete(self):
        cases = {
            '{"from": "2.5.4", "to": "2.5.4.1", "phase": "installed"}\n': "installed",
            '{"from":"","to":"2.5.4.1","phase":"installed"}\n': "installed",
            '{"phase": "installed"}\n': "switching",
            '{"to": "2.5.4.1", "phase": "installed"}\n': "switching",
            '{"from": "2.5.4", "to": "", "phase": "installed"}\n': "switching",
        }
        for content, phase in cases.items():
            with self.subTest(content=content):
                pathlib.Path(self.u.RESTART_PENDING).write_text(content)
                self.assertEqual(self.u.read_pending()["phase"], phase)

    def test_switch_waits_for_the_service_lock(self):
        import fcntl
        held = open(self.u.SERVICE_LOCK, "w")
        fcntl.flock(held, fcntl.LOCK_EX)
        seen = []
        real_set = self.u.set_restart_pending

        def set_pending(phase, a, b):
            seen.append(phase)
            real_set(phase, a, b)

        self.u.set_restart_pending = set_pending
        sleeps = {"n": 0}

        def sleep(seconds):
            sleeps["n"] += 1
            if sleeps["n"] == 3:
                held.close()  # S95's operation ends

        self.u.time = types.SimpleNamespace(sleep=sleep, monotonic=__import__("time").monotonic)
        path, digest = self.package("2.5.4.1")
        self.run_update(file=path, sha512=digest)
        self.assertGreaterEqual(sleeps["n"], 3)  # waited before recording "switching"
        self.assertEqual(seen[0], "switching")

    def test_record_with_non_version_text_is_not_installed(self):
        for content in ('{"from": "\\q", "to": "2.5.4.1", "phase": "installed"}\n',
                        '{"from": "2.5.4 x", "to": "2.5.4.1", "phase": "installed"}\n'):
            with self.subTest(content=content):
                pathlib.Path(self.u.RESTART_PENDING).write_text(content)
                self.assertEqual(self.u.read_pending()["phase"], "switching")

    def test_s95_record_rule_matches_the_others(self):
        body = s95_function("pending_installed")
        cases = {
            '{"from":"2.5.4","to":"2.5.4.1","phase":"installed"}': True,
            '{"from": "", "to": "2.5.4.1", "phase": "installed"}': True,
            '{"from":"\\q","to":"2.5.4.1","phase":"installed"}': False,
            '{"from":"2.5.4 x","to":"2.5.4.1","phase":"installed"}': False,
            '{"phase":"installed"}': False,
        }
        for content, installed in cases.items():
            with self.subTest(content=content):
                record = self.base / "pending"
                record.write_text(content + "\n")
                script = f"PENDING={record}\n" + body + "\npending_installed\n"
                result = subprocess.run(["sh", "-c", script], capture_output=True)
                self.assertEqual(result.returncode == 0, installed)

    def locking_init_script(self, label):
        """An init script that, like S95nanokvm, refuses to run while its lock is held."""
        return (f"#!/usr/bin/env python3\n"
                f"import fcntl, sys\n"
                f"lock = open({self.u.SERVICE_LOCK!r}, 'w')\n"
                f"try:\n"
                f"    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                f"except OSError:\n"
                f"    sys.exit(1)\n"
                f"open({str(self.base / 'root/calls.log')!r}, 'a').write({label!r} + ' ' + sys.argv[1] + '\\n')\n").encode()

    def test_previous_version_starts_after_a_failed_switch(self):
        write_script(pathlib.Path(self.u.APP_DIR) / "system/init.d/S95nanokvm", self.locking_init_script("old-S95"))
        real_rename = os.rename

        def rename(src, dst):
            if src == self.u.STAGE_DIR:
                raise OSError("no space left")
            real_rename(src, dst)

        self.u.os = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
        self.u.os.rename, self.u.os.path = rename, os.path
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("previous version was started again", file=path, sha512=digest)
        self.assertIn("old-S95 restart", self.calls())

    def test_previous_version_starts_after_a_failed_record(self):
        write_script(pathlib.Path(self.u.STOP_SCRIPT), self.locking_init_script("etc-S95"))
        real_set = self.u.set_restart_pending

        def set_pending(phase, a, b):
            if phase == "switching":
                raise OSError("no space left on device")
            real_set(phase, a, b)

        self.u.set_restart_pending = set_pending
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("previous version was started again", file=path, sha512=digest)
        self.assertIn("etc-S95 restart", self.calls())

    def test_a_start_that_slipped_in_after_the_stop_aborts_the_switch(self):
        state = {"locked": False}
        real_lock = self.u.lock_service_operations

        def lock(opened):
            state["locked"] = True
            return real_lock(opened)

        self.u.lock_service_operations = lock
        # The daemons are gone at the stop, but running again once the lock is taken.
        self.u.running = lambda name: state["locked"]
        self.complete_app(self.u.BACKUP_DIR)
        (pathlib.Path(self.u.BACKUP_DIR) / "keep").write_text("previous backup")
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("started again after it had been stopped", file=path, sha512=digest)
        # Nothing was changed, the previous backup included.
        self.assertEqual((pathlib.Path(self.u.BACKUP_DIR) / "keep").read_text(), "previous backup")
        self.assertFalse(os.path.exists(self.u.RESTART_PENDING))
        self.assertFalse(os.path.exists(self.u.STAGE_DIR))

    def test_boot_status_needs_explicit_success_for_the_new_version(self):
        mic, pico = pathlib.Path(self.u.MIC_STATUS), pathlib.Path(self.u.PICOCLAW_STATUS)
        cases = [
            ("off", "off", False, True),
            ("on", "off", False, True),
            ("off (enabled in /etc/kvm, reboot to turn it on)", "off", False, True),
            ("", "off", False, False),          # empty: could not write FAILED
            (None, "off", False, False),        # missing
            ("off", None, False, False),
            ("FAILED: capture still present on card 0", "off", False, False),
            ("off", "FAILED: picoclaw could not be stopped", False, False),
            (None, None, True, True),           # back on the "from" version (maybe upstream)
            (None, None, False, False),
            ("", "", True, False),
        ]
        for m, p_, recovering, ok in cases:
            with self.subTest(mic=m, pico=p_, recovering=recovering):
                for path, value in ((mic, m), (pico, p_)):
                    if value is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_text(value + "\n" if value else "")
                self.assertEqual(self.u.boot_statuses_ok(recovering), ok)

    def test_half_moved_app_from_an_upstream_web_update_keeps_the_backup(self):
        # Upstream moved /kvmapp to /root/old in name order and stopped after kvm_system/ and
        # server/: the daemons are on both sides (copied back by hand or a retry), but system/
        # and version are only in /root/old.
        self.complete_app(self.u.BACKUP_DIR)
        (pathlib.Path(self.u.BACKUP_DIR) / "version").write_text("2.5.4\n")
        app = pathlib.Path(self.u.APP_DIR)
        (app / "version").unlink()
        shutil.rmtree(app / "system" / "init.d")
        before = sorted(str(p.relative_to(self.u.BACKUP_DIR)) for p in pathlib.Path(self.u.BACKUP_DIR).rglob("*"))
        path, digest = self.package("2.5.4.1")
        with self.assertRaisesRegex(self.u.UpdateError, "incomplete"):
            self.run_update(file=path, sha512=digest)
        after = sorted(str(p.relative_to(self.u.BACKUP_DIR)) for p in pathlib.Path(self.u.BACKUP_DIR).rglob("*"))
        self.assertEqual(before, after)
        self.assertNotIn("stop", self.calls())  # refused before anything was stopped

    def test_version_only_in_the_backup_is_refused(self):
        self.complete_app(self.u.BACKUP_DIR)
        (pathlib.Path(self.u.BACKUP_DIR) / "version").write_text("2.5.4\n")
        (pathlib.Path(self.u.APP_DIR) / "version").unlink()
        path, digest = self.package("2.5.4.1")
        with self.assertRaisesRegex(self.u.UpdateError, r"version \(it is in the backup\)"):
            self.run_update(file=path, sha512=digest)
        self.assertFalse((pathlib.Path(self.u.APP_DIR) / "version").exists())
        self.assertTrue((pathlib.Path(self.u.BACKUP_DIR) / "version").exists())

    def test_each_install_is_counted(self):
        for n, version in enumerate(("2.5.4.1", "2.5.4.1"), start=1):
            path, digest = self.package(version)
            self.run_update(file=path, sha512=digest)
            self.assertEqual(pathlib.Path(self.u.INSTALL_COUNT).read_text(), f"{n}\n")
            pathlib.Path(self.u.RESTART_PENDING).unlink(missing_ok=True)

    def test_libraries_are_checked_against_the_packages_own_list(self):
        base = self.entries("2.5.4.2", skip=("server/dl_lib.list",))
        root = "nanokvm_2.5.4.2"

        def with_list(listing, *libs):
            return base + [(f"{root}/server/dl_lib.list", listing.encode())] + [
                (f"{root}/server/dl_lib/{lib}", b"x") for lib in libs]

        ok = self.package("2.5.4.2", with_list("libkvm.so\nlibsys.so\n", "libsys.so"))
        (self.base / "x1").mkdir()
        self.u.extract(ok[0], "2.5.4.2", str(self.base / "x1"))  # fewer libraries than today: fine
        for entries, message in (
            (with_list("libkvm.so\nlibsys.so\n"), "missing server/dl_lib/libsys.so"),
            (with_list("libkvm.so\n", "libextra.so"), "libextra.so, which its list does not name"),
            (with_list("libsys.so\n", "libsys.so"), "does not name libkvm.so"),
            (with_list("libkvm.so\n" + "a" * 20000 + "\n"), "larger than"),
            (with_list("libkvm.so\n" + "x" * 200 + "\n"), "too long"),
        ):
            with self.subTest(message=message):
                path, _ = self.package("2.5.4.2", entries, name=f"p{abs(hash(message))}/nanokvm_2.5.4.2.tar.gz")
                work = self.base / f"x{abs(hash(message))}"
                work.mkdir()
                with self.assertRaisesRegex(self.u.UpdateError, message):
                    self.u.extract(path, "2.5.4.2", str(work))

    def test_lock_file_that_cannot_be_created_stops_nothing(self):
        def fail():
            raise OSError(28, "No space left on device")

        self.u.open_service_lock = fail
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("nothing was stopped or changed", file=path, sha512=digest)
        self.assertNotIn("stop", self.calls())

    def test_a_count_that_cannot_be_saved_is_reported(self):
        pathlib.Path(self.u.INSTALL_COUNT).mkdir()
        path, digest = self.package("2.5.4.1")
        self.run_update(file=path, sha512=digest)
        self.assertTrue(os.path.exists(self.u.INSTALL_COUNT_FAILED))

    def test_reboot_message_names_the_reason(self):
        self.u.migration_reboots = lambda new_init: "installs its own init script (S95nanokvm)"
        self.u.time = types.SimpleNamespace(sleep=lambda s: None, monotonic=__import__("time").monotonic)
        said = []
        self.u.say = lambda message, err=False: said.append(message)
        path, digest = self.package("2.5.4.1")
        self.run_update(file=path, sha512=digest)
        self.assertTrue(any("installs its own init script" in m for m in said), said)
        self.assertFalse(any("HDMI" in m for m in said), said)

    def test_previous_backup_is_removed_before_switching_is_recorded(self):
        self.complete_app(self.u.BACKUP_DIR)
        seen = []
        real_set = self.u.set_restart_pending
        self.u.set_restart_pending = lambda phase, a, b: (seen.append((phase, os.path.exists(self.u.BACKUP_DIR))), real_set(phase, a, b))[1]
        path, digest = self.package("2.5.4.1")
        self.run_update(file=path, sha512=digest)
        self.assertEqual(seen[0], ("switching", False))

    def test_cut_short_backup_removal_does_not_block_the_next_update(self):
        self.complete_app(self.u.BACKUP_DIR)
        real_rmtree = shutil.rmtree
        state = {"cut": True}

        def rmtree(path, *a, **kw):
            if state["cut"] and path == self.u.backup_trash():
                state["cut"] = False
                os.remove(os.path.join(path, "version"))  # part of it is gone, then power is lost
                raise OSError("power lost")
            real_rmtree(path, *a, **kw)

        self.u.shutil = types.SimpleNamespace(**{k: getattr(shutil, k) for k in dir(shutil) if not k.startswith("__")})
        self.u.shutil.rmtree = rmtree
        path, digest = self.package("2.5.4.1")
        with self.assertRaises(self.u.UpdateError):
            self.run_update(file=path, sha512=digest)
        self.assertFalse(os.path.exists(self.u.BACKUP_DIR))  # renamed away: never half-deleted
        self.assertEqual(self.installed_version(), "2.4.3")
        # The next run finishes the removal and installs.
        self.assertEqual(self.run_update(file=path, sha512=digest), "2.5.4.1")
        self.assertFalse(os.path.exists(self.u.backup_trash()))

    def test_short_writes_still_write_the_whole_record(self):
        real_write = os.write
        self.u.os = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
        self.u.os.write = lambda fd, data: real_write(fd, bytes(data[:5]))  # 5 bytes at a time
        self.u.os.path = os.path
        self.u.set_restart_pending("installed", "2.4.3", "2.5.4.1")
        self.assertEqual(json.loads(pathlib.Path(self.u.RESTART_PENDING).read_text())["phase"], "installed")

    def test_short_write_then_failure_keeps_the_previous_record(self):
        pathlib.Path(self.u.RESTART_PENDING).write_text('{"from": "2.4.2", "to": "2.4.3", "phase": "installed"}\n')
        real_write = os.write
        calls = {"n": 0}

        def write(fd, data):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_write(fd, bytes(data[:10]))
            raise OSError("no space left on device")

        self.u.os = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
        self.u.os.write, self.u.os.path = write, os.path
        with self.assertRaises(OSError):
            self.u.set_restart_pending("switching", "2.4.3", "2.5.4.1")
        self.assertIn('"to": "2.4.3"', pathlib.Path(self.u.RESTART_PENDING).read_text())

    def test_longest_allowed_version_installs_and_reads_back(self):
        longest = "999999999.999999999.999999999.999999999"
        path, digest = self.package(longest)
        self.assertEqual(self.run_update(file=path, sha512=digest), longest)
        self.assertEqual(self.u.read_previous_version(), longest)
        too_long = "1234567890.0.0.1"
        path, digest = self.package(too_long)
        self.assert_rejected_keeping("must be named", file=path, sha512=digest)

    def test_failed_backup_removal_records_nothing(self):
        self.complete_app(self.u.BACKUP_DIR)
        real_rmtree = shutil.rmtree

        def rmtree(path, *a, **kw):
            if path.startswith(self.u.BACKUP_DIR) or path == self.u.backup_trash():
                raise OSError("I/O error")
            real_rmtree(path, *a, **kw)

        self.u.shutil = types.SimpleNamespace(**{k: getattr(shutil, k) for k in dir(shutil) if not k.startswith("__")})
        self.u.shutil.rmtree = rmtree
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("could not remove the previous backup", file=path, sha512=digest)
        self.assertFalse(os.path.exists(self.u.RESTART_PENDING))

    def test_failed_switch_and_failed_restore_without_a_version_file(self):
        (pathlib.Path(self.u.APP_DIR) / "version").unlink()  # a very old device
        real_rename = os.rename

        def rename(src, dst):
            if src == self.u.STAGE_DIR or src == self.u.BACKUP_DIR:
                raise OSError("I/O error")
            real_rename(src, dst)

        self.u.os = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
        self.u.os.rename, self.u.os.path = rename, os.path
        path, digest = self.package("2.5.4.1")
        with self.assertRaisesRegex(self.u.UpdateError, "could not be put back either"):
            self.run_update(file=path, sha512=digest)
        self.assertTrue(os.path.exists(self.u.RESTART_PENDING))
        self.assertTrue(os.path.isdir(self.u.STAGE_DIR))

    def test_failed_switch_and_failed_restore_keeps_everything(self):
        real_rename = os.rename

        def rename(src, dst):
            if src == self.u.STAGE_DIR or src == self.u.BACKUP_DIR:
                raise OSError("I/O error")  # neither the new nor the old version can be moved in
            real_rename(src, dst)

        self.u.os = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
        self.u.os.rename, self.u.os.path = rename, os.path
        path, digest = self.package("2.5.4.1")
        with self.assertRaisesRegex(self.u.UpdateError, "could not be put back either"):
            self.run_update(file=path, sha512=digest)
        record = json.loads(pathlib.Path(self.u.RESTART_PENDING).read_text())
        self.assertEqual(record["from"], "2.4.3")
        self.assertTrue(os.path.isdir(self.u.STAGE_DIR))
        self.assertEqual((pathlib.Path(self.u.BACKUP_DIR) / "version").read_text().strip(), "2.4.3")

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
        (pathlib.Path(self.u.BACKUP_DIR) / "server/NanoKVM-Server").write_text("moved first")
        path, digest = self.package("2.5.4.1")
        with self.assertRaisesRegex(self.u.UpdateError, "another update is running"):
            self.run_update(file=path, sha512=digest)
        self.assertTrue((pathlib.Path(self.u.BACKUP_DIR) / "server/NanoKVM-Server").exists())

    def test_web_update_found_after_stop_aborts_and_stays_stopped(self):
        # The web update was moving files when the stop ran.
        app = pathlib.Path(self.u.APP_DIR)
        write_script(pathlib.Path(self.u.STOP_SCRIPT),
                     f"#!/bin/sh\necho \"etc-S95 $1\" >> {self.base}/root/calls.log\n"
                     f"[ $1 = stop ] && rm -f {app}/kvm_system/kvm_system\nexit 0\n".encode())
        path, digest = self.package("2.5.4.1")
        with self.assertRaisesRegex(self.u.UpdateError, "left stopped"):
            self.run_update(file=path, sha512=digest)
        # Stopped, and not started again from the incomplete tree.
        self.assertEqual(self.calls().split(), ["etc-S95", "stop"])
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
            if path.startswith(self.u.BACKUP_DIR) or path == self.u.backup_trash():  # renamed away first
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

        self.u.migration_reboots = lambda new_init: "replaces the HDMI driver"
        self.u.started = lambda: False  # the daemons never come up in this boot
        self.u.time = types.SimpleNamespace(sleep=killed_by_reboot, monotonic=__import__("time").monotonic)
        path, digest = self.package("2.5.4.1")
        with self.assertRaises(Rebooted):
            self.run_update(file=path, sha512=digest)
        self.assertIn("restart", self.calls())
        self.assertEqual(self.installed_version(), "2.5.4.1")

    def test_driver_reboot_that_fails_first_is_reported(self):
        self.u.migration_reboots = lambda new_init: "replaces the HDMI driver"
        path, digest = self.package("2.5.4.1", self.entries("2.5.4.1", init=FAIL_SCRIPT))
        with self.assertRaisesRegex(self.u.UpdateError, "failed before rebooting"):
            self.run_update(file=path, sha512=digest)

    def test_driver_reboot_that_never_happens_is_reported(self):
        self.u.migration_reboots = lambda new_init: "replaces the HDMI driver"
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
        same = self.base / "S95-same"  # the installed init script, unchanged by the update
        same.write_bytes(b"#!/bin/sh\n")
        u.STOP_SCRIPT = str(same)
        driver.write_bytes(b"driver")
        digest = hashlib.md5(b"driver").hexdigest()
        u.CURRENT_DRIVER_MD5 = digest
        self.assertEqual(u.migration_reboots(same), "")
        u.CURRENT_DRIVER_MD5, u.LEGACY_DRIVER_MD5 = "0" * 32, digest
        self.assertEqual(u.migration_reboots(same), "")  # legacy driver is fine without hdmi_version
        hdmi.write_text("u")
        self.assertEqual(u.migration_reboots(same), "replaces the HDMI driver")  # but not for HDMI version 2
        hdmi.write_text("")
        with self.assertRaisesRegex(u.UpdateError, "is empty"):
            u.migration_reboots(same)  # new_app_init fails on an empty hdmi_version
        hdmi.unlink()
        driver.unlink()
        with self.assertRaisesRegex(u.UpdateError, "cannot read"):
            u.migration_reboots(same)  # new_app_init fails when md5sum of the driver fails

    def test_a_changed_init_script_means_a_reboot(self):
        u = load()
        driver = self.base / "driver.ko"
        driver.write_bytes(b"driver")
        u.DRIVER, u.HDMI_VERSION = str(driver), str(self.base / "none")
        u.CURRENT_DRIVER_MD5 = __import__("hashlib").md5(b"driver").hexdigest()
        installed, new = self.base / "S95-installed", self.base / "S95-new"
        installed.write_bytes(b"#!/bin/sh\n# upstream\n")
        new.write_bytes(b"#!/bin/sh\n# fork\n")
        u.STOP_SCRIPT = str(installed)
        self.assertIn("init script", u.migration_reboots(str(new)))  # first install over an upstream S95
        new.write_bytes(installed.read_bytes())
        self.assertEqual(u.migration_reboots(str(new)), "")

    def test_kvm_system_refuses_to_migrate_over_an_unfinished_update(self):
        source = (SCRIPT.parents[2] / "support/sg2002/kvm_system/main/lib/system_init/system_init.cpp").read_text()
        body = source[source.index("AppInitResult new_app_init(void)"):]
        body = body[body.index("{") + 1:]
        # The guard is the very first statement: nothing (system(), init_command(), file writes)
        # can run before it.
        first_statement = body.strip().split("\n", 1)[0].strip()
        self.assertEqual(first_statement, "if(update_switch_unfinished()){")
        guard = body.index("update_switch_unfinished()")
        for effect in ("system(", "init_command(", "fopen(", "unlink(", "write_hdmi_state("):
            if effect in body:
                self.assertLess(guard, body.index(effect), effect)
        self.assertIn('fopen("/etc/kvm/update-restart-pending", "r")', source)

    def test_kvm_system_record_parser(self):
        # The cases live in pending_record_test.cpp; scripts/fork-test.sh always runs it with g++.
        import shutil as sh
        compiler = sh.which("g++") or sh.which("c++") or sh.which("clang++")
        if not compiler:
            self.skipTest("no C++ compiler")
        probe = self.base / "probe.cpp"
        probe.write_text("int main() { return 0; }\n")
        if subprocess.run([compiler, str(probe), "-o", str(self.base / "probe")], capture_output=True).returncode != 0:
            self.skipTest("the C++ compiler here cannot link a program")
        source = pathlib.Path(__file__).resolve().parent / "pending_record_test.cpp"
        include = SCRIPT.parents[2] / "support/sg2002/kvm_system/main/lib/system_init"
        binary = self.base / "pending_record_test"
        built = subprocess.run([compiler, "-std=c++11", "-I", str(include), "-o", str(binary), str(source)],
                               capture_output=True, text=True)
        self.assertEqual(built.returncode, 0, built.stderr)
        result = subprocess.run([str(binary)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_kvm_system_reboots_when_it_replaces_the_init_script(self):
        source = (SCRIPT.parents[2] / "support/sg2002/kvm_system/main/lib/system_init/system_init.cpp").read_text()
        self.assertIn("cmp -s /kvmapp/system/init.d/S95nanokvm /etc/init.d/S95nanokvm", source)
        self.assertIn("(current_driver && !init_script_changed) ? APP_INIT_OK : APP_INIT_REBOOT", source)

    def test_unreadable_previous_version_is_refused_before_stop(self):
        (pathlib.Path(self.u.APP_DIR) / "version").write_bytes(b"\xff\xfe")
        path, digest = self.package("2.5.4.1")
        with self.assertRaisesRegex(self.u.UpdateError, "cannot read"):
            self.run_update(file=path, sha512=digest)
        self.assertEqual(self.calls(), "")

    def test_record_made_visible_before_a_failed_sync_is_taken_back(self):
        real_fsync = os.fsync
        state = {"failed": False}

        def fsync(fd):
            # The first sync after the record has appeared: the directory sync after its rename.
            if not state["failed"] and os.path.exists(self.u.RESTART_PENDING):
                state["failed"] = True
                raise OSError("I/O error")
            return real_fsync(fd)

        self.u.os = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
        self.u.os.fsync, self.u.os.path = fsync, os.path
        path, digest = self.package("2.5.4.1")
        self.assert_rejected("could not record the pending restart", file=path, sha512=digest)
        self.assertFalse(os.path.exists(self.u.RESTART_PENDING))
        self.assertTrue(self.calls().splitlines()[-1].endswith("restart"))

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
        """Serve GitHub-like release URLs that redirect, with absolute URLs, to a second server
        (GitHub sends release assets to another host)."""
        package = pathlib.Path(package_path).read_bytes()
        with tarfile.open(package_path, "r:gz") as tar:
            unpacked = sum(m.size for m in tar if m.isreg())
        manifest = json.dumps({"manifest_version": 2, "version": version, "name": f"nanokvm_{version}.tar.gz",
                               "sha512": sha512, "size_bytes": len(package) + size_error,
                               "unpacked_size_bytes": unpacked + unpacked_error}).encode()
        requested = []

        def quiet(handler):
            handler.log_message = lambda *args: None
            return handler

        class Assets(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                requested.append("assets" + path)
                bodies = {"/latest.json": manifest, "/package": package}
                if path not in bodies:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(bodies[path])))
                self.end_headers()
                self.wfile.write(bodies[path])

        assets = http.server.HTTPServer(("127.0.0.1", 0), Assets)
        threading.Thread(target=assets.serve_forever, daemon=True).start()
        self.addCleanup(assets.server_close)
        self.addCleanup(assets.shutdown)
        # "localhost" vs "127.0.0.1": a different host name, as GitHub's asset host is.
        assets_base = f"http://localhost:{assets.server_port}"

        class Releases(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                requested.append(path)
                redirects = {
                    "/rel/latest/download/latest.json": "/latest.json",
                    f"/rel/download/{version}/latest.json": "/latest.json",
                    f"/rel/download/{version}/nanokvm_{version}.tar.gz": "/package",
                }
                if path not in redirects:
                    self.send_error(404)
                    return
                self.send_response(302)
                self.send_header("Location", assets_base + redirects[path])
                self.end_headers()

        releases = http.server.HTTPServer(("127.0.0.1", 0), Releases)
        threading.Thread(target=releases.serve_forever, daemon=True).start()
        self.addCleanup(releases.server_close)
        self.addCleanup(releases.shutdown)
        self.u.RELEASES = f"http://127.0.0.1:{releases.server_port}/rel"
        return requested

    def test_downloads_latest_release(self):
        path, digest = self.package("2.5.4.1")
        requested = self.serve_releases("2.5.4.1", path, digest)
        self.assertEqual(self.run_update(dry_run=True), "2.5.4.1")
        self.assertIn("/rel/latest/download/latest.json", requested)
        self.assertIn("/rel/download/2.5.4.1/nanokvm_2.5.4.1.tar.gz", requested)
        self.assertIn("assets/package", requested)  # fetched from the other host, then verified

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

    def run_s95(self, action, mic_fails=False, picoclaw_fails=False, pidof_after_start=False, expect_status=None,
                kvm_up=None, server_up=None, real_lock=False):
        """Run the real S95nanokvm branch with everything that touches the device stubbed out,
        recording the order of the fork's boot steps and the daemon starts."""
        log = self.base / "order.log"
        text = S95.read_text()
        if not real_lock:
            text = text.replace("exec 9>/run/nanokvm-service.lock || exit 1", "exec 9>/dev/null")
            text = text.replace("flock -n 9 ||", "true ||")
        # (with real_lock, the lock is fs/run/nanokvm-service.lock, as /run/ is moved below)
        # Daemons: real processes that either stay up for a while or exit at once.
        kvm_up = pidof_after_start if kvm_up is None else kvm_up
        server_up = pidof_after_start if server_up is None else server_up
        kvm_daemon = "command sleep 4" if kvm_up else "true"
        server_daemon = "command sleep 4" if server_up else "true"
        text = text.replace("/tmp/kvm_system/kvm_system 9>&- &", f"record daemon-kvm_system; {kvm_daemon} >/dev/null 2>&1 &")
        text = text.replace("/tmp/server/NanoKVM-Server 9>&- &", f"record daemon-NanoKVM-Server; {server_daemon} >/dev/null 2>&1 &")
        text = text.replace("(sleep 15", "(command sleep 1")
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
pidof() {{ return 1; }}
killall() {{ :; }}
iptables() {{ return 0; }}
rm() {{ case "$*" in *update-restart-pending*) command rm "$@" ;; esac; }}
cp() {{ :; }}
mv() {{ :; }}
awk() {{ :; }}
sync() {{ :; }}
reboot() {{ record reboot; }}
sleep() {{ :; }}
{("stop_services() { " + self.stop_hook + "; }") if getattr(self, "stop_hook", None) else ""}
"""
        head, case = text.split('case "$1" in', 1)
        script = head + stubs + 'case "$1" in' + case
        result = subprocess.run(["sh", "-c", script, "S95nanokvm", action], capture_output=True, text=True)
        self.last_result = result
        if expect_status is None:
            expect_status = 1 if (mic_fails or picoclaw_fails) else 0
        self.assertEqual(result.returncode, expect_status, result.stderr)
        if action == "start" and expect_status in (None, 0, 1) and "not starting" not in result.stderr:
            self.assertTrue((fake / "boot/hostname.prefix").read_text().strip().endswith("kvm"))
        return log.read_text().split() if log.exists() else []

    @unittest.skipUnless(shutil.which("flock"), "needs flock(1), as on the device")
    def test_s95_refuses_while_the_updater_holds_the_service_lock(self):
        import fcntl
        lock_path = self.base / "fs/run/nanokvm-service.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # The updater holds the lock it shares with S95: the same file, taken the way
        # update-nanokvm.py takes it (fcntl.flock, which is flock(2) like flock(1)).
        held = open(lock_path, "w")
        fcntl.flock(held, fcntl.LOCK_EX)
        try:
            order = self.run_s95("restart", expect_status=1, real_lock=True)
            self.assertNotIn("daemon-NanoKVM-Server", order)  # refused before stopping or starting
            self.assertIn("already in progress", self.last_result.stderr)
        finally:
            held.close()
        order = self.run_s95("restart", real_lock=True)
        self.assertIn("daemon-NanoKVM-Server", order)

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
        installed = '{"from":"2.4.3","to":"2.5.4.1","phase":"installed"}'
        cases = (
            (installed, "2.5.4.1", True, True),       # Go-style JSON, both daemons stay up
            (installed.replace(":", ": "), "2.5.4.1", True, True),  # Python-style JSON
            (installed, "2.5.4.1", False, False),     # a daemon died
            (installed, "2.4.3", True, False),        # another version was started
        )
        base = self.base
        for content, started_version, daemons_up, cleared in cases:
            with self.subTest(content=content, version=started_version, daemons_up=daemons_up):
                # A fresh tree per case: a monitor left by one case cannot touch the next.
                self.base = pathlib.Path(tempfile.mkdtemp(dir=base))
                fake = self.base / "fs"
                record = fake / "etc/kvm/update-restart-pending"
                record.parent.mkdir(parents=True, exist_ok=True)
                record.write_text(content + "\n")  # both writers end the record with a newline
                (fake / "kvmapp").mkdir(parents=True, exist_ok=True)
                (fake / "kvmapp/version").write_text(started_version + "\n")
                self.run_s95("start", pidof_after_start=daemons_up)
                __import__("time").sleep(2)  # the monitor waits 1 s, then decides; let it finish
                self.assertEqual(record.exists(), not cleared)
        self.base = base

    def test_half_way_record_stops_start_and_restart_before_anything_runs(self):
        for content in ('{"from":"2.4.3","to":"2.5.4.1","phase":"switching"}', "", "garbage",
                        '{"from":"2.4.3","to":"2.5.4.1","phase":"installed"',  # truncated
                        '{"from":"2.4.3","to":"2.5.4.1","phase":"installed"}\n{"x":1}'):
            for action in ("start", "restart"):
                with self.subTest(content=content, action=action):
                    fake = self.base / "fs"
                    record = fake / "etc/kvm/update-restart-pending"
                    record.parent.mkdir(parents=True, exist_ok=True)
                    record.write_text(content)
                    (self.base / "order.log").unlink(missing_ok=True)
                    order = self.run_s95(action, expect_status=1)
                    # Only the microphone and picoclaw are switched off; no migration, no daemons.
                    self.assertEqual(order, ["mic", "picoclaw"])
                    self.assertIn("not starting", self.last_result.stderr)
                    self.assertTrue(record.exists())

    def test_record_is_kept_when_either_daemon_dies(self):
        base = self.base
        for kvm_up, server_up in ((True, False), (False, True)):
            with self.subTest(kvm_up=kvm_up, server_up=server_up):
                self.base = pathlib.Path(tempfile.mkdtemp(dir=base))
                fake = self.base / "fs"
                record = fake / "etc/kvm/update-restart-pending"
                record.parent.mkdir(parents=True, exist_ok=True)
                record.write_text('{"from":"2.4.3","to":"2.5.4.1","phase":"installed"}\n')
                (fake / "kvmapp").mkdir(parents=True, exist_ok=True)
                (fake / "kvmapp/version").write_text("2.5.4.1\n")
                self.run_s95("start", kvm_up=kvm_up, server_up=server_up)
                __import__("time").sleep(2)
                self.assertTrue(record.exists())
        self.base = base

    def test_record_is_kept_when_a_boot_step_failed(self):
        for kw in ({"mic_fails": True}, {"picoclaw_fails": True}):
            with self.subTest(**kw):
                fake = self.base / "fs"
                record = fake / "etc/kvm/update-restart-pending"
                record.parent.mkdir(parents=True, exist_ok=True)
                record.write_text('{"from":"2.4.3","to":"2.5.4.1","phase":"installed"}\n')
                (fake / "kvmapp").mkdir(parents=True, exist_ok=True)
                (fake / "kvmapp/version").write_text("2.5.4.1\n")
                self.run_s95("start", pidof_after_start=True, **kw)
                __import__("time").sleep(2)
                self.assertTrue(record.exists())

    def test_record_changed_during_the_check_is_kept(self):
        fake = self.base / "fs"
        record = fake / "etc/kvm/update-restart-pending"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text('{"from":"2.4.3","to":"2.5.4.1","phase":"installed"}\n')
        (fake / "kvmapp").mkdir(parents=True, exist_ok=True)
        (fake / "kvmapp/version").write_text("2.5.4.1\n")
        self.run_s95("start", pidof_after_start=True)
        record.write_text('{"from":"2.5.4.1","to":"2.5.4.2","phase":"installed"}\n')  # a newer update
        __import__("time").sleep(2)
        self.assertTrue(record.exists())

    def test_restart_does_not_start_when_an_update_began_during_the_stop(self):
        fake = self.base / "fs"
        record = fake / "etc/kvm/update-restart-pending"
        record.parent.mkdir(parents=True, exist_ok=True)
        (self.base / "order.log").unlink(missing_ok=True)
        self.stop_hook = f"printf '%s\\n' '{{\"from\":\"2.4.3\",\"to\":\"2.5.4.1\",\"phase\":\"switching\"}}' > {record}"
        order = self.run_s95("restart", expect_status=1)
        self.assertNotIn("daemon-NanoKVM-Server", order)
        self.assertNotIn("daemon-kvm_system", order)

    def test_crlf_version_file_still_clears_the_record(self):
        fake = self.base / "fs"
        record = fake / "etc/kvm/update-restart-pending"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text('{"from":"2.4.3","to":"2.5.4.1","phase":"installed"}\n')
        (fake / "kvmapp").mkdir(parents=True, exist_ok=True)
        (fake / "kvmapp/version").write_bytes(b"2.5.4.1\r\n")
        self.run_s95("start", pidof_after_start=True)
        __import__("time").sleep(2)
        self.assertFalse(record.exists())

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
        run = self.base / "run"
        run.mkdir(exist_ok=True)
        body = s95_function("wait_for_exit") + s95_function("disable_picoclaw")
        body = (body.replace("/etc/init.d", str(etc)).replace("/kvmapp/", f"{self.base}/kvmapp/")
                .replace("/run/", f"{run}/"))
        self.assertNotIn(" /run/", body)  # nothing outside the temp dir
        script = "pidof() { return 1; }\nmv() { return 1; }\n" + body + "\ndisable_picoclaw\n"
        result = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Failed to disable", result.stderr)
        self.assertTrue((run / "nanokvm-picoclaw").read_text().startswith("FAILED"))

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
