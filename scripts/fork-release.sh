#!/bin/bash
# akkijp fork: build release assets from exactly the commit a release tag names.
#
#   1. Resolve the annotated tag on origin and check it is on akki/<upstream>
#      (scripts/verify-release-tag.sh). Its commit is the source.
#   2. Check that commit out into a new temporary worktree (the normal checkout
#      and its files are not touched) and build it there: builder image, web in
#      the pinned node container, package.
#   3. Check the assets, resolve the tag again with the same commit (catches a
#      tag moved during the build), and only then copy the assets to
#      ~/releases/<version>/.
#
# It does not publish. Upload ~/releases/<v>/{nanokvm_<v>.tar.gz,latest.json,
# sha256.txt} to the GitHub release <v> afterwards.
#
# Usage (on nkbuild, from any checkout of the fork): scripts/fork-release.sh 2.5.4.1
set -euo pipefail

VERSION="${1:-}"
if ! echo "$VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "Usage: $0 <version, e.g. 2.5.4.1>" >&2
    exit 1
fi

REPO="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
OUT="$HOME/releases/$VERSION"
mkdir -p "$HOME/releases"
# One build per version at a time.
exec 8>"$HOME/releases/.lock-$VERSION"
if ! flock -n 8; then
    echo "[ERROR] another release build of $VERSION is running" >&2
    exit 1
fi
if [ -e "$OUT" ]; then
    echo "[ERROR] $OUT already exists; move it away first" >&2
    exit 1
fi

SHA="$(cd "$REPO" && scripts/verify-release-tag.sh "$VERSION")"
echo "== source $VERSION = $SHA"
# Fetch only that commit; local tags are left as they are.
git -C "$REPO" fetch -q --no-tags origin "$SHA"

TREE="$(mktemp -d)/NanoKVM"
# Set by this run only (never inherited from the caller's environment).
IMAGE=""
STAGE=""
cleanup() {
    local status=$?
    set +e
    [ -n "$IMAGE" ] && docker image rm -f "$IMAGE" >/dev/null 2>&1
    [ -n "$STAGE" ] && [ -d "$STAGE" ] && rm -rf "$STAGE"  # a partial copy; the complete set was renamed away
    git -C "$REPO" worktree remove --force "$TREE" >/dev/null 2>&1
    rm -rf "$(dirname "$TREE")"
    exit "$status"
}
trap cleanup EXIT
git -C "$REPO" worktree add -q --detach "$TREE" "$SHA"
cd "$TREE"

# A builder image of its own, so a parallel release build cannot retag the one this uses.
IMAGE="nanokvm-builder-release-$VERSION-$$"
make release VERSION="$VERSION" DOCKER_TTY= IMAGE_NAME="$IMAGE"

scripts/verify-release-assets.sh build/release "$VERSION"
if [ "$(git rev-parse HEAD)" != "$SHA" ] || [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "[ERROR] the worktree changed during the build" >&2
    exit 1
fi
(cd "$REPO" && scripts/verify-release-tag.sh "$VERSION" "$SHA" > /dev/null)

# Assemble next to the destination, then put the complete set in place with one rename.
STAGE="$(mktemp -d "$HOME/releases/.stage-$VERSION-XXXXXX")"
cp build/release/"nanokvm_$VERSION.tar.gz" build/release/latest.json build/release/sha256.txt "$STAGE/"
echo "$SHA" > "$STAGE/SOURCE_COMMIT"
scripts/verify-release-assets.sh "$STAGE" "$VERSION"
mv -T "$STAGE" "$OUT"
echo "== DONE $VERSION from $SHA (not published): $OUT"
ls -la "$OUT"
