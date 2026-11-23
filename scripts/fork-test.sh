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
# Usage: scripts/fork-test.sh   (needs docker; run on nkbuild)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/scripts/fork-env.sh"
LOGS="$(mktemp -d)"  # per run: a root-owned leftover from an accidental sudo run cannot block it
COPY=""
IMAGE=""
cleanup() {
    status=$?
    [ -n "$IMAGE" ] && docker image rm -f "$IMAGE" >/dev/null 2>&1
    [ -n "$COPY" ] && rm -rf "$(dirname "$COPY")"
    if [ "$status" -eq 0 ]; then
        rm -rf "$LOGS"
    else
        echo "== FAILED; logs kept in $LOGS" >&2
    fi
    exit "$status"
}
trap cleanup EXIT
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
    COPY="$(mktemp -d)/NanoKVM"
    IMAGE="nanokvm-builder-forktest-$$"
    mkdir -p "$COPY"
    (cd "$ROOT" && tar --exclude=./build --exclude=./web/node_modules --exclude=./web/dist -cf - .) | tar -C "$COPY" -xf -
    (cd "$COPY" && make release VERSION=0.0.0.1 DOCKER_TTY= IMAGE_NAME="$IMAGE") > "$LOGS/release.log" 2>&1 \
        || { tail -40 "$LOGS/release.log"; exit 1; }
    "$COPY/scripts/verify-release-assets.sh" "$COPY/build/release" 0.0.0.1
fi

echo "== ALL OK"
