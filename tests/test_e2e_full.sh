#!/bin/bash
# The whole capability, on real processes: a session is created, it crashes,
# /revive finds it and brings it back, and the restored process is checked to
# actually exist in the right folder with the right session id.
#
# This is the test that was missing. The unit suite asserts classification, and
# the only existing "restore" checks read restore()'s SOURCE with
# inspect.getsource to confirm it mentions the right calls. Nothing ran it. A
# ValueError in both CLI call sites survived that way: restore did the work and
# then the command died printing the result.
#
# Runs anywhere macOS runs, including inside a fresh Tart VM, which is the
# point: it needs no Cursor, no dashboard and no login. Claude Code writes the
# transcript and fires the hooks whether or not it is authenticated, and those
# are the only things /revive consumes.
#
#   bash tests/test_e2e_full.sh
set -u
SRC=$(cd "$(dirname "$0")/.." && pwd)
PY="${REVIVE_PYTHON:-$(command -v python3)}"
PASS=0; FAIL=0
ok(){ printf '  \033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
no(){ printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
say(){ printf '\n== %s ==\n' "$1"; }

command -v claude >/dev/null || { echo "claude not installed; nothing to test"; exit 2; }
command -v tmux   >/dev/null || { echo "tmux not installed; needed to hold a real session"; exit 2; }

# Not under /tmp: revive.py's EPHEMERAL filter ignores it by design.
ROOM=$(mktemp -d "$HOME/.revive-full.XXXXXX")
H=$ROOM/home; PROJ=$ROOM/proj; SOCK=revive-full-$$
mkdir -p "$H" "$PROJ"; echo "print('hi')" > "$PROJ/main.py"
cleanup(){
  [ -n "${SID:-}" ] && pkill -9 -f "claude --resume $SID" 2>/dev/null
  tmux -L "$SOCK" kill-server 2>/dev/null
  tmux kill-session -t revive 2>/dev/null
  [ "${KEEP_ROOM:-0}" = 1 ] || rm -rf "$ROOM"
}
trap cleanup EXIT
echo "room: $ROOM"

say "1. install the skill, as a new user would"
HOME=$H $PY "$SRC/scripts/install.py" >/dev/null 2>&1
grep -q hook.py "$H/.claude/settings.json" 2>/dev/null && ok "hooks installed" || { no "hooks not installed"; exit 1; }

say "2. get past first run without logging in"
# Onboarding, not authentication: Claude Code refuses to start until the theme
# and trust prompts are answered, and those answers live in ~/.claude.json.
# Nothing here is a credential.
$PY - "$H" <<'PY'
import json, os, sys
json.dump({"hasCompletedOnboarding": True, "theme": "dark",
           "projects": {}}, open(os.path.join(sys.argv[1], ".claude.json"), "w"))
PY
ok "onboarding state seeded (no credential involved)"

say "3. start a REAL claude session in a real terminal"
tmux -L "$SOCK" new-session -d -s src -c "$PROJ" "HOME=$H exec claude"
for i in $(seq 1 40); do
  out=$(tmux -L "$SOCK" capture-pane -p -t src 2>/dev/null)
  echo "$out" | grep -q "trust this folder" && { tmux -L "$SOCK" send-keys -t src Down; sleep 1
                                                 tmux -L "$SOCK" send-keys -t src Enter; }
  # Never answer the login menu. Pressing Enter here starts an OAuth flow and
  # throws a browser window at whoever is running the suite.
  if echo "$out" | grep -q "Select login method"; then
    no "claude wants a login on this machine"
    cat <<MSG

  This machine has no Claude Code credentials, and the suite will not click
  through an OAuth prompt on your behalf. Either:

    - run 'claude' once in this environment and log in, then re-run; or
    - export ANTHROPIC_API_KEY before running.

  The rest of the suite needs no credential: hooks fire and the transcript is
  written whether or not the session is authenticated, and those are the only
  things /revive reads.
MSG
    exit 2
  fi
  echo "$out" | grep -qE "for shortcuts|esc to interrupt" && break
  sleep 2
done
echo "$out" | grep -qE "for shortcuts|esc to interrupt" && ok "claude reached its prompt" || no "claude never became ready"

say "4. submit a prompt, which is what makes the session recordable"
tmux -L "$SOCK" send-keys -t src "Reply with exactly: OK"; sleep 1
tmux -L "$SOCK" send-keys -t src Enter
for i in $(seq 1 30); do
  SID=$(ls "$H/.claude/session-registry/live" 2>/dev/null | head -1 | sed 's/\.json$//')
  [ -n "$SID" ] && break; sleep 2
done
[ -n "${SID:-}" ] && ok "registry recorded the session: ${SID:0:8}" || { no "no registry record"; exit 1; }
REC=$H/.claude/session-registry/live/$SID.json
TR=$($PY -c "import json;print(json.load(open('$REC'))['transcript_path'])")

# Wait for an ASSISTANT turn, not merely for the file to exist. `claude
# --resume` refuses a transcript that holds only a user turn ("No conversation
# found with session ID"), exits at once, and tmux tears the empty session
# down, which reads as a restore that silently did nothing. The turn does not
# need to be a model reply: an unauthenticated session still records one, so
# this needs no credential and works in a bare VM.
for i in $(seq 1 30); do
  [ -s "$TR" ] && grep -q '"type":"assistant"' "$TR" && break
  sleep 2
done
if [ -s "$TR" ] && grep -q '"type":"assistant"' "$TR"; then
  ok "transcript has a resumable conversation ($(wc -l < "$TR" | tr -d ' ') lines)"
else
  no "transcript never reached a resumable state"
fi

say "5. the two things the real environment decides"
HOST=$(HOME=$H $PY -c "
import sys,json; sys.path.insert(0,'$SRC/scripts')
import registry as R; print(R.host_info(json.load(open('$REC')))['host'])")
[ "$HOST" = "tmux" ] && ok "host detected as tmux (term_program is read)" \
                     || no "host detected as '$HOST', wanted tmux"
# The inherited sse_port stays on the record on purpose: it is how the hook
# found the window lock file, and so how it recorded the Cursor main pid that
# separates a crash from a closed tab. What must not happen is restore treating
# that port as a delivery target. Assert the routing, not the absence.

say "6. crash it: SIGKILL, no SessionEnd, exactly like an OOM"
CPID=$($PY -c "import json;print(json.load(open('$REC'))['pid'])")
kill -9 "$CPID" 2>/dev/null; sleep 3
kill -0 "$CPID" 2>/dev/null && no "claude survived SIGKILL" || ok "claude is dead (pid $CPID)"

say "7. does /revive see it, and say the right thing"
V=$(HOME=$H $PY -c "
import sys,json; sys.path.insert(0,'$SRC/scripts')
import registry as R; v=R.view(json.load(open('$REC')))
print('%s|%s|%s' % (v['state'], v['restorable'], v.get('detail','')))")
[ "${V%%|*}" = CRASHED ] && ok "state CRASHED" || no "state ${V%%|*}, wanted CRASHED"
echo "$V" | cut -d'|' -f2 | grep -q True && ok "marked restorable" || no "not restorable"
HOME=$H $PY "$SRC/scripts/revive.py" list 2>&1 | grep -q "$(basename "$PROJ")" \
  && ok "revive.py list offers it" || no "revive.py list does not offer it"
GRP=$(HOME=$H $PY -c "
import sys; sys.path.insert(0,'$SRC/scripts')
import revive as V
cs=[c for c in V.candidates(days=3650) if c['session_id']=='$SID']
print(V.group_key(cs[0]) if cs else 'NO-CANDIDATE')")
[ "$GRP" = "app:tmux" ] && ok "grouped as app:tmux, never into a Cursor window" \
                        || no "grouped as '$GRP', wanted app:tmux"

say "8. restore it for real"
OUT=$(HOME=$H TMUX= $PY "$SRC/scripts/revive.py" restore "$SID" 2>&1)
echo "$OUT" | sed 's/^/      /'
echo "$OUT" | grep -q Traceback && no "restore ended in a traceback" || ok "restore exited cleanly"
sleep 6

say "9. is the session actually back"
RPID=$(pgrep -f "claude --resume $SID" | tail -1)
[ -n "$RPID" ] && ok "a claude --resume process exists (pid $RPID)" || no "nothing was resumed"
if [ -n "$RPID" ]; then
  CWD=$(lsof -a -p "$RPID" -d cwd 2>/dev/null | tail -1 | awk '{print $NF}')
  [ "$CWD" = "$PROJ" ] && ok "restored into the original folder" || no "restored into $CWD, wanted $PROJ"
fi

printf '\n  \033[1mE2E %d passed, %d failed\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
