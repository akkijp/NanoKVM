#!/bin/bash
# akkijp fork: replace the Buildroot in a LicheeRV-Nano-Build tree (Sipeed's 2023.11.2 with their
# additions) by Buildroot 2025.02.x LTS (supported until March 2028), carrying Sipeed's additions:
#   - board/cvitek (overlay, busybox config) and configs/cvitek_SG200X_*_defconfig
#   - their packages: aic8800-sdio-firmware axp2101 cvitek-riscv64-musl-sysroot gb-test-roms
#     lcdtest mgba sg2002-codec-firmware tpudemo-sg200x xuantie-gdb, in a "Sipeed" menu
#   - freetype: fix-build-in-cvi_mmf_sdk.patch; busybox-minimal: exFAT volume id
# Not carried, as 2025.02 already has them: python-pyqt5-sip, opencv4 >= 4.9 (4.12.0), numpy on
# riscv. Not carried: the gdb 13.2 musl patch (2025.02 has gdb 14-16; the image has no gdb).
# Then os/buildroot-2025.02/defconfig.fixup is applied to the defconfig (options renamed or
# removed since 2023.11).
#
#   os/buildroot-2025.02/apply.sh <sdk-work-dir>
set -euo pipefail
WORK="$(cd "$1" && pwd)"
HERE="$(cd "$(dirname "$0")" && pwd)"
VERSION=2025.02.20
SHA256=44904ee44ac6750e06135454ed5cbcb8ea9354e6d5b7e2b2668581bd8b6ba1b6  # from buildroot-2025.02.20.tar.gz.sign
CACHE="${OS_CACHE:-$HOME/os}"
TARBALL="$CACHE/buildroot-$VERSION.tar.gz"
OLD="$WORK/buildroot-2023.11.2-sipeed"
NEW="$WORK/buildroot"
PACKAGES="aic8800-sdio-firmware axp2101 cvitek-riscv64-musl-sysroot gb-test-roms lcdtest mgba sg2002-codec-firmware tpudemo-sg200x xuantie-gdb"

[ -f "$TARBALL" ] || curl -fsSL -o "$TARBALL" "https://buildroot.org/downloads/buildroot-$VERSION.tar.gz"
echo "$SHA256  $TARBALL" | sha256sum -c - >/dev/null || { echo "buildroot-$VERSION.tar.gz: checksum mismatch" >&2; exit 1; }

if [ -f "$NEW/.akkijp-buildroot-$VERSION" ]; then
    echo "Buildroot $VERSION already in place"
    exit 0
fi
[ -d "$OLD" ] || mv "$NEW" "$OLD"
rm -rf "$NEW" && mkdir -p "$NEW"
tar -xzf "$TARBALL" -C "$NEW" --strip-components=1

cp -a "$OLD/board/cvitek" "$NEW/board/"
cp -a "$OLD"/configs/cvitek_SG200X_*_defconfig "$NEW/configs/"
# The musl defconfig in its minimal form (savedefconfig of Sipeed's, which is a full .config dump
# whose default values trip 2025.02's legacy checks), then the fixups below.
cp "$HERE/cvitek_SG200X_musl_riscv64_defconfig" "$NEW/configs/"
for p in $PACKAGES; do cp -a "$OLD/package/$p" "$NEW/package/"; done
cp -a "$OLD/package/freetype/fix-build-in-cvi_mmf_sdk.patch" "$NEW/package/freetype/"
# Our own fixes for the SDK toolchain (os/buildroot-2025.02/patches/<package>/*.patch).
for dir in "$HERE"/patches/*/; do
    pkg="$(basename "$dir")"
    cp "$dir"*.patch "$NEW/package/$pkg/"
done
sed -i 's/^# CONFIG_FEATURE_VOLUMEID_EXFAT is not set$/CONFIG_FEATURE_VOLUMEID_EXFAT=y/' "$NEW/package/busybox/busybox-minimal.config"

# The "Sipeed" menu, before the closing endmenu of package/Config.in.
python3 - "$NEW/package/Config.in" $PACKAGES <<'EOF'
import sys
path, packages = sys.argv[1], sys.argv[2:]
text = open(path).read()
menu = 'menu "Sipeed"\n' + "".join(f'\tsource "package/{p}/Config.in"\n' for p in packages) + "endmenu\n\n"
cut = text.rstrip().rfind("endmenu")
open(path, "w").write(text[:cut] + menu + text[cut:])
EOF

# Options renamed or removed since 2023.11 (each line: sed expression with a comment).
DEFCONFIG="$NEW/configs/cvitek_SG200X_musl_riscv64_defconfig"
if [ -f "$HERE/defconfig.fixup" ]; then
    grep -v '^\s*#' "$HERE/defconfig.fixup" | grep -v '^\s*$' | while read -r expr; do sed -i "$expr" "$DEFCONFIG"; done
fi

echo "$VERSION" > "$NEW/.akkijp-buildroot-$VERSION"
echo "Buildroot $VERSION in place (Sipeed's 2023.11.2 kept in $OLD)"
