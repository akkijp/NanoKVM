# akkijp fork: container images shared by scripts/fork-test.sh and scripts/fork-release.sh,
# pinned by digest so a fresh VM and a VM with cached images build with the same tools.
# pnpm itself is pinned (version and hash) by "packageManager" in web/package.json.
NODE_IMAGE="node:22-bookworm@sha256:e13726ca604184d7a91fcacab1fc09eefd642dc28e79ac34e85540dd93b78cfb"
GO_IMAGE="golang:1.25.0@sha256:5502b0e56fca23feba76dbc5387ba59c593c02ccc2f0f7355871ea9a0852cebe"
PY_IMAGE="python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
