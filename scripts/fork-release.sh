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
if [ -e "$OUT" ]; then
    echo "[ERROR] $OUT already exists; move it away first" >&2
    exit 1
fi

git -C "$REPO" fetch -q origin --tags --force
SHA="$(cd "$REPO" && scripts/verify-release-tag.sh "$VERSION")"
echo "== source $VERSION = $SHA"

TREE="$(mktemp -d)/NanoKVM"
cleanup() {
    git -C "$REPO" worktree remove --force "$TREE" 2>/dev/null || true
    rm -rf "$(dirname "$TREE")"
}
trap cleanup EXIT
git -C "$REPO" worktree add -q --detach "$TREE" "$SHA"
cd "$TREE"
. scripts/fork-env.sh

make release-build DOCKER_TTY=
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$TREE/web:/w" -w /w "$NODE_IMAGE" bash -euc \
    'mkdir -p /tmp/bin && corepack enable --install-directory /tmp/bin && export PATH=/tmp/bin:$PATH && pnpm install --frozen-lockfile && pnpm build'
make package VERSION="$VERSION"

scripts/verify-release-assets.sh build/release "$VERSION"
if [ "$(git rev-parse HEAD)" != "$SHA" ] || [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "[ERROR] the worktree changed during the build" >&2
    exit 1
fi
(cd "$REPO" && scripts/verify-release-tag.sh "$VERSION" "$SHA" > /dev/null)

mkdir -p "$OUT"
cp build/release/"nanokvm_$VERSION.tar.gz" build/release/latest.json build/release/sha256.txt "$OUT/"
echo "$SHA" > "$OUT/SOURCE_COMMIT"
echo "== DONE $VERSION from $SHA (not published): $OUT"
ls -la "$OUT"
