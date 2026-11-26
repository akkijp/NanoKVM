#!/usr/bin/env python3
"""akkijp fork: reduce the SBOMs with vulnerabilities (cve-check output) to one list.

Usage: summarize.py <dir>
  reads <dir>/<tree>.sbom-cve.json, writes <dir>/summary.json:
    {"generated": ..., "trees": {<tree>: {"packages": {name: version},
                                          "cves": [{"id", "package", "version", "score",
                                                    "severity", "state", "published"}]}}}
  and prints the counts per tree and severity. Vulnerabilities Buildroot marks as not affecting
  the package (patched or ignored in the package, state "resolved"/"false_positive"/
  "not_affected") are kept with their state but not counted.
"""
import collections
import datetime
import glob
import json
import os
import sys

NOT_COUNTED = {"resolved", "resolved_with_pedigree", "false_positive", "not_affected"}


def best_rating(vuln):
    best = None
    for rating in vuln.get("ratings", []):
        score = rating.get("score")
        if score is None:
            continue
        if best is None or score > best["score"]:
            best = {"score": score, "severity": rating.get("severity", "")}
    return best or {"score": None, "severity": "unknown"}


def summarize(path):
    with open(path) as f:
        sbom = json.load(f)
    refs = {c.get("bom-ref"): c for c in sbom.get("components", [])}
    packages = {c["name"]: c.get("version", "") for c in sbom.get("components", [])
                if any(p.get("name") == "BR_TYPE" and p.get("value") == "target" for p in c.get("properties", []))}
    cves = []
    for vuln in sbom.get("vulnerabilities", []):
        rating = best_rating(vuln)
        state = (vuln.get("analysis") or {}).get("state", "affected")
        for affect in vuln.get("affects", []):
            comp = refs.get(affect.get("ref"), {})
            cves.append({
                "id": vuln.get("id"),
                "package": comp.get("name", affect.get("ref")),
                "version": comp.get("version", ""),
                "score": rating["score"],
                "severity": (rating["severity"] or "unknown").lower(),
                "state": state,
                "published": vuln.get("published", ""),
                "description": (vuln.get("description") or "")[:400],
            })
    return {"packages": packages, "cves": cves}


def main() -> int:
    out = sys.argv[1]
    result = {"generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "trees": {}}
    for path in sorted(glob.glob(os.path.join(out, "*.sbom-cve.json"))):
        tree = os.path.basename(path)[: -len(".sbom-cve.json")]
        result["trees"][tree] = summarize(path)
    with open(os.path.join(out, "summary.json"), "w") as f:
        json.dump(result, f, indent=1, ensure_ascii=False)
    for tree, data in result["trees"].items():
        counted = [c for c in data["cves"] if c["state"] not in NOT_COUNTED]
        by_sev = collections.Counter(c["severity"] for c in counted)
        kernel = sum(1 for c in counted if c["package"] == "linux")
        print(f"{tree}: {len(data['packages'])} packages, {len(counted)} open CVEs "
              f"({kernel} kernel) " + ", ".join(f"{k} {v}" for k, v in sorted(by_sev.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
