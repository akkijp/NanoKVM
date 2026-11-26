#!/bin/bash
# akkijp fork: turn a LicheeRV-Nano-Build tree (generic LicheeRV Nano image) into the NanoKVM
# image, as observed in the official NanoKVM image 1.4.3 (20260610_NanoKVM_Rev1_4_3.img):
#   - memory map: ION 75 MiB (the official dtb has ion-region size 0x4b00000), boot logo 5632 KiB
#   - boot partition: usb.dev usb.disk0 usb.rndis0 wifi.sta gt9xx logo.jpeg ver (no usb.ncm)
#   - packages: without the GUI and desktop pieces the official image lacks (Qt, fonts, cursors,
#     tslib, PyQt5, babel, lftp, vsftpd, android-tools, fbv, Kerberos); with ser2net and OpenIPMI.
#     tailscale is not built in: the NanoKVM app installs the current release on demand.
#   - /etc/init.d: only the scripts of the official image (os/nanokvm/init.d.keep); the NanoKVM
#     ones come from the app
#   - /kvmapp: the fork's release package (verified by its SHA-512), with the first-start markers
#     kvm_new_app and kvm_new_img, as in the official image
#
#   os/nanokvm/apply.sh <sdk-work-dir> <nanokvm_X.Y.Z.N.tar.gz> <sha512-base64 from latest.json>
set -euo pipefail
WORK="$(cd "$1" && pwd)"
PACKAGE="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"
SHA512="$3"
HERE="$(cd "$(dirname "$0")" && pwd)"
BOARD=build/boards/sg200x/sg2002_licheervnano_sd
DEFCONFIG="$WORK/buildroot/configs/cvitek_SG200X_musl_riscv64_defconfig"
OVERLAY="$WORK/nanokvm-overlay"

got="$(openssl dgst -sha512 -binary "$PACKAGE" | base64 | tr -d '\n')"
[ -n "$SHA512" ] && [ "$got" = "$SHA512" ] || { echo "$PACKAGE: SHA-512 does not match" >&2; exit 1; }

# 1. memory map
sed -i -E 's/^(\s*ION_SIZE = ).* \* SIZE_1M/\175 * SIZE_1M/; s/^(\s*BOOTLOGO_SIZE = ).* \* SIZE_1K/\15632 * SIZE_1K/' "$WORK/$BOARD/memmap.py"
grep -q "ION_SIZE = 75 \* SIZE_1M" "$WORK/$BOARD/memmap.py" || { echo "memmap.py: ION_SIZE not set" >&2; exit 1; }

# 2. boot partition files
CFG="$WORK/build/tools/common/sd_tools/genimage_rootless.cfg"
GEN="$WORK/build/tools/common/sd_tools/sd_gen_burn_image_rootless.sh"
sed -i 's/"usb\.ncm"/"usb.disk0"/; s/"usb\.rndis"/"usb.rndis0"/' "$CFG"
sed -i 's|touch ${output_dir}/input/usb.ncm|touch ${output_dir}/input/usb.disk0|; s|touch ${output_dir}/input/usb.rndis$|touch ${output_dir}/input/usb.rndis0|' "$GEN"
grep -q '"usb.disk0"' "$CFG" && grep -q 'input/usb.disk0' "$GEN" || { echo "genimage: usb.disk0 not set" >&2; exit 1; }

# 3. packages
while read -r option; do
    [ -n "$option" ] && sed -i "/^${option}=/d" "$DEFCONFIG"
done < <(grep -v '^\s*#' "$HERE/packages.remove" | sed '/^\s*$/d')
cat "$HERE/packages.add" | grep -v '^\s*#' | sed '/^\s*$/d' >> "$DEFCONFIG"

# 4. the overlay: /kvmapp and the app's init scripts; init scripts not kept are removed after build
rm -rf "$OVERLAY" && mkdir -p "$OVERLAY/etc/init.d" "$OVERLAY/etc/kvm"
tmp="$(mktemp -d)"
tar -xzf "$PACKAGE" -C "$tmp"
mv "$tmp"/nanokvm_*/ "$OVERLAY/kvmapp"
rmdir "$tmp"
touch "$OVERLAY/kvmapp/kvm_new_app" "$OVERLAY/kvmapp/kvm_new_img"
for s in S00kmod S01fs S03usbdev S15kvmhwd S30eth S30wifi S50avahi-daemon S50sshd S80dnsmasq S95nanokvm; do
    install -m 0755 "$OVERLAY/kvmapp/system/init.d/$s" "$OVERLAY/etc/init.d/$s"
done
install -m 0755 "$OVERLAY/kvmapp/system/update-nanokvm.py" "$OVERLAY/etc/kvm/update-nanokvm.py"
# The generic image's USB network script, under the name the official image uses.
ov="$WORK/buildroot/board/cvitek/SG200X/overlay/etc/init.d"
[ -e "$ov/S30gadget_nic" ] && [ ! -e "$ov/S30rndis" ] && mv "$ov/S30gadget_nic" "$ov/S30rndis"
install -m 0755 "$HERE/post-build.sh" "$WORK/nanokvm-post-build.sh"
cp "$HERE/init.d.keep" "$WORK/nanokvm-init.d.keep"

# Paths as Buildroot sees them: the SDK runs in a container with the work dir at another path,
# so they are relative to Buildroot's own directory ($(TOPDIR) = <work>/buildroot).
python3 - "$DEFCONFIG" '$(TOPDIR)/../nanokvm-overlay' '$(TOPDIR)/../nanokvm-post-build.sh' <<'EOF'
import re, sys
path, overlay, post = sys.argv[1:]
text = open(path).read()
def add(key, value):
    global text
    m = re.search(rf'^{key}="(.*)"$', text, re.M)
    if m:
        values = [v for v in m.group(1).split() if "nanokvm-overlay" not in v and "nanokvm-post-build" not in v]
        if value not in values:
            text = text[:m.start()] + f'{key}="{" ".join(values + [value])}"' + text[m.end():]
    else:
        text += f'\n{key}="{value}"\n'
add("BR2_ROOTFS_OVERLAY", overlay)
add("BR2_ROOTFS_POST_BUILD_SCRIPT", post)
open(path, "w").write(text)
EOF
echo "NanoKVM layer applied to $WORK (app $(cat "$OVERLAY/kvmapp/version"))"
