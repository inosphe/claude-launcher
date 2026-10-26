#!/usr/bin/env bash
# Sample the running claunch daemon with py-spy and print a summary.
#
# Usage: misc/profile_daemon.sh [OUT_DIR] [SECONDS]
#   OUT_DIR  where the samples go (default ~/claunch-pyspy/<timestamp>)
#   SECONDS  length of each recording (default 60)
#
# py-spy needs ptrace on a process that is not its child, so this calls sudo.
# Run it from a plain terminal: inside a Claude Code session the sandbox
# refuses sudo. --nonblocking keeps the daemon running while it is sampled.
#
# Two recordings, one after the other:
#   all.raw  every thread that is not idle
#   gil.raw  only the thread holding the GIL (who starves the event loop)
set -u

PID=$(pgrep -f -- '-m claude_launcher.daemon$' | head -1)
if [ -z "$PID" ]; then
    echo "no claunch daemon process found" >&2
    exit 1
fi
OUT=${1:-$HOME/claunch-pyspy/$(date +%Y%m%d-%H%M%S)}
DUR=${2:-60}
SPY=$(command -v py-spy) || { echo "py-spy not on PATH" >&2; exit 1; }
mkdir -p "$OUT"
echo "daemon pid $PID -> $OUT (${DUR}s per recording)"

# Thread id -> name, so the summary can label the raw thread ids.
for t in /proc/"$PID"/task/*; do
    echo "${t##*/} $(cat "$t"/comm 2>/dev/null)"
done > "$OUT/threads.txt"
grep loop_lag "$HOME/.claude-launcher/daemon/daemon.log" 2>/dev/null | tail -n 200 > "$OUT/loop_lag_before.txt"

sudo "$SPY" record -p "$PID" -d "$DUR" -r 50 --threads --nonblocking -f raw \
    -o "$OUT/all.raw" 2> "$OUT/all.err" || echo "all.raw recording failed, see $OUT/all.err" >&2
sudo "$SPY" record -p "$PID" -d "$DUR" -r 50 --threads --gil --nonblocking -f raw \
    -o "$OUT/gil.raw" 2> "$OUT/gil.err" || echo "gil.raw recording failed, see $OUT/gil.err" >&2
sudo chown -R "$(id -u):$(id -g)" "$OUT"

grep loop_lag "$HOME/.claude-launcher/daemon/daemon.log" 2>/dev/null | tail -n 200 > "$OUT/loop_lag_after.txt"

summarize() {
    local f=$1
    [ -s "$f" ] || { echo "(no samples in $f)"; return; }
    echo "== $(basename "$f"): samples per thread"
    awk -v names="$OUT/threads.txt" '
        BEGIN { while ((getline l < names) > 0) { split(l, p, " "); nm[p[1]] = p[2] } }
        { n = $NF; split($0, a, ";"); id = a[1]; gsub(/[^0-9]/, "", id); t[id] += n; tot += n }
        END { for (k in t) printf "%6d %-8s %s\n", t[k], k, nm[k]; printf "%6d total\n", tot }
    ' "$f" | sort -rn | head -15
    echo "== $(basename "$f"): claude_launcher call chains"
    awk '
        { n = $NF; m = split($0, a, ";"); s = ""
          for (i = 1; i <= m; i++) if (a[i] ~ /claude_launcher/) { x = a[i]; sub(/ [0-9]+$/, "", x); sub(/ \(claude_launcher\//, " (", x); s = s " > " x }
          c[s == "" ? " (no claunch frame)" : s] += n }
        END { for (k in c) printf "%5d%s\n", c[k], k }
    ' "$f" | sort -rn | head -20
}

{
    summarize "$OUT/all.raw"
    echo
    summarize "$OUT/gil.raw"
} | tee "$OUT/summary.txt"
echo "summary: $OUT/summary.txt"
