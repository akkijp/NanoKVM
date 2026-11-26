#!/usr/bin/env python3
"""akkijp fork: the CVEs new since the previous report.

Usage: diff.py <summary.json> [<previous summary.json>] <triage-max> <triage.json>
  stdout: {"new": {tree: [cve...]}}  (every new one)
  <triage.json>: the new ones to hand to Codex, each named once even if several trees have it,
          highest CVSS first: up to 60% of triage-max outside the kernel and the rest kernel ones
          (the kernel has thousands and would otherwise fill the list), short fields only
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
    limit, triage_path = int(args[-2]), args[-1]
    previous_path = args[1] if len(args) == 4 else ""
    previous = json.load(open(previous_path)) if previous_path else {"trees": {}}
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
    ranked = sorted(seen.values(), key=lambda c: -(c["score"] or 0))
    userland = [c for c in ranked if c["package"] != "linux"]
    kernel = [c for c in ranked if c["package"] == "linux"]
    take_user = min(len(userland), max(limit - len(kernel), int(limit * 0.6)))
    triage = userland[:take_user] + kernel[:limit - take_user]
    short = [{k: c[k] for k in ("id", "package", "version", "score", "severity", "trees")}
             | {"description": c["description"][:300]} for c in triage]
    with open(triage_path, "w") as f:
        json.dump({"cves": short, "not_handed_over": len(ranked) - len(triage)}, f, ensure_ascii=False, indent=1)
    overall = f"new {len(seen)} CVEs ({len(userland)} outside the kernel)" + (
        f", to Codex: {len(triage)}" if len(ranked) > len(triage) else "")
    print(overall + " | " + " | ".join(lines), file=sys.stderr)
    for line in lines:
        print(line, file=sys.stderr)
    json.dump({"new": new}, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
