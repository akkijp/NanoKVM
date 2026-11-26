#!/usr/bin/env python3
"""akkijp fork: the CVEs new since the previous report.

Usage: diff.py <summary.json> [<previous summary.json>] <triage-max>
  stdout: {"new": {tree: [cve...]}, "triage": [cve...]}  (triage: the new ones, highest CVSS first,
          at most triage-max, each named once even if several trees have it)
  stderr: one line of counts per tree, the first line being the overall one (for the notification)
Without a previous summary every open CVE counts as new (the first run).
"""
import json
import sys

NOT_COUNTED = {"resolved", "resolved_with_pedigree", "false_positive", "not_affected"}


def open_ids(tree):
    return {(c["id"], c["package"]) for c in tree["cves"] if c["state"] not in NOT_COUNTED}


def main() -> int:
    args = sys.argv[1:]
    current = json.load(open(args[0]))
    previous = json.load(open(args[1])) if len(args) == 3 and args[1] else {"trees": {}}
    limit = int(args[-1])
    new, lines, seen = {}, [], {}
    for name, tree in current["trees"].items():
        before = open_ids(previous["trees"].get(name, {"cves": []}))
        fresh = [c for c in tree["cves"] if c["state"] not in NOT_COUNTED and (c["id"], c["package"]) not in before]
        new[name] = fresh
        total = len(open_ids(tree))
        high = sum(1 for c in fresh if (c["score"] or 0) >= 7.0)
        lines.append(f"{name}: open {total}, new {len(fresh)} (CVSS 7.0+ {high})")
        for c in fresh:
            seen.setdefault((c["id"], c["package"]), dict(c, trees=[]))["trees"].append(name)
    triage = sorted(seen.values(), key=lambda c: -(c["score"] or 0))
    overall = f"new {len(seen)} CVEs" + (f" (to Codex: top {limit})" if len(triage) > limit else "")
    print(overall + " | " + " | ".join(lines), file=sys.stderr)
    for line in lines:
        print(line, file=sys.stderr)
    json.dump({"new": new, "triage": triage[:limit], "not_triaged": max(0, len(triage) - limit)}, sys.stdout,
              ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
