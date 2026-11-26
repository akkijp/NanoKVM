#!/bin/sh
# akkijp fork: Buildroot post-build script of the NanoKVM image. $1 is the target directory.
# Keeps only the init scripts the official NanoKVM image has (nanokvm-init.d.keep, next to this
# script): packages and the generic board overlay add test, demo and desktop ones.
set -eu
TARGET="$1"
KEEP="$(dirname "$0")/nanokvm-init.d.keep"
for f in "$TARGET"/etc/init.d/*; do
    name="$(basename "$f")"
    grep -qx "$name" "$KEEP" || rm -rf "$f"
done
for name in $(cat "$KEEP"); do
    [ -e "$TARGET/etc/init.d/$name" ] || echo "post-build: note: /etc/init.d/$name is not in this image" >&2
done
