#!/bin/bash
# akkijp fork: the CVE watch of the NanoKVM OS, run on the Mac (launchd, weekly).
#
#   os/cve/watch.sh [--no-triage]
#
# 1. On nkbuild: update the fork checkout and run os/cve/scan.sh (NVD mirror, SBOM, cve-check).
# 2. Bring summary.json back to $REPORTS/<date>/ and compare with the last report: the CVEs that
#    are new (per tree: "device" = the image on the device, "rebuild" = the 2025.02 rebuild).
# 3. Hand the new ones to Codex (read-only) with os/cve/triage-prompt.md; its answer is
#    $REPORTS/<date>/triage.md. At most TRIAGE_MAX of them, highest CVSS first; the rest are
#    listed by count.
# 4. A macOS notification with the counts.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPORTS="${REPORTS:-$HOME/nanokvm-cve}"  # outside ~/Documents: launchd jobs may not read it (TCC)
TRIAGE_MAX="${TRIAGE_MAX:-80}"
CODEX_MODEL="${CODEX_MODEL:-gpt-6.2-astra}"
DATE="$(date +%Y-%m-%d)"
DIR="$REPORTS/$DATE"
mkdir -p "$DIR"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

# nkbuild keeps its own clone for this (~/os/fork; ~/NanoKVM is the app's test checkout).
ssh -o ConnectTimeout=30 nkbuild "set -e; [ -d ~/os/fork ] || git clone -q https://github.com/akkijp/NanoKVM ~/os/fork; \
    cd ~/os/fork && git fetch -q origin && git checkout -q --detach origin/akki/os \
    && os/cve/scan.sh ~/os/cve-out/$DATE" > "$DIR/scan.log" 2>&1 \
    || { osascript -e 'display notification "nkbuild に入れず、見張りを回せませんでした" with title "NanoKVM CVE の見張り"' 2>/dev/null; exit 1; }
scp -q "nkbuild:os/cve-out/$DATE/summary.json" "$DIR/summary.json"

PREVIOUS="$(ls -1d "$REPORTS"/20??-??-?? 2>/dev/null | grep -v "/$DATE\$" | tail -1 || true)"
python3 "$HERE/diff.py" "$DIR/summary.json" "${PREVIOUS:+$PREVIOUS/summary.json}" "$TRIAGE_MAX" \
    > "$DIR/new.json" 2> "$DIR/counts.txt"
cat "$DIR/counts.txt"

if [ "${1:-}" != "--no-triage" ] && [ "$(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))["triage"]))' "$DIR/new.json")" -gt 0 ]; then
    codex exec -s read-only -m "$CODEX_MODEL" -o "$DIR/triage.md" \
        "$(cat "$HERE/triage-prompt.md")

## 入力（新しく出てきた CVE）
$(cat "$DIR/new.json")" < /dev/null > "$DIR/triage.log" 2>&1 || echo "codex failed; see $DIR/triage.log" >&2
fi

osascript -e "display notification \"$(head -1 "$DIR/counts.txt")\" with title \"NanoKVM CVE の見張り\"" 2>/dev/null || true
echo "report: $DIR"
