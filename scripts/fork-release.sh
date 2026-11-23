#!/bin/bash
# akkijp fork: build release assets from exactly the commit a release tag names.
#
#   1. Resolve the annotated tag on origin and check it is on akki/<upstream>
#      (scripts/verify-release-tag.sh). Its commit is the source.
#   2. Check that commit out in a clean tree and build it (builder image, web
#      in a node container, package).
#   3. Check the assets, then resolve the tag again with the same commit, so a
#      tag moved during the build is caught before anything is published.
#
# It does not publish. Upload build/release/{nanokvm_<v>.tar.gz,latest.json,
# sha256.txt} to the GitHub release <v> afterwards.
#
# Usage (on nkbuild, in ~/NanoKVM where the Makefile expects the checkout):
#   scripts/fork-release.sh 2.5.4.1
set -euo pipefail

VERSION="${1:-}"
if [ -z "$VERSION" ]; then
    echo "Usage: $0 <version>" >&2
    exit 1
fi

# The checkout below replaces this file; run from a private copy.
if [ -z "${FORK_RELEASE_COPY:-}" ]; then
    copy="$(mktemp)"
    cp "$0" "$copy"
    FORK_RELEASE_COPY=1 exec bash "$copy" "$@"
fi

ROOT="$HOME/NanoKVM"
cd "$ROOT"
git fetch -q origin --tags --force

TOOLS="$(mktemp -d)"
trap 'rm -rf "$TOOLS"' EXIT
git show "origin/akki/${VERSION%.*}:scripts/verify-release-tag.sh" > "$TOOLS/verify-release-tag.sh"
SHA="$(bash "$TOOLS/verify-release-tag.sh" "$VERSION")"
echo "== source $VERSION = $SHA"

git checkout -q --force --detach "$SHA"
git clean -fdxq
if [ -n "$(git status --porcelain)" ] || [ "$(git rev-parse HEAD)" != "$SHA" ]; then
    echo "[ERROR] checkout is not a clean $SHA" >&2
    exit 1
fi

make release-build DOCKER_TTY=
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$ROOT/web:/w" -w /w node:22-bookworm bash -euc \
    'mkdir -p /tmp/bin && corepack enable --install-directory /tmp/bin && export PATH=/tmp/bin:$PATH && pnpm install --frozen-lockfile && pnpm build'
make package VERSION="$VERSION"

scripts/verify-release-assets.sh build/release "$VERSION"
if [ "$(git rev-parse HEAD)" != "$SHA" ] || [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "[ERROR] the checkout changed during the build" >&2
    exit 1
fi
bash "$TOOLS/verify-release-tag.sh" "$VERSION" "$SHA" > /dev/null
echo "$SHA" > build/release/SOURCE_COMMIT
echo "== DONE $VERSION from $SHA (not published)"
ls -la build/release
