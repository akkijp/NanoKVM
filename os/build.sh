#!/bin/bash
# akkijp fork: build the NanoKVM OS image from sipeed/LicheeRV-Nano-Build in the pinned builder
# (os/Dockerfile), on nkbuild.
#
#   os/build.sh <sdk-dir> <variant>
#     <sdk-dir>  a checkout of sipeed/LicheeRV-Nano-Build with host-tools/ inside (see os/README.md)
#     <variant>  baseline   the SDK as it is (its Buildroot 2023.11.2)
#                br2025     Buildroot replaced by 2025.02.x LTS (os/buildroot-2025.02/apply.sh)
#     With NANOKVM_RELEASE=<dir with nanokvm_X.Y.Z.N.tar.gz and latest.json> the NanoKVM layer
#     (os/nanokvm/apply.sh) is applied too, and the work dir is <sdk-dir>-<variant>-nanokvm.
#
# The SDK is copied to <sdk-dir>-<variant> (hard links for host-tools), so the checkout stays
# clean and variants do not mix. The log is <sdk-dir>-<variant>.log.
set -euo pipefail
SDK="$(cd "$1" && pwd)"
VARIANT="$2"
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$SDK-$VARIANT${NANOKVM_RELEASE:+-nanokvm}"
LOG="$WORK.log"
IMAGE=nanokvm-os-builder

docker build -q -t "$IMAGE" --build-arg UID="$(id -u)" --build-arg GID="$(id -g)" -f "$HERE/Dockerfile" "$HERE" >/dev/null

if [ ! -d "$WORK" ]; then
    rsync -a --exclude /host-tools "$SDK/" "$WORK/"
    cp -al "$SDK/host-tools" "$WORK/host-tools"
fi
case "$VARIANT" in
    baseline) ;;
    br2025) "$HERE/buildroot-2025.02/apply.sh" "$WORK" ;;
    *) echo "unknown variant $VARIANT" >&2; exit 2 ;;
esac
if [ -n "${NANOKVM_RELEASE:-}" ]; then
    pkg="$(ls "$NANOKVM_RELEASE"/nanokvm_*.tar.gz)"
    sha="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["sha512"])' "$NANOKVM_RELEASE/latest.json")"
    "$HERE/nanokvm/apply.sh" "$WORK" "$pkg" "$sha"
fi

echo "== building $VARIANT in $WORK (log: $LOG)"
docker run --rm -v "$WORK:/work" -w /work "$IMAGE" bash -c '
    set -e
    source build/cvisetup.sh
    defconfig sg2002_licheervnano_sd
    build_all || build_all   # upstream: qt5 sometimes fails on the first pass
' > "$LOG" 2>&1
ls -la "$WORK"/install/soc_*/images/*.img 2>/dev/null || { echo "no image; see $LOG" >&2; exit 1; }
