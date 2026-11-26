#!/bin/bash
# akkijp fork: list the known vulnerabilities (CVE) of the NanoKVM OS, on nkbuild.
#
#   os/cve/scan.sh <out-dir>
#
# For each Buildroot tree named in TREES (the image on the device and the 2025.02 rebuild), in the
# pinned builder (os/Dockerfile):
#   make show-info | utils/generate-cyclonedx      -> SBOM of the configuration (no build needed)
#   + the Linux kernel, which the SDK builds outside Buildroot (KERNEL_VERSION)
#   support/scripts/cve-check --nvd-path NVD       -> the SBOM with the CVEs that affect it
# The NVD data is the git mirror Buildroot uses (fkie-cad/nvd-json-data-feeds), pulled first.
# Results: <out-dir>/<tree>.sbom-cve.json and <out-dir>/summary.json (per tree: package, CVE).
set -euo pipefail
OUT="$(mkdir -p "$1" && cd "$1" && pwd)"
OS="${OS_DIR:-$HOME/os}"
NVD="$OS/nvd"
IMAGE=nanokvm-os-builder
KERNEL_VERSION="${KERNEL_VERSION:-5.10.4}"
# name=path of a LicheeRV-Nano-Build buildroot tree
TREES="${TREES:-device=$OS/sdk/buildroot rebuild=$OS/sdk-br2025/buildroot}"
HERE="$(cd "$(dirname "$0")" && pwd)"
# generate-cyclonedx and cve-check come from stock Buildroot 2025.02 (2023.11 has neither); they
# read the show-info of either tree.
tool="${CVE_TOOL:-$OS/br2025/buildroot-2025.02.20}"

mkdir -p "$NVD"
for entry in $TREES; do
    name="${entry%%=*}"; tree="${entry#*=}"
    [ -d "$tree" ] || { echo "[skip] $name: $tree missing"; continue; }
    echo "== $name ($tree)"
    docker run --rm -v "$tree:/br" -v "$tool:/tool:ro" -v "$NVD:/nvd" -v "$OUT:/out" -v "$HERE:/cve:ro" \
        -w /br "$IMAGE" bash -euc "
        cp -a /br /tmp/br && cd /tmp/br
        make -s cvitek_SG200X_musl_riscv64_defconfig >/dev/null
        make -s show-info > /tmp/show-info.json
        /tool/utils/generate-cyclonedx < /tmp/show-info.json > /tmp/sbom.json
        python3 /cve/add-kernel.py /tmp/sbom.json $KERNEL_VERSION
        /tool/support/scripts/cve-check --nvd-path /nvd < /tmp/sbom.json > /out/$name.sbom-cve.json
    "
done
python3 "$HERE/summarize.py" "$OUT"
