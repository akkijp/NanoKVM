"""Tests for kvmapp/system/update-nanokvm.py. Run: python3 -m unittest discover scripts/tests"""
import base64
import hashlib
import importlib.util
import io
import os
import pathlib
import tarfile
import tempfile
import types
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "kvmapp/system/update-nanokvm.py"


def load():
    spec = importlib.util.spec_from_file_location("update_nanokvm", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UpdateScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.u = load()
        self.u.APP_DIR = str(self.base / "kvmapp")
        self.u.BACKUP_DIR = str(self.base / "old")
        self.u.CACHE_DIR = str(self.base / "cache")
        self.u.SERVICE = "true"
        os.mkdir(self.u.APP_DIR)
        (self.base / "kvmapp/version").write_text("2.4.3\n")

    def tearDown(self):
        self.tmp.cleanup()

    def package(self, version, entries=None, name=None):
        root = f"nanokvm_{version}"
        if entries is None:
            entries = [(root, None), (f"{root}/version", f"{version}\n".encode())]
            entries += [(f"{root}/{f}", b"x") for f in self.u.REQUIRED_FILES if f != "version"]
        path = self.base / (name or f"{root}.tar.gz")
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
                tar.addfile(info, io.BytesIO(data) if data else None)
        digest = hashlib.sha512(path.read_bytes()).digest()
        return str(path), base64.b64encode(digest).decode()

    def run_update(self, **kw):
        args = types.SimpleNamespace(file=None, sha512=None, version=None, dry_run=False)
        args.__dict__.update(kw)
        return self.u.run(args)

    def installed_version(self):
        return (self.base / "kvmapp/version").read_text().strip()

    def test_installs_four_part_version_and_keeps_backup(self):
        path, digest = self.package("2.5.4.1")
        self.assertEqual(self.run_update(file=path, sha512=digest), "2.5.4.1")
        self.assertEqual(self.installed_version(), "2.5.4.1")
        self.assertEqual((self.base / "old/version").read_text().strip(), "2.4.3")

    def test_accepts_hex_digest(self):
        path, digest = self.package("2.5.4.1")
        hexdigest = base64.b64decode(digest).hex()
        self.run_update(file=path, sha512=hexdigest)
        self.assertEqual(self.installed_version(), "2.5.4.1")

    def test_dry_run_does_not_install(self):
        path, digest = self.package("2.5.4.1")
        self.run_update(file=path, sha512=digest, dry_run=True)
        self.assertEqual(self.installed_version(), "2.4.3")

    def assert_rejected(self, message, **kw):
        with self.assertRaisesRegex(self.u.UpdateError, message):
            self.run_update(**kw)
        self.assertEqual(self.installed_version(), "2.4.3")

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
        root = "nanokvm_2.5.4.1"
        entries = [(root, None), (f"{root}/version", b"9.9.9\n")]
        entries += [(f"{root}/{f}", b"x") for f in self.u.REQUIRED_FILES if f != "version"]
        path, digest = self.package("2.5.4.1", entries)
        self.assert_rejected("version file", file=path, sha512=digest)

    def test_rejects_missing_required_file(self):
        root = "nanokvm_2.5.4.1"
        path, digest = self.package("2.5.4.1", [(root, None), (f"{root}/version", b"2.5.4.1\n")])
        self.assert_rejected("missing", file=path, sha512=digest)

    def test_rejects_bad_file_name(self):
        path, digest = self.package("2.5.4.1", name="other.tar.gz")
        self.assert_rejected("must be named", file=path, sha512=digest)

    def test_downloads_only_from_fork(self):
        self.assertTrue(self.u.RELEASES.startswith("https://github.com/akkijp/NanoKVM/"))
        self.assertNotIn("sipeed", SCRIPT.read_text())


if __name__ == "__main__":
    unittest.main()
