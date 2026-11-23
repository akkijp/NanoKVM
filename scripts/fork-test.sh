#!/bin/bash
# akkijp fork: run every test this fork relies on, in throwaway containers.
#   - Go server tests (amd64). Packages that link the RISC-V libkvm.so get a
#     stub library whose functions all return 0, so they can load and run.
#   - The maintenance updater tests (python3).
#   - Web: version helper tests, type check and production build.
# Usage: scripts/fork-test.sh   (needs docker; run on nkbuild)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GO_IMAGE="golang:1.25.0"
NODE_IMAGE="node:22-bookworm"

echo "== Go"
docker run --rm -v "$ROOT/server:/src" -w /src -e GOFLAGS=-mod=mod "$GO_IMAGE" sh -euc '
  mkdir -p /tmp/stub
  nm -D --defined-only dl_lib/libkvm.so | awk "\$2==\"T\" { print \"long \" \$3 \"(void) { return 0; }\" }" > /tmp/stub/stub.c
  gcc -shared -fPIC -o /tmp/stub/libkvm.so /tmp/stub/stub.c
  export CGO_LDFLAGS=-L/tmp/stub LD_LIBRARY_PATH=/tmp/stub
  go vet ./service/application/ ./service/picoclaw/ ./config/
  go test ./...
'

echo "== Python updater"
docker run --rm -v "$ROOT:/r:ro" -w /r python:3.12-slim python3 -m unittest discover -s scripts/tests

echo "== Web"
docker run --rm -v "$ROOT/web:/w" -w /w "$NODE_IMAGE" sh -euc '
  mkdir -p /tmp/bin && corepack enable --install-directory /tmp/bin && export PATH=/tmp/bin:$PATH
  CI=1 pnpm install --frozen-lockfile >/dev/null
  pnpm test
  pnpm exec tsc --noEmit -p .
  pnpm build >/dev/null
'
echo "== ALL OK"
