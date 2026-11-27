#!/bin/bash
# akkijp fork: make a copy of a built NanoKVM image for one device that is replacing its current
# microSD, so the device comes back as it was:
#   - partition 3 (the USB virtual disk, exFAT) is entered in the MBR at the device's current
#     start and size; the image ends before it, so its data is not written over. /etc/kvm.disk0
#     (S01fs's marker that p3 exists) keeps the first boot from re-creating and formatting it
#   - the device's own files: boot partition (eth.nodhcp, hostname.prefix, resolv.conf, wifi.sta,
#     usb.*, gt9xx), /etc/kvm (passwords, TLS, settings), /etc/kvm.disk0, /device_key and, if
#     present, /root/.ssh/authorized_keys
#
#   os/nanokvm/personalize.sh <image.img> <carry.tar.gz> <out.img> <p3-start-sector> <p3-sectors>
# carry.tar.gz is made on the device:
#   cd / && tar -cf - etc/kvm etc/kvm.disk0 device_key root/.ssh/authorized_keys boot/eth.nodhcp \
#     boot/hostname.prefix boot/resolv.conf boot/wifi.sta boot/usb.* boot/gt9xx | gzip
# Needs root (losetup, mount). Prints the SHA-256 of the result.
set -euo pipefail
IMG="$1"; CARRY="$2"; OUT="$3"; P3_START="$4"; P3_SECTORS="$5"
cp "$IMG" "$OUT"
python3 - "$OUT" "$P3_START" "$P3_SECTORS" <<'EOF'
import struct, sys
path, start, sectors = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
with open(path, "r+b") as f:
    f.seek(446)
    table = bytearray(f.read(64))
    assert table[32:48] == bytes(16), "partition slot 3 is not empty"
    p2_start, p2_sectors = struct.unpack("<II", table[16 + 8:16 + 16])
    assert start >= p2_start + p2_sectors, "p3 would overlap p2"
    table[32:48] = bytes([0, 0xfe, 0xff, 0xff, 0x83, 0xfe, 0xff, 0xff]) + struct.pack("<II", start, sectors)
    f.seek(446)
    f.write(table)
EOF
loop="$(losetup -fP --show "$OUT")"
mnt="$(mktemp -d)"; carry="$(mktemp -d)"
cleanup() { umount "$mnt/boot" "$mnt/root" 2>/dev/null || true; losetup -d "$loop" 2>/dev/null || true; rm -rf "$mnt" "$carry"; }
trap cleanup EXIT
mkdir -p "$mnt/boot" "$mnt/root"
mount "${loop}p1" "$mnt/boot"
mount "${loop}p2" "$mnt/root"
tar -xzf "$CARRY" -C "$carry"
cp "$carry"/boot/* "$mnt/boot/"
for f in etc/kvm etc/kvm.disk0 device_key root/.ssh; do
    [ -e "$carry/$f" ] || continue
    mkdir -p "$mnt/root/$(dirname "$f")"
    cp -a "$carry/$f" "$mnt/root/$(dirname "$f")/"
    chown -R 0:0 "$mnt/root/$f"
done
[ -d "$mnt/root/root/.ssh" ] && chmod 700 "$mnt/root/root/.ssh" && chmod 600 "$mnt/root/root/.ssh/authorized_keys"
grep -q "^sshd:" "$mnt/root/etc/passwd" && [ -x "$mnt/root/usr/sbin/sshd" ] || { echo "the image has no sshd" >&2; exit 1; }
umount "$mnt/boot" "$mnt/root"
e2fsck -fn "${loop}p2" >/dev/null
fsck.vfat -n "${loop}p1" >/dev/null
losetup -d "$loop"
sha256sum "$OUT"
