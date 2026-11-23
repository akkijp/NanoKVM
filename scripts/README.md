# Release packaging

These scripts assemble the application update package that NanoKVM devices pull
over the air: `nanokvm_<version>.tar.gz` plus its `latest.json` manifest.

| Script | Purpose |
|---|---|
| `build-in-container.sh` | Builds every riscv64 artifact (`kvm_system`, `libkvm.so`, `NanoKVM-Server`). Runs inside the `nanokvm-builder` image only. |
| `package.sh` | Stages the package tree, creates the tarball, and writes `latest.json`. |
| `compare-release.sh` | Diffs a freshly built package against the currently published one. Informational. |
| `verify-release-assets.sh` | Checks the three GitHub Release assets before publishing or promotion. |
| `verify-release-tag.sh` | Requires an annotated `X.Y.Z.N` tag whose commit is on the fork branch for its upstream version (`2.5.4.1` must be on `akki/2.5.4`; see [FORK.md](../FORK.md)). |

## What the updater expects

The format is not arbitrary — it is fixed by the on-device updater in
`server/service/application/`:

- **One safe root directory.** The updater scans before extracting and accepts
  only directories and regular files under `nanokvm_<version>/`; links,
  special files, duplicate paths, and path traversal are rejected.
- **`name` is the file name.** In the akkijp fork, `version.go` reads
  `latest.json` from `https://github.com/akkijp/NanoKVM/releases/latest/download`
  and downloads the package from
  `https://github.com/akkijp/NanoKVM/releases/download/<version>/<name>`.
- **`sha512` is base64, not hex.** `update.go` compares against
  `base64(raw sha512 digest)`. A hex digest will fail verification on device.
- **`/kvmapp/version`** is what the device reports as its installed version, so
  it must match the `version` field.

New releases use manifest v2. `size` remains the exact compressed byte count
for older clients, while v2 clients use `size_bytes` and
`unpacked_size_bytes` for storage preflight checks. Historical v1 `size`
values have inconsistent units, so v2 clients only require v1 `size` to be
non-zero and verify the downloaded SHA-512 instead. Publish v2 metadata before
releasing clients that consume it; clients retain v1 compatibility for custom
update servers.

## Building a release

The frontend is built in a pinned node container (`make web`); everything riscv64 needs the
builder image (see `docker/Dockerfile`).

```bash
make release VERSION=2.4.4
```

That is equivalent to:

```bash
make release-build         # kvm_system + libkvm.so + NanoKVM-Server, in Docker
make web                   # web/dist
make package VERSION=2.4.4 # build/release/{nanokvm_2.4.4.tar.gz,latest.json}
```

In the akkijp fork there are no Actions workflows and nothing is uploaded to the
upstream CDN. Releases are built with `scripts/fork-release.sh <version>` on the
build VM and published to the fork's GitHub Releases with three assets:

- `nanokvm_<version>.tar.gz`
- `latest.json`
- `sha256.txt`

Devices read `latest.json` from the latest GitHub release. The full procedure
is in [FORK.md](../FORK.md).

## Where each file in the package comes from

| Package path | Source |
|---|---|
| `version` | the requested version |
| `server/NanoKVM-Server` | `server/build.sh` (BoringCrypto, RPATH `$ORIGIN/dl_lib`) |
| `server/dl_lib/` | whatever the `kvm_vision` build emits, with any library it does not emit backfilled from the tracked `server/dl_lib/` |
| `server/web/` | `web/dist/` |
| `kvm_system/kvm_system` | `support/sg2002` `build kvm_system` |
| `system/tool/` | prebuilt binaries in `tools/nanokvm_update_edid/` |
| `kvm/` | default runtime state (input dimensions, fps, quality) |
| everything else | tracked `kvmapp/` |

Note that `kvm/` ships default runtime state, and `install.go` replaces
`/kvmapp` wholesale, so an update resets those values on the device. That is
long-standing behaviour, not something these scripts introduce.

## Video library linking

`make app` and `server/build.sh` compile the server using the prebuilt libraries
in `server/dl_lib/`. They do not rebuild the video libraries. The cgo directive
in `server/common/kvm_vision.go` links against `libkvm.so`, which depends on
`libkvm_mmf.so`.

`make vision` rebuilds the video libraries and stages them in
`kvmapp/server/dl_lib/`; `make release-build` does this as part of the full build.
After changing library interfaces, update both prebuilt files in
`server/dl_lib/` from the same build. The tracked directory also supplies the
SDK runtime libraries used by the video build and backfills libraries that
are absent from the staged directory.

`build-in-container.sh` checks that the staged `libkvm.so` exports every KVM
symbol imported by the server. See the [server instructions](../server/README.md#compile--deploy)
for manual compilation and deployment.
