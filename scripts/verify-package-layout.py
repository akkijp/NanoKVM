#!/usr/bin/env python3
"""akkijp fork: check a release tarball with the device's own layout check.

Runs the same checks kvmapp/system/update-nanokvm.py applies before installing:
safe paths, regular files and directories only, one nanokvm_<version>/ root,
the version file, and every required file (including kvm_new_app and the boot
files the first start copies). Server-side Go applies the same list
(server/service/application/archive.go requiredPackageFiles).

Usage: scripts/verify-package-layout.py <nanokvm_<version>.tar.gz>
"""
import importlib.util
import pathlib
import sys
import tempfile

UPDATER = pathlib.Path(__file__).resolve().parents[1] / "kvmapp/system/update-nanokvm.py"


def main() -> int:
    # Loading the updater must not leave kvmapp/system/__pycache__ behind: package.sh copies
    # all of kvmapp/ into the release.
    sys.dont_write_bytecode = True
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    spec = importlib.util.spec_from_file_location("update_nanokvm", UPDATER)
    updater = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(updater)

    package = sys.argv[1]
    with tempfile.TemporaryDirectory() as work:
        # The staged copy is not written here; check space against the work directory only.
        updater.STAGE_DIR = str(pathlib.Path(work) / "stage")
        # The device keeps a free-space reserve when installing; here only the unpacked size counts.
        updater.MIN_FREE_RESERVE = 0
        updater.FREE_RESERVE_PERCENT = 0
        try:
            version = updater.version_from_name(package)
            updater.extract(package, version, work)
        except updater.UpdateError as e:
            print(f"[ERROR] {package}: {e}", file=sys.stderr)
            return 1
    print(f"[OK] {package} has the layout the device requires")
    return 0


if __name__ == "__main__":
    sys.exit(main())
