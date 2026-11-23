#!/bin/bash
# Verify the three assets that make up a NanoKVM GitHub release.

set -euo pipefail

ASSET_DIR="${1:-}"
VERSION="${2:-}"

if [ -z "$ASSET_DIR" ] || [ -z "$VERSION" ]; then
    echo "Usage: $0 <asset-directory> <version>" >&2
    exit 1
fi
if ! echo "$VERSION" | grep -qE '^[0-9]{1,9}\.[0-9]{1,9}\.[0-9]{1,9}(\.[0-9]{1,9})?$'; then
    echo "[ERROR] invalid version '$VERSION', expected MAJOR.MINOR.PATCH[.N]" >&2
    exit 1
fi

TARBALL_NAME="nanokvm_${VERSION}.tar.gz"
TARBALL="$ASSET_DIR/$TARBALL_NAME"
MANIFEST="$ASSET_DIR/latest.json"
CHECKSUM="$ASSET_DIR/sha256.txt"

for path in "$TARBALL" "$MANIFEST" "$CHECKSUM"; do
    if [ ! -f "$path" ]; then
        echo "[ERROR] missing release asset: $path" >&2
        exit 1
    fi
done

PACKAGE_ROOT="nanokvm_${VERSION}"
MAX_PACKAGE_SIZE=$((1 << 30))
MAX_UNPACKED_SIZE=$((2 << 30))
MAX_ARCHIVE_ENTRIES=100000
ENTRY_LIST=$(mktemp)
VERBOSE_LIST=$(mktemp)
trap 'rm -f "$ENTRY_LIST" "$VERBOSE_LIST"' EXIT

if ! tar -tzf "$TARBALL" > "$ENTRY_LIST"; then
    echo "[ERROR] could not list release tarball" >&2
    exit 1
fi
if ! tar -tvzf "$TARBALL" > "$VERBOSE_LIST"; then
    echo "[ERROR] could not inspect release tarball entry types" >&2
    exit 1
fi

ENTRY_COUNT=0
while IFS= read -r entry; do
    ENTRY_COUNT=$((ENTRY_COUNT + 1))
    if [ "$ENTRY_COUNT" -gt "$MAX_ARCHIVE_ENTRIES" ]; then
        echo "[ERROR] archive contains more than $MAX_ARCHIVE_ENTRIES entries" >&2
        exit 1
    fi
    case "$entry" in
        "$PACKAGE_ROOT"|"$PACKAGE_ROOT"/*) ;;
        *)
            echo "[ERROR] archive entry is outside $PACKAGE_ROOT/: $entry" >&2
            exit 1
            ;;
    esac
    case "/$entry/" in
        */../*|*/./*)
            echo "[ERROR] archive entry contains an unsafe path component: $entry" >&2
            exit 1
            ;;
    esac
done < "$ENTRY_LIST"
if [ "$ENTRY_COUNT" -eq 0 ]; then
    echo "[ERROR] release tarball is empty" >&2
    exit 1
fi

while IFS= read -r verbose_entry; do
    entry_type=${verbose_entry:0:1}
    case "$entry_type" in
        -|d) ;;
        *)
            echo "[ERROR] archive contains a link or special entry: $verbose_entry" >&2
            exit 1
            ;;
    esac
done < "$VERBOSE_LIST"

ARCHIVE_VERSION=$(tar -xOzf "$TARBALL" "$PACKAGE_ROOT/version" 2>/dev/null) || {
    echo "[ERROR] release tarball does not contain $PACKAGE_ROOT/version" >&2
    exit 1
}
if [ "$ARCHIVE_VERSION" != "$VERSION" ]; then
    echo "[ERROR] archive version '$ARCHIVE_VERSION' does not match '$VERSION'" >&2
    exit 1
fi

SHA256=$(sha256sum "$TARBALL" | cut -d' ' -f1)
if ! printf '%s  %s\n' "$SHA256" "$TARBALL_NAME" | cmp -s - "$CHECKSUM"; then
    echo "[ERROR] sha256.txt does not match $TARBALL_NAME" >&2
    exit 1
fi

# akkijp fork: read latest.json with python3 (already needed above) instead of jq, so the build
# VM needs no extra tool. Each field is checked as before; a missing or wrong field fails.
manifest_field() {
    python3 - "$MANIFEST" "$1" <<'PY'
import json, sys
path, key = sys.argv[1], sys.argv[2]
value = json.load(open(path)).get(key)
def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)
ok = {
    "version": isinstance(value, str) and len(value) > 0,
    "name": isinstance(value, str) and len(value) > 0,
    "sha512": isinstance(value, str) and len(value) > 0,
    "size": is_int(value) and value >= 0,
    "manifest_version": is_int(value) and value == 2,
    "size_bytes": is_int(value) and value > 0,
    "unpacked_size_bytes": is_int(value) and value > 0,
}[key]
if not ok:
    sys.exit(1)
print(value)
PY
}
MANIFEST_VERSION=$(manifest_field version)
MANIFEST_NAME=$(manifest_field name)
MANIFEST_SIZE=$(manifest_field size)
MANIFEST_FORMAT=$(manifest_field manifest_version)
MANIFEST_SIZE_BYTES=$(manifest_field size_bytes)
MANIFEST_UNPACKED_SIZE_BYTES=$(manifest_field unpacked_size_bytes)
MANIFEST_SHA512=$(manifest_field sha512)

if [ "$MANIFEST_VERSION" != "$VERSION" ]; then
    echo "[ERROR] latest.json version '$MANIFEST_VERSION' does not match '$VERSION'" >&2
    exit 1
fi
if [ "$MANIFEST_NAME" != "$TARBALL_NAME" ]; then
    echo "[ERROR] latest.json name '$MANIFEST_NAME' does not match '$TARBALL_NAME'" >&2
    exit 1
fi

ACTUAL_SIZE=$(wc -c < "$TARBALL" | tr -d ' ')
if [ "$MANIFEST_FORMAT" != "2" ]; then
    echo "[ERROR] latest.json manifest_version must be 2" >&2
    exit 1
fi
if [ "$MANIFEST_SIZE" != "$ACTUAL_SIZE" ] || [ "$MANIFEST_SIZE_BYTES" != "$ACTUAL_SIZE" ]; then
    echo "[ERROR] latest.json size '$MANIFEST_SIZE' does not match '$ACTUAL_SIZE'" >&2
    exit 1
fi
if [ "$ACTUAL_SIZE" -gt "$MAX_PACKAGE_SIZE" ]; then
    echo "[ERROR] release tarball exceeds device limit of $MAX_PACKAGE_SIZE bytes" >&2
    exit 1
fi

ACTUAL_UNPACKED_SIZE=$(python3 - "$TARBALL" <<'PY'
import sys
import tarfile

total = 0
with tarfile.open(sys.argv[1], "r:gz") as archive:
    for member in archive:
        if member.isfile():
            total += member.size
print(total)
PY
)
if [ "$MANIFEST_UNPACKED_SIZE_BYTES" != "$ACTUAL_UNPACKED_SIZE" ]; then
    echo "[ERROR] latest.json unpacked_size_bytes '$MANIFEST_UNPACKED_SIZE_BYTES' does not match '$ACTUAL_UNPACKED_SIZE'" >&2
    exit 1
fi
if [ "$ACTUAL_UNPACKED_SIZE" -gt "$MAX_UNPACKED_SIZE" ]; then
    echo "[ERROR] unpacked package exceeds device limit of $MAX_UNPACKED_SIZE bytes" >&2
    exit 1
fi

ACTUAL_SHA512=$(openssl dgst -sha512 -binary "$TARBALL" | openssl base64 -A)
if [ "$MANIFEST_SHA512" != "$ACTUAL_SHA512" ]; then
    echo "[ERROR] latest.json sha512 does not match $TARBALL_NAME" >&2
    exit 1
fi

# akkijp fork: the same layout check the device runs before installing.
if ! python3 "$(dirname "$0")/verify-package-layout.py" "$TARBALL"; then
    exit 1
fi

echo "[OK] verified NanoKVM release assets for $VERSION"
