#!/bin/bash
# End-to-end edge cases for /revive: REAL claude processes, REAL signals.
#
# The crash case is simulated faithfully: a sentinel process stands in for the
# Cursor main process and is named in a fake ~/.claude/ide/<port>.lock. Killing
# the sentinel BEFORE hanging up the terminal reproduces exactly what an OOM
# crash does -- Cursor dies, then its terminals get SIGHUP.
set -u
SKILL="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS="$SKILL/scripts"
PY="${REVIVE_PYTHON:-$(command -v python3)}"
LAB=$(mktemp -d /tmp/revive-e2e-XXXX)
export REVIVE_ROOT="$LAB/registry"
export REVIVE_IDE_DIR="$LAB/ide"
mkdir -p "$REVIVE_ROOT/live" "$REVIVE_ROOT/ended" "$REVIVE_IDE_DIR" "$LAB/proj/.claude"
PORT=59999
PASS=0; FAIL=0
sleepf(){ perl -e "select(undef,undef,undef,$1)"; }

cat > "$LAB/proj/.claude/settings.json" <<J
{ "hooks": {
  "SessionStart":     [{"hooks":[{"type":"command","command":"$PY $SCRIPTS/hook.py SessionStart","timeout":10}]}],
  "UserPromptSubmit": [{"hooks":[{"type":"command","command":"$PY $SCRIPTS/hook.py UserPromptSubmit","timeout":10}]}],
  "SessionEnd":       [{"hooks":[{"type":"command","command":"$PY $SCRIPTS/hook.py SessionEnd","timeout":10}]}]
} }
J

boot(){ # $1 session name -> echoes claude pid
  tmux kill-session -t "$1" 2>/dev/null
  tmux new-session -d -s "$1" -x 200 -y 50 -c "$LAB/proj" \
    "env REVIVE_ROOT=$REVIVE_ROOT REVIVE_IDE_DIR=$REVIVE_IDE_DIR CLAUDE_CODE_SSE_PORT=$PORT claude --permission-mode bypassPermissions"
  for i in $(seq 1 40); do
    o=$(tmux capture-pane -t "$1" -p 2>/dev/null)
    echo "$o" | grep -q "Yes, I trust this folder" && { tmux send-keys -t "$1" Down Enter; sleepf 2; }
    echo "$o" | grep -qE "for shortcuts|Try \"|╰" && break
    sleepf 1
  done
  # Deliberately does NOT type anything. A restored session you lose before
  # typing must still be recorded; an earlier build deleted those records and
  # silently forgot the session.
  tmux list-panes -t "$1" -F "#{pane_pid}"
}
sid_of(){ ls -t "$REVIVE_ROOT"/live/*.json "$REVIVE_ROOT"/ended/*.json 2>/dev/null | head -1; }
state_of(){ [ -z "$1" ] && { echo "NO_RECORD"; return; }; $PY -c "
import sys,os,json;sys.path.insert(0,'$SCRIPTS')
os.environ['REVIVE_ROOT']='$REVIVE_ROOT'
import registry as R
r=json.load(open('$1'));print(R.classify(r))"; }
ck(){ if [ "$2" = "$3" ]; then echo "  PASS $1 (=$2)"; PASS=$((PASS+1));
      else echo "  FAIL $1 got=$2 want=$3"; FAIL=$((FAIL+1)); fi; }

mkfake(){ # $1 = sentinel pid
  cat > "$REVIVE_IDE_DIR/$PORT.lock" <<L
{"pid":$1,"workspaceFolders":["$LAB/proj"],"ideName":"Cursor","transport":"ws"}
L
}

echo "=== E2E-1  tab close: Cursor ALIVE, terminal SIGHUP -> TAB_CLOSED ==="
sleep 300 & SENT=$!; mkfake $SENT
P=$(boot e1); kill -HUP "$P" 2>/dev/null; sleepf 6
F=$(sid_of); ck "E2E-1 terminated" "$(state_of "$F")" "TERMINATED"
kill -9 $SENT 2>/dev/null; tmux kill-session -t e1 2>/dev/null
rm -f "$REVIVE_ROOT"/live/*.json "$REVIVE_ROOT"/ended/*.json

echo "=== E2E-2  crash: Cursor DEAD first, then terminal SIGHUP -> CRASHED ==="
sleep 300 & SENT=$!; mkfake $SENT
P=$(boot e2)
kill -9 $SENT 2>/dev/null; wait $SENT 2>/dev/null   # Cursor dies
sleepf 1
kill -HUP "$P" 2>/dev/null; sleepf 6                # then terminals hang up
F=$(sid_of); ck "E2E-2 crash" "$(state_of "$F")" "CRASHED"
tmux kill-session -t e2 2>/dev/null
rm -f "$REVIVE_ROOT"/live/*.json "$REVIVE_ROOT"/ended/*.json

echo "=== E2E-3  OOM SIGKILL on claude: no end event -> ORPHANED ==="
sleep 300 & SENT=$!; mkfake $SENT
P=$(boot e3); kill -9 "$P" 2>/dev/null; sleepf 5
F=$(sid_of); ck "E2E-3 crashed-sigkill" "$(state_of "$F")" "CRASHED"
ck "E2E-3 record stayed in live/" "$(ls "$REVIVE_ROOT"/live/*.json 2>/dev/null | wc -l | tr -d ' ')" "1"
kill -9 $SENT 2>/dev/null; tmux kill-session -t e3 2>/dev/null
rm -f "$REVIVE_ROOT"/live/*.json "$REVIVE_ROOT"/ended/*.json

echo "=== E2E-4  /exit typed -> CLEAN_EXIT ==="
sleep 300 & SENT=$!; mkfake $SENT
P=$(boot e4); tmux send-keys -t e4 "/exit" Enter; sleepf 8
F=$(sid_of); ck "E2E-4 clean exit" "$(state_of "$F")" "EXITED"
kill -9 $SENT 2>/dev/null; tmux kill-session -t e4 2>/dev/null
rm -f "$REVIVE_ROOT"/live/*.json "$REVIVE_ROOT"/ended/*.json

echo "=== E2E-5  still running -> RUNNING (never offered, no duplicate) ==="
sleep 300 & SENT=$!; mkfake $SENT
P=$(boot e5); sleepf 2
F=$(sid_of); ck "E2E-5 running" "$(state_of "$F")" "RUNNING"
kill -9 "$P" 2>/dev/null; kill -9 $SENT 2>/dev/null; tmux kill-session -t e5 2>/dev/null

echo
echo "E2E PASSED $PASS / $((PASS+FAIL))"
pkill -9 -f "claude --permission-mode bypassPermissions" 2>/dev/null
rm -rf "$LAB"
[ "$FAIL" -eq 0 ]
