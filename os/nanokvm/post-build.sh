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
# OpenSSH 9.8+ penalises a source address after a session it counts as failed (PerSourcePenalties).
# On this build the pre-authentication child ends with status 255 even after a good login, which
# counts as a "crash", so the next connections from the same address hang (and behind a router
# every client shares one address). Only the crash penalty is set to 0 (a 0 s penalty never
# exceeds min:15, so it never blocks); the others (authfail, noauth, ...) keep their defaults.
# In 9.9p2 a LoginGraceTime overrun uses the crash value too; LoginGraceTime still cuts it off.
CFG="$TARGET/etc/ssh/sshd_config"
if [ -f "$CFG" ] && ! grep -q "^PerSourcePenalties" "$CFG"; then
    printf '\n# akkijp fork: see os/README.md (SSH)\nPerSourcePenalties crash:0\n' >> "$CFG"
fi

for name in $(cat "$KEEP"); do
    [ -e "$TARGET/etc/init.d/$name" ] || echo "post-build: note: /etc/init.d/$name is not in this image" >&2
done
