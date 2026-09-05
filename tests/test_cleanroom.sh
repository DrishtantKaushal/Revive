#!/bin/bash
# Clean-room install test: publish -> clone -> install -> run, all under a
# scratch HOME. This is the only test that exercises the FIRST-RUN path, which
# is invisible to the unit suite: the unit suite imports from wherever the tool
# already lives, on a machine whose settings.json, registry and hooks are all
# already in place. Every bug this file has caught so far was of that shape.
#
# Nothing here touches the real ~/.claude; HOME is redirected before any of the
# tool's code runs, and everything it writes stays inside $ROOM.
#
#   bash tests/test_cleanroom.sh
#
# Two things it deliberately does NOT do, because a scratch HOME cannot fake
# them: it never launches Cursor or Obsidian, so host detection and the restore
# path itself are unproven here, and it runs as the same user on the same OS,
# so nothing about a different platform is established. Those need a real VM or
# a second macOS account.
set -u
SRC=$(cd "$(dirname "$0")/.." && pwd)
ROOM=$(mktemp -d "$HOME/.revive-cleanroom.XXXXXX")   # not /tmp: see EPHEMERAL in revive.py
PUB=$ROOM/publish; CLONE=$ROOM/clone; H=$ROOM/home
PY=$(command -v python3)
pass=0; fail=0
ok(){ printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass+1)); }
no(){ printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=$((fail+1)); }

echo "room: $ROOM"
echo
echo "== 1. build the publishable tree (what a git repo would contain) =="
mkdir -p "$PUB"
rsync -a --exclude 'node_modules' --exclude '__pycache__' --exclude '*.pyc' \
      --exclude '.DS_Store' --exclude '*.patch' --exclude '.git' \
      "$SRC/" "$PUB/"
git -C "$PUB" init -q
git -C "$PUB" add -A
git -C "$PUB" -c user.email=t@t -c user.name=t commit -qm "revive"
echo "  tracked files: $(git -C "$PUB" ls-files | wc -l | tr -d ' ')  size: $(du -sh "$PUB" | cut -f1)"

echo
echo "== 2. clone it, exactly as a user would =="
git clone -q "$PUB" "$CLONE"
[ -f "$CLONE/SKILL.md" ] && ok "clone has SKILL.md" || no "clone missing SKILL.md"
[ -f "$CLONE/app/dist/index.html" ] && ok "clone has prebuilt dashboard (no bun needed)" \
  || no "clone has no app/dist: user must run 'bun run build'"
[ -f "$CLONE/ui/index.html" ] && ok "clone has dependency-free fallback UI" || no "no fallback UI"

echo
echo "== 3. scratch HOME, nothing pre-existing =="
mkdir -p "$H"            # deliberately NO .claude: a machine that has never
export HOME=$H           # run Claude Code. install.py must create it.
echo "  HOME=$HOME  (expanduser ~ -> $($PY -c 'import os;print(os.path.expanduser("~"))'))"

echo
echo "== 4. install.py on a machine with no settings.json =="
out=$($PY "$CLONE/scripts/install.py" 2>&1); rc=$?
[ $rc -eq 0 ] && ok "install.py exit 0" || { no "install.py exit $rc"; echo "$out" | sed 's/^/      /'; }
if [ -f "$H/.claude/settings.json" ]; then
  ok "settings.json created"
  for ev in SessionStart UserPromptSubmit SessionEnd; do
    grep -q "$ev" "$H/.claude/settings.json" && ok "hook registered: $ev" || no "hook missing: $ev"
  done
  if grep -q "$CLONE/scripts/hook.py" "$H/.claude/settings.json"; then
    ok "hook path points at the clone, not the author's install"
  else
    no "hook path wrong:"; grep -o '"command":[^,]*' "$H/.claude/settings.json" | sed 's/^/      /'
  fi
  hits=$(grep -rIl "$(id -un)\|/Users/\|/home/\|CommandLineTools" \
           "$CLONE/scripts" "$CLONE/SKILL.md" 2>/dev/null)
  [ -z "$hits" ] && ok "no author paths in shipped scripts or SKILL.md" \
    || { no "author paths still shipped:"; echo "$hits" | sed 's/^/      /'; }
else
  no "settings.json NOT created"
fi

  n1=$(grep -c "hook.py" "$H/.claude/settings.json")
  $PY "$CLONE/scripts/install.py" >/dev/null 2>&1
  $PY "$CLONE/scripts/install.py" >/dev/null 2>&1
  n3=$(grep -c "hook.py" "$H/.claude/settings.json")
  [ "$n1" = "$n3" ] && ok "install is idempotent ($n1 entries after 3 installs)" \
    || no "installs accumulate: $n1 -> $n3 hook entries"

echo
echo "== 5. the hook actually runs (SessionStart, empty registry) =="
hookout=$(echo '{"session_id":"cleanroom-1","cwd":"'"$H"'","hook_event_name":"SessionStart","source":"startup"}' \
  | $PY "$CLONE/scripts/hook.py" SessionStart 2>&1); rc=$?
[ $rc -eq 0 ] && ok "SessionStart hook exit 0" || { no "SessionStart hook exit $rc"; echo "$hookout" | sed 's/^/      /'; }
echo '{"session_id":"cleanroom-1","cwd":"'"$H"'","hook_event_name":"UserPromptSubmit","prompt":"hi"}' \
  | $PY "$CLONE/scripts/hook.py" UserPromptSubmit >/dev/null 2>&1 && ok "UserPromptSubmit hook exit 0" || no "UserPromptSubmit hook failed"
if [ -d "$H/.claude/session-registry/live" ] && [ -n "$(ls -A "$H/.claude/session-registry/live" 2>/dev/null)" ]; then
  ok "registry record written on first prompt"
else
  no "no registry record after UserPromptSubmit"
fi
echo '{"session_id":"cleanroom-1","cwd":"'"$H"'","hook_event_name":"SessionEnd","reason":"other"}' \
  | $PY "$CLONE/scripts/hook.py" SessionEnd >/dev/null 2>&1 && ok "SessionEnd hook exit 0" || no "SessionEnd hook failed"

echo
echo "== 6. CLI on a virgin machine =="
for cmd in list doctor "backfill --days 7"; do
  o=$($PY "$CLONE/scripts/revive.py" $cmd 2>&1); rc=$?
  [ $rc -eq 0 ] && ok "revive.py $cmd exit 0" || { no "revive.py $cmd exit $rc"; echo "$o" | tail -12 | sed 's/^/      /'; }
done
$PY "$CLONE/scripts/revive.py" list 2>&1 | head -6 | sed 's/^/      | /'

echo
echo "== 7. the server, on a port the OS picks =="
REVIVE_PORT=0 $PY "$CLONE/scripts/serve.py" > "$ROOM/serve.log" 2>&1 &
SPID=$!
for i in $(seq 1 40); do [ -f "$H/.claude/session-registry/dashboard.json" ] && break; sleep 0.25; done
if [ -f "$H/.claude/session-registry/dashboard.json" ]; then
  URL=$($PY -c "import json,os;d=json.load(open(os.path.expanduser('~/.claude/session-registry/dashboard.json')));print('http://127.0.0.1:%s/?t=%s'%(d['port'],d['token']))")
  ok "server came up: $(echo "$URL" | sed 's/t=.*/t=REDACTED/')"
  code=$(curl -s -o "$ROOM/page.html" -w '%{http_code}' "$URL")
  [ "$code" = 200 ] && ok "GET / -> 200 ($(wc -c < "$ROOM/page.html" | tr -d ' ') bytes)" || no "GET / -> $code"
  TOK=$($PY -c "import json,os;print(json.load(open(os.path.expanduser('~/.claude/session-registry/dashboard.json')))['token'])")
  base=$(echo "$URL" | cut -d'?' -f1)
  c=$(curl -s -o /dev/null -w '%{http_code}' -H "X-Revive-Token: $TOK" "${base}api/state"); [ "$c" = 200 ] && ok "GET /api/state -> 200" || no "/api/state -> $c"
  c=$(curl -s -o /dev/null -w '%{http_code}' "${base}api/state"); [ "$c" = 401 ] && ok "unauth /api/state -> 401" || no "unauth /api/state -> $c (expected 401)"
  c=$(curl -s -o /dev/null -w '%{http_code}' -H "X-Revive-Token: $TOK" -H "Origin: https://evil.com" "${base}api/state"); [ "$c" = 403 ] && ok "cross-origin -> 403" || no "cross-origin -> $c (expected 403)"
  LOG=$H/.claude/session-registry/dashboard.log
  if [ -f "$LOG" ]; then
    grep -q "$TOK" "$LOG" && no "TOKEN LEAKED into dashboard.log" || ok "no token in dashboard.log"
    m=$(stat -f '%Sp' "$LOG"); [ "$m" = "-rw-------" ] && ok "dashboard.log mode $m" || no "dashboard.log mode $m (expected -rw-------)"
  fi
else
  no "server never wrote dashboard.json"; tail -20 "$ROOM/serve.log" | sed 's/^/      /'
fi
kill $SPID 2>/dev/null; wait $SPID 2>/dev/null

echo
echo "== 7b. revive.py ui, the only path that writes dashboard.log =="
# serve.py started directly writes no log, so running only that silently skips
# the token-redaction check. Exercise the real entry point instead.
REVIVE_NO_OPEN=1 $PY "$CLONE/scripts/revive.py" ui > "$ROOM/ui.out" 2>&1
LOG=$H/.claude/session-registry/dashboard.log
for i in $(seq 1 40); do [ -f "$LOG" ] && break; sleep 0.25; done
if [ -f "$LOG" ]; then
  ok "revive.py ui started and wrote dashboard.log"
  TOK=$($PY -c "import json,os;print(json.load(open(os.path.expanduser('~/.claude/session-registry/dashboard.json')))['token'])")
  U=$(grep -o "http://127.0.0.1:[0-9]*" "$ROOM/ui.out" | head -1)
  for i in 1 2 3; do curl -s -o /dev/null "$U/?t=$TOK"; curl -s -o /dev/null "$U/api/state?t=$TOK"; done
  sleep 1
  c=$(grep -c "$TOK" "$LOG"); [ "$c" = 0 ] && ok "token absent from dashboard.log after 6 token-bearing requests" \
    || no "TOKEN LEAKED into dashboard.log ($c lines)"
  grep -q "GET /api/state" "$LOG" && ok "log still records method, path and status" || no "log lost its diagnostic value"
  m=$(stat -f "%Sp" "$LOG" 2>/dev/null || stat -c "-%A" "$LOG" 2>/dev/null | cut -c1-11)
  case "$m" in -rw-------) ok "dashboard.log mode $m" ;; *) no "dashboard.log mode $m (want -rw-------)" ;; esac
  grep -q "$TOK" "$ROOM/ui.out" && ok "URL on stdout carries the token (expected, it is the handoff)" || true
  pkill -f "$CLONE/scripts/serve.py" 2>/dev/null
else
  no "revive.py ui never produced dashboard.log"; head -10 "$ROOM/ui.out" | sed "s/^/      /"
fi

echo
echo "== 8. unit suite, run from the clone =="
t=$($PY "$CLONE/tests/test_logic.py" 2>&1 | tail -3); echo "$t" | sed 's/^/      /'
sum=$($PY "$CLONE/tests/test_logic.py" 2>&1 | grep -o "PASSED [0-9]* / [0-9]*" | tail -1)
p=$(echo "$sum" | awk "{print \$2}"); n=$(echo "$sum" | awk "{print \$4}")
[ -n "$p" ] && [ "$p" = "$n" ] && ok "test_logic.py: $sum" || no "test_logic.py: ${sum:-no summary line}"

echo
echo "== 9. uninstall leaves nothing behind =="
$PY "$CLONE/scripts/install.py" uninstall >/dev/null 2>&1
if grep -q "hook.py" "$H/.claude/settings.json" 2>/dev/null; then
  no "hooks still in settings.json after uninstall ($(grep -c hook.py "$H/.claude/settings.json") left)"
else ok "hooks removed on uninstall"; fi
$PY "$CLONE/scripts/install.py" uninstall >/dev/null 2>&1 && ok "uninstall twice is not an error" || no "second uninstall failed"

echo
echo "== 10. cleanup =="
if [ "${KEEP_ROOM:-0}" = 1 ]; then
  echo "  room kept at $ROOM (KEEP_ROOM=1)"
else
  rm -rf "$ROOM" && echo "  room removed (KEEP_ROOM=1 to inspect it)"
fi

printf '\n  \033[1m%d passed, %d failed\033[0m\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
