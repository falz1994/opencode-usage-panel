#!/bin/bash
# Restart the OpenCode Usage panel.
#
# The panel is a long-running GTK process, so a code change only takes effect
# after a restart. This stops whatever instance is running, starts a fresh one
# detached from this shell, and reports the window it created so the result can
# be verified without a display.
#
#   ./restart-panel.sh            stop the old instance, start a new one
#   ./restart-panel.sh --status   only report what is running
#   ./restart-panel.sh --stop     only stop it

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/usage_widget.py"
LOG="$HERE/widget.log"
INTERVAL="${INTERVAL:-300}"

running_pids() {
    # Anchored on purpose. An unanchored `pgrep -f usage_widget.py` also matches
    # any shell, editor or agent that merely *mentions* the script on its own
    # command line — and this script would then kill that process too. Requiring
    # the command line to begin with the interpreter keeps the match to real
    # panel processes (setsid/nohup/env all exec into python3, so the panel's
    # argv[0] really is the interpreter).
    pgrep -f "^python3? .*usage_widget\.py" 2>/dev/null \
        | grep -v -e "^$$\$" -e "^$PPID\$" || true
}

report() {
    local pids
    pids="$(running_pids)"
    if [ -z "$pids" ]; then
        echo "panel: NOT running"
        return 1
    fi
    echo "panel: running (pid $(echo "$pids" | tr '\n' ' '))"
    for p in $pids; do
        echo "  pid $p  up $(ps -o etimes= -p "$p" 2>/dev/null | tr -d ' ')s  cmd $(ps -o args= -p "$p" 2>/dev/null)"
    done
    # Window geometry tells which build is live: the pre-key panel was a fixed
    # 190 px, the key-aware one auto-sizes above 230 px (it grows with its rows).
    if command -v xwininfo >/dev/null 2>&1; then
        local wid
        wid="$(xwininfo -root -tree 2>/dev/null | grep '"OpenCode Usage"' | head -1 | awk '{print $1}')"
        if [ -n "$wid" ]; then
            local geom h
            geom="$(xwininfo -id "$wid" 2>/dev/null | grep -E 'Width:|Height:|Absolute upper-left')"
            echo "  window $wid:"
            echo "$geom" | sed 's/^/    /'
            h="$(echo "$geom" | awk '/Height:/ {print $2}')"
            if [ -n "$h" ] && [ "$h" -ge 230 ] 2>/dev/null; then
                echo "  -> key-aware build (${h} px): OK"
            else
                echo "  -> NOT the key-aware build (expected >= 230 px, got ${h:-?})"
            fi
        fi
    fi
    return 0
}

case "${1:---restart}" in
    --status)
        report
        exit $?
        ;;
    --stop)
        for p in $(running_pids); do
            echo "stopping pid $p"
            kill "$p" 2>/dev/null
        done
        sleep 1
        report || true
        exit 0
        ;;
    --restart|"")
        for p in $(running_pids); do
            echo "stopping pid $p"
            kill "$p" 2>/dev/null
        done
        sleep 1
        for p in $(running_pids); do
            echo "pid $p ignored SIGTERM; killing"
            kill -9 "$p" 2>/dev/null
        done
        sleep 1
        ;;
    *)
        echo "usage: $0 [--restart|--status|--stop]" >&2
        exit 2
        ;;
esac

# GDK_BACKEND=x11 matches the .desktop launcher: under XWayland the panel can
# place itself, which native Wayland forbids.
setsid nohup env GDK_BACKEND=x11 python3 "$SCRIPT" --interval "$INTERVAL" \
    >>"$LOG" 2>&1 < /dev/null &
echo "started pid $! (interval ${INTERVAL}s, log $LOG)"

sleep 3
report
