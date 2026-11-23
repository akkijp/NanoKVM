#!/bin/bash
# akkijp fork: run every test this fork relies on, in throwaway containers.
#   - Go server tests (amd64). Packages that link the RISC-V libkvm.so get a
#     stub library whose functions all return 0, so they can load and run.
#   - The maintenance updater tests (python3).
#   - Web: version helper tests, type check and production build.
#   - The builder image from docker/Dockerfile (checksums of the pinned downloads, SDK build).
#   - A full release build with a four-part version, checked like a release (FORK_TEST_QUICK=1 skips it).
# The source is mounted read-only and copied inside each container, and the
# containers run as the calling user, so nothing in the checkout changes owner
# or gains build output (a later `make release` still works).
# In a git checkout, every stage runs on a temporary worktree of HEAD, so what passes is exactly
# what is committed (an uncommitted fix cannot make a broken commit pass). Outside git (a plain
# copy), the copy itself is tested and the release stage is skipped.
# Usage: scripts/fork-test.sh   (needs docker; run on nkbuild)
set -euo pipefail
CHECKOUT="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$CHECKOUT"
. "$ROOT/scripts/fork-env.sh"
LOGS="$(mktemp -d)"  # per run: a root-owned leftover from an accidental sudo run cannot block it
COPY=""
IMAGE=""
cleanup() {
    status=$?
    set +e  # finish the cleanup whatever fails in it
    [ -n "$IMAGE" ] && docker image rm -f "$IMAGE" >/dev/null 2>&1
    if [ -n "$COPY" ]; then
        git -C "$CHECKOUT" worktree remove --force "$COPY" >/dev/null 2>&1
        rm -rf "$(dirname "$COPY")"
    fi
    if [ "$status" -eq 0 ]; then
        rm -rf "$LOGS"
    else
        echo "== FAILED; logs kept in $LOGS" >&2
    fi
    exit "$status"
}
trap cleanup EXIT
if git -C "$CHECKOUT" rev-parse --verify -q HEAD >/dev/null 2>&1; then
    if [ -n "$(git -C "$CHECKOUT" status --porcelain)" ]; then
        echo "[WARN] uncommitted changes are not tested; commit them first" >&2
    fi
    COPY="$(mktemp -d)/NanoKVM"
    git -C "$CHECKOUT" worktree add -q --detach "$COPY" HEAD
    ROOT="$COPY"
    echo "== Testing $(git -C "$ROOT" rev-parse --short HEAD)"
else
    echo "[WARN] not a git checkout: testing the files as they are, without the release stage" >&2
    FORK_TEST_QUICK=1
fi
RUN=(docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$ROOT:/src:ro")

echo "== Builder image (docker/Dockerfile: pinned downloads, SDK build; cached layers are reused)"
(cd "$ROOT" && make builder-image) > "$LOGS/builder.log" 2>&1 || { tail -30 "$LOGS/builder.log"; exit 1; }

echo "== Go"
"${RUN[@]}" -e GOFLAGS=-mod=readonly -e GOCACHE=/tmp/gocache -e GOPATH=/tmp/gopath "$GO_IMAGE" sh -euc '
  cp -r /src/server /tmp/server && cp -r /src/kvmapp /tmp/kvmapp && cd /tmp/server
  mkdir -p /tmp/stub
  nm -D --defined-only dl_lib/libkvm.so | awk "\$2==\"T\" { print \"long \" \$3 \"(void) { return 0; }\" }" > /tmp/stub/stub.c
  gcc -shared -fPIC -o /tmp/stub/libkvm.so /tmp/stub/stub.c
  export CGO_LDFLAGS=-L/tmp/stub LD_LIBRARY_PATH=/tmp/stub
  go vet ./service/application/ ./service/picoclaw/ ./config/
  go test ./...
  go test -race -count=2 ./service/application/  # the update code runs restarts in the background
'

echo "== kvm_system record parser (C++)"
"${RUN[@]}" "$GO_IMAGE" sh -euc '
  g++ -std=c++11 -Wall -Werror -I /src/support/sg2002/kvm_system/main/lib/system_init -o /tmp/pending_record_test /src/scripts/tests/pending_record_test.cpp
  /tmp/pending_record_test
'

echo "== Python updater"
"${RUN[@]}" -w /src "$PY_IMAGE" python3 -B -m unittest discover -s scripts/tests

echo "== Web"
"${RUN[@]}" "$NODE_IMAGE" sh -euc '
  mkdir -p /tmp/web && cd /src/web && tar --exclude=./node_modules --exclude=./dist -cf - . | tar -C /tmp/web -xf -
  cd /tmp/web
  mkdir -p /tmp/bin && corepack enable --install-directory /tmp/bin && export PATH=/tmp/bin:$PATH
  CI=1 pnpm install --frozen-lockfile >/dev/null
  pnpm test
  pnpm exec tsc --noEmit -p .
  pnpm build >/dev/null
'
if [ "${FORK_TEST_QUICK:-}" != 1 ]; then
    # What actually ships: the RISC-V server, kvm_system and kvm_vision built in the builder image,
    # packaged with a four-part version and checked like a release. In a throwaway copy, with a
    # builder image of its own.
    echo "== Release build (set FORK_TEST_QUICK=1 to skip)"
    # The same worktree of HEAD as every stage above, with a builder image of its own.
    IMAGE="nanokvm-builder-forktest-$$"
    (cd "$COPY" && make release VERSION=0.0.0.1 DOCKER_TTY= IMAGE_NAME="$IMAGE") > "$LOGS/release.log" 2>&1 \
        || { tail -40 "$LOGS/release.log"; exit 1; }
    "$COPY/scripts/verify-release-assets.sh" "$COPY/build/release" 0.0.0.1
fi

echo "== ALL OK"
