#!/bin/bash
# akkijp fork: run every test this fork relies on, in throwaway containers.
#   - Go server tests (amd64). Packages that link the RISC-V libkvm.so get a
#     stub library whose functions all return 0, so they can load and run.
#   - The maintenance updater tests (python3).
#   - Web: version helper tests, type check and production build.
# The source is mounted read-only and copied inside each container, and the
# containers run as the calling user, so nothing in the checkout changes owner
# or gains build output (a later `make release` still works).
# Usage: scripts/fork-test.sh   (needs docker; run on nkbuild)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/scripts/fork-env.sh"
RUN=(docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$ROOT:/src:ro")

echo "== Go"
"${RUN[@]}" -e GOFLAGS=-mod=mod -e GOCACHE=/tmp/gocache -e GOPATH=/tmp/gopath "$GO_IMAGE" sh -euc '
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
echo "== ALL OK"
