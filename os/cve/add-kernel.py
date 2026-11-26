#!/usr/bin/env python3
"""akkijp fork: add the Linux kernel to a Buildroot CycloneDX SBOM.

The LicheeRV-Nano-Build SDK builds the kernel (linux_5.10) outside Buildroot, so the SBOM of the
Buildroot configuration does not name it; cve-check matches components by CPE, name and version.

Usage: add-kernel.py <sbom.json> <kernel-version>   (edits the file in place)
"""
import json
import sys


def main() -> int:
    path, version = sys.argv[1], sys.argv[2]
    with open(path) as f:
        sbom = json.load(f)
    components = sbom.setdefault("components", [])
    if not any(c.get("name") == "linux" for c in components):
        components.append({
            "bom-ref": "linux",
            "type": "operating-system",
            "name": "linux",
            "version": version,
            "cpe": f"cpe:2.3:o:linux:linux_kernel:{version}:*:*:*:*:*:*:*",
            "properties": [{"name": "BR_TYPE", "value": "target"},
                           {"name": "AKKIJP_SOURCE", "value": "LicheeRV-Nano-Build linux_5.10 (outside Buildroot)"}],
        })
    with open(path, "w") as f:
        json.dump(sbom, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
