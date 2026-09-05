#!/usr/bin/env python3
"""Local dashboard for /revive. Binds 127.0.0.1 only, no deps, no build step."""
import json, os, re, sys, glob, threading, subprocess, secrets, socket, time
import http.server, socketserver, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import registry as R
import revive as V

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Built shadcn/React app. Falls back to the dependency-free page if not built.
DIST = os.path.join(SKILL, "app", "dist")
UI = os.path.join(SKILL, "ui")
# Audit finding #7. A fixed port with no auth meant any local process, and
# potentially any web page in your browser, could POST /api/restore or hit
# /api/shutdown. Now: an ephemeral port, a per-launch capability token, and
# Host/Origin pinned to loopback.
PORT = int(os.environ.get("REVIVE_PORT", "0"))      # 0 = let the OS pick
TOKEN = secrets.token_urlsafe(24)


def _reuse_endpoint():
    """Keep the SAME port and token across restarts, so the tab's URL stays live.

    Refreshing the dashboard was failing with "connection refused" because every
    launch minted a fresh ephemeral port AND a fresh token, which orphaned the
    URL already loaded in the browser. The token stays a random secret in a 0600
    file; what changes is that it survives a restart instead of invalidating the
    open tab.
    """
    global PORT, TOKEN
    if PORT:                                    # an explicit --port wins
        return
    try:
        with open(ENDPOINT_FILE) as fh:
            d = json.load(fh)
        port, tok = int(d.get("port") or 0), str(d.get("token") or "")
    except Exception:
        return
    if not port or not tok:
        return
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", port))          # free? then we can have it back
        PORT, TOKEN = port, tok
    except OSError:
        pass                                     # still held; take a new one
    finally:
        probe.close()
ENDPOINT_FILE = os.path.join(R.ROOT, "dashboard.json")
MAX_BODY = 1 << 20                                   # 1 MB is plenty for ids
LOOPBACK = ("127.0.0.1", "localhost", "[::1]", "::1")


def _loopback_only(handler):
    """Reject anything not addressed to us on loopback.

    A browser page on any origin can POST to 127.0.0.1 without CORS ever being
    consulted, because a simple request is SENT first and only the response is
    withheld. So the check has to happen here, not in the browser.
    """
    host = (handler.headers.get("Host") or "").rsplit(":", 1)[0]
    if host not in LOOPBACK:
        return False
    origin = handler.headers.get("Origin")
    if origin:
        try:
            h = urllib.parse.urlparse(origin).hostname or ""
        except Exception:
            return False
        if h not in ("127.0.0.1", "localhost", "::1"):
            return False
    return True


COOKIE_NAME = "revive_token"


def _cookie_token(handler):
    """The token from the Cookie header, if the browser has one.

    Note on scope: cookies are keyed by HOST, not port (RFC 6265), so this
    cookie is offered to anything on 127.0.0.1 whatever its port. That is a
    genuine widening versus a URL token, and it is accepted only because any
    process running as this user can already read dashboard.json and drive
    restore, so the boundary is unchanged in practice.
    """
    raw = handler.headers.get("Cookie") or ""
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME:
            return value
    return ""


def _authed(handler, q):
    # Cookie OR header OR query. Keeping the query form is deliberate: it is
    # what lets an already-open tab keep working after the server restarts,
    # which took several rounds to get right and must not regress.
    for got in (_cookie_token(handler),
                handler.headers.get("X-Revive-Token"),
                (q.get("t") or [""])[0]):
        if got and secrets.compare_digest(got, TOKEN):
            return True
    return False
# No practical limit by default: a session from months ago is still yours.
DAYS = int(os.environ.get("REVIVE_DAYS", "3650"))

# Global skills and the plugin cache are the same on every machine. Project
# skills are NOT: the previous list named a single hardcoded project folder, so
# on anyone else's machine the Skills tab quietly showed fewer skills than they
# had. Project directories are now discovered from the sessions themselves,
# which on the author's own machine found four such directories where the
# hardcoded line found one.
SKILL_DIRS = [
    os.path.expanduser("~/.claude/skills"),
    os.path.expanduser("~/.claude/plugins/cache/claude-plugins-official"),
]
if os.environ.get("REVIVE_SKILL_DIRS"):
    SKILL_DIRS += [os.path.expanduser(p) for p
                   in os.environ["REVIVE_SKILL_DIRS"].split(os.pathsep) if p]


def project_skill_dirs(cwds):
    """`.claude/skills` directories reachable from the folders sessions ran in.

    A project's skills usually live at the repo root while a session runs some
    levels below it, so walk up from each cwd until home or the filesystem root.
    Bounded at 6 levels, and every path is stat'd once because the result is
    cached for the request.
    """
    home = os.path.expanduser("~")
    found = []
    for cwd in cwds:
        d = cwd
        for _ in range(6):
            if not d or d in ("/", home):
                break
            cand = os.path.join(d, ".claude", "skills")
            if os.path.isdir(cand) and cand not in found:
                found.append(cand)
            d = os.path.dirname(d)
    return found

BADGE = {                       # card status chip -> label shown in the UI
    "CRASHED":    "Crashed",
    "TERMINATED": "Terminated",
    "EXITED":     "Exited",
    # No hook record exists for these, but they did not exit deliberately
    # either, so they are crashes we inferred rather than observed.
    "BACKFILL":   "Crashed",
    "RUNNING":    "Running",
}


def read_skills(extra_dirs=()):
    """Every SKILL.md across the workspace, deduped by name.

    `extra_dirs` carries the project directories discovered from the sessions,
    so a user's own repo skills appear without anyone's folder being named in
    the source.
    """
    out, seen = [], set()
    for base in list(SKILL_DIRS) + list(extra_dirs):
        for f in glob.glob(os.path.join(base, "**", "SKILL.md"), recursive=True):
            try:
                head = open(f, errors="ignore").read(4000)
            except OSError:
                continue
            m = re.search(r"^---\s*$(.*?)^---\s*$", head, re.S | re.M)
            fm = m.group(1) if m else ""
            name = (re.search(r"^name:\s*(.+)$", fm, re.M) or [None, None])[1]
            desc = (re.search(r"^description:\s*(.+)$", fm, re.M) or [None, ""])[1]
            name = (name or os.path.basename(os.path.dirname(f))).strip()
            if name in seen:
                continue
            seen.add(name)
            out.append({"name": name, "description": (desc or "").strip()[:300],
                        "path": f})
    out.sort(key=lambda s: s["name"].lower())
    return out


def current_claude_pid():
    """The claude process that launched this dashboard.

    serve.py is a descendant of the session the user is typing in, so walking
    up the tree finds it. Lets the UI say "This session" instead of a generic
    "Still open", which reads like a different session was left behind.
    """
    pid = os.getpid()
    for _ in range(10):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=3).stdout.strip()
        except Exception:
            return 0
        if not out:
            return 0
        parts = out.split(None, 1)
        ppid = int(parts[0])
        comm = (parts[1] if len(parts) > 1 else "").lower()
        if "claude" in comm:
            return pid
        pid = ppid
        if pid <= 1:
            return 0
    return 0


SELF_PID = None


# revive.py keeps module-level caches (_TRANSCRIPTS, _HISTORY_INDEX, the scan
# caches) and clears them per request with reset_caches(). That was safe while
# the server was single-threaded. Making it threaded turned it into a race: one
# thread nulls _TRANSCRIPTS while another is inside transcript_for(), giving
# "AttributeError: 'NoneType' object has no attribute 'setdefault'" and a failed
# /api/state. A hard refresh fires the page and the API together, so it hit this
# every time, and the dashboard silently kept showing stale sessions.
STATE_LOCK = threading.Lock()


def state():
    global SELF_PID
    if SELF_PID is None:
        SELF_PID = current_claude_pid()
    # candidates() now returns RUNNING registry records too, and
    # running_sessions() reports the same sessions, so dedupe by id and let the
    # live view win.
    merged = {}
    for c in V.candidates(days=DAYS, only_restorable=False):
        merged[c["session_id"]] = c
    for c in V.running_sessions():
        # Same naming rule as every other card. This path REPLACES the registry
        # record, so without it the session you are typing in is the one card
        # that loses its name.
        merged[c["session_id"]] = V.name_from_history(c)
    cands = list(merged.values())
    marked = set(R.bookmarks())
    cands.sort(key=lambda c: c.get("last_seen", 0), reverse=True)
    # Stable "Window N" labels so the grouping is visible in the UI.
    wlabel, order = {}, []
    for c in cands:
        k = V.group_key(c)
        if k not in wlabel:
            order.append(k)
            # The label names WHERE the session reopens, so it comes from the
            # host. "(not Cursor)" described what a thing is not, and "detached"
            # leaked an internal marker onto the card.
            if k.startswith("app:"):
                wlabel[k] = k.split(":", 1)[1]
            elif k.startswith("port:"):
                # Just the app. The number was an index over distinct sse_ports
                # in the order they were encountered, which matches nothing you
                # can see: "window 4" is not the fourth window on screen, and a
                # restore labelled window 4 landed in the window already open.
                # Grouping still uses the port, so sessions that shared a window
                # still come back together; we simply cannot NAME that window.
                wlabel[k] = "Cursor"
            else:
                # Say Unknown, not the default's name. Printing "Cursor" here
                # and "Cursor*" on the chip said the same guess twice, and the
                # banner above already states where unknowns go.
                wlabel[k] = "Unknown"
    sessions = []
    for c in cands:
        cwd = c.get("cwd") or ""
        sessions.append({
            "id": c["session_id"],
            "short": c["session_id"][:8],
            "folder": os.path.basename(cwd.rstrip("/")) or cwd or "?",
            "sessionName": c.get("session_name") or "",
            "cwd": cwd,
            "prompt": c.get("last_prompt") or "",
            "state": c.get("_state"),
            "badge": BADGE.get(c.get("_state"), c.get("_state")),
            "ago": V.ago(c.get("last_seen")),
            "lastSeen": c.get("last_seen") or 0,
            "lifetime": V.human_span(c.get("lifetime") or 0),
            "how": R.end_detail(c) if c.get("_origin") == "registry" else "",
            # a property, not a state: the session ended how it ended, but
            # without a transcript there is nothing to resume from
            "gone": not R.view(c)["has_transcript"],
            # Per session, not per badge. A cleared session is EXITED but still
            # resumable, which a badge list can never express.
            "restorable": bool(R.view(c)["restorable"]),
            "bookmarked": c["session_id"] in marked,
            # /clear mints a new id; carry the chain both ways so a cleared
            # session is not a dead end on screen
            "predecessor": (c.get("predecessor") or "")[:8],
            "successor": (c.get("successor") or "")[:8],
            "window": V.group_key(c),
            # One rule for every host: name it only when it was OBSERVED.
            # Declared and default are both guesses, so an Obsidian backfill is
            # as Unknown as a Cursor one. Naming a guess, and then flagging it
            # with an asterisk, was two ways of saying the same uncertainty.
            "windowLabel": (wlabel[V.group_key(c)]
                            if R.host_info(c)["source"] == "observed"
                            else "Unknown"),
            "running": c.get("_state") == "RUNNING",
            "host": R.host_info(c)["host"] or "unknown",
            # observed | declared | default | unknown. The card shows the
            # difference so a guess never reads as a fact.
            "host_source": R.host_info(c)["source"],
            "isCurrent": bool(SELF_PID) and (
                c.get("live_pid") == SELF_PID or c.get("pid") == SELF_PID),
        })
    folders = []
    for s in sessions:
        hit = next((f for f in folders if f["name"] == s["folder"]), None)
        if hit:
            hit["count"] += 1
        else:
            folders.append({"name": s["folder"], "cwd": s["cwd"], "count": 1})
    folders.sort(key=lambda f: (-f["count"], f["name"].lower()))
    windows = len({s["window"] for s in sessions if not s["running"]})

    return {"sessions": sessions, "folders": folders, "skills": read_skills(project_skill_dirs(
            {c.get("cwd") for c in cands if c.get("cwd")})),
            "windows": windows,
            "restorable": ["Crashed", "Terminated"],
            "default_host": R.default_host() or "",
            "sort_mode": R.sort_mode(),
            "bookmarks": sorted(marked),
            "available_hosts": R.available_hosts(),
            # name -> observed | plugin | pty | known, so the picker can say
            # what each answer rests on instead of presenting them as equal
            "host_evidence": R.detect_hosts(),
            "missing_hosts": R.missing_hosts(),
            "statusOrder": ["Crashed", "Terminated", "Running", "Exited"]}


class H(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p, q = u.path, urllib.parse.parse_qs(u.query)
        # Static assets stay open: the browser must be able to load the page
        # before any JS exists to present a token. Everything under /api/ is
        # guarded, because that is what leaks prompts and paths and what acts.
        if p.startswith("/api/"):
            if not _loopback_only(self):
                return self._send(403, {"error": "non-loopback request refused"})
            if not _authed(self, q):
                return self._send(401, {"error": "bad or missing capability token"})
        if p == "/api/pulse":
            # A cheap change-detector. /api/state costs ~1s because it walks
            # every project dir and tails ~700 transcripts, so polling THAT for
            # liveness would be absurd. This stats two directories instead, so
            # the page can poll every couple of seconds and only fetch the full
            # state when something actually changed. That is what makes a
            # /rename or an exit show up on its own instead of on focus.
            sig = []
            for sub in ("live", "ended"):
                d = os.path.join(R.ROOT, sub)
                try:
                    for n in os.listdir(d):
                        if n.endswith(".json"):
                            sig.append((n, os.path.getmtime(os.path.join(d, n))))
                except OSError:
                    pass
            try:
                sig.append(("history", os.path.getmtime(R.HISTORY)))
            except OSError:
                pass
            sig.sort()
            import hashlib
            h = hashlib.sha1(repr(sig).encode()).hexdigest()[:16]
            return self._send(200, {"pulse": h, "n": len(sig)})
        if p == "/api/search":
            # Searching the CONVERSATION, not just the card fields. cwd, name
            # and last prompt are already on the client; this is the other 99%
            # of the text, which only exists inside the transcripts. grep does
            # 1.5 GB far faster than Python can, so shell out to it.
            term = (q.get("q") or [""])[0].strip()
            if len(term) < 2:
                return self._send(200, {"ids": [], "note": "query too short"})
            root = os.path.expanduser("~/.claude/projects")
            try:
                r = subprocess.run(
                    ["grep", "-rilF", "--include=*.jsonl", "--", term, root],
                    capture_output=True, text=True, timeout=25)
                ids = []
                for line in r.stdout.splitlines():
                    base = os.path.basename(line)
                    if base.endswith(".jsonl"):
                        ids.append(base[:-6])
                return self._send(200, {"ids": sorted(set(ids)),
                                        "count": len(set(ids))})
            except subprocess.TimeoutExpired:
                return self._send(200, {"ids": [], "note": "search timed out"})
            except Exception as e:
                return self._send(200, {"ids": [], "note": str(e)})
        if p == "/api/state":
            with STATE_LOCK:
                return self._send(200, state())
        if p in ("/", "/index.html"):
            # Hand the page a cookie so the token can leave the URL. Only when
            # the request already proved it holds the token: this mints nothing,
            # it moves a credential the caller already had.
            extra = None
            if _loopback_only(self) and _authed(self, q):
                extra = [("Set-Cookie",
                          "%s=%s; Path=/; HttpOnly; SameSite=Strict"
                          % (COOKIE_NAME, TOKEN))]
            for base in (DIST, UI):
                f = os.path.join(base, "index.html")
                if os.path.exists(f):
                    return self._send(200, open(f).read(),
                                      "text/html; charset=utf-8", extra=extra)
            return self._send(500, "no UI build found", "text/plain")
        # static assets from the vite build
        rel = p.lstrip("/")
        f = os.path.normpath(os.path.join(DIST, rel))
        if f.startswith(DIST) and os.path.isfile(f):
            ctype = {".js": "text/javascript", ".css": "text/css",
                     ".svg": "image/svg+xml", ".woff2": "font/woff2",
                     ".png": "image/png", ".ico": "image/x-icon",
                     ".map": "application/json"}.get(os.path.splitext(f)[1],
                                                     "application/octet-stream")
            with open(f, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        p, q = u.path, urllib.parse.parse_qs(u.query)
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self._send(413, {"error": "payload too large"})
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            payload = {}
        if not _loopback_only(self):
            return self._send(403, {"error": "non-loopback request refused"})
        if not _authed(self, q):
            return self._send(401, {"error": "bad or missing capability token"})
        if p == "/api/restore":
            ids = set(payload.get("ids") or [])
            with STATE_LOCK:                     # same shared caches
                chosen = [c for c in V.candidates(days=DAYS)
                          if c["session_id"] in ids]
            if not chosen:
                return self._send(400, {"error": "no matching sessions"})
            res = V.restore(chosen)
            if not res["started"] and not res["handed_off"]:
                res["error"] = ("Nothing started. Cursor caches extension code "
                                "per window, so reload the window (Cmd+Shift+P, "
                                "'Developer: Reload Window') and try again.")
            # counts stay for the UI, but the ids are the truth
            res["restored"] = len(res["started"])
            return self._send(200, res)
        if p == "/api/bookmark":
            sid = str(payload.get("id") or "")
            if not sid:
                return self._send(400, {"error": "no session id"})
            marks = R.set_bookmark(sid, bool(payload.get("on")))
            return self._send(200, {"ok": True, "bookmarks": marks})
        if p == "/api/settings":
            cur = R.settings()
            dh = payload.get("default_host")
            if dh is not None:
                dh = str(dh).strip()
                if dh and dh not in R.available_hosts():
                    return self._send(400, {"error": "unknown host %r" % dh})
                cur["default_host"] = dh
            sm = payload.get("sort_mode")
            if sm is not None:
                if sm not in R.SORT_MODES:
                    return self._send(400, {"error": "unknown sort %r" % sm})
                cur["sort_mode"] = sm
            hosts = payload.get("hosts")
            if isinstance(hosts, dict):
                cur["hosts"] = {str(k): str(v) for k, v in hosts.items()}
            R.save_settings(cur)
            return self._send(200, {"ok": True, "settings": cur})
        if p == "/api/shutdown":
            threading.Timer(0.4, lambda: os._exit(0)).start()
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})

    def log_message(self, fmt, *a):
        # Logging exists because, while diagnosing "refresh does nothing", there
        # was no way to tell whether the browser's request even ARRIVED.
        #
        # The query string is STRIPPED. Logging it wrote the capability token on
        # every line, ~1650 times, into a file that was world readable, which
        # cancelled out the token entirely. Method, path, status and client are
        # what made the log useful; the token never was.
        try:
            line = fmt % a
            line = re.sub(r"\?[^\s\"]*", "", line)
            sys.stderr.write("[%s] %s %s\n" % (time.strftime("%H:%M:%S"),
                                                self.address_string(), line))
            sys.stderr.flush()
        except Exception:
            pass


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Threaded, and daemon threads so a hung request cannot block shutdown.

    A browser loads the page, the JS bundle, the CSS and /api/state at the same
    time. On a single-threaded server those queue behind a /api/state that takes
    over a second, so a refresh looked like the dashboard had died. It also made
    one aborted request enough to stall everything behind it.
    """
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # A refresh aborts in-flight requests; that is normal, not a crash.
        import sys as _s, traceback
        exc = _s.exc_info()[0]
        if exc in (BrokenPipeError, ConnectionResetError):
            return
        traceback.print_exc()


def _log(msg):
    sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stderr.flush()


def _install_death_logging():
    """Record HOW this process dies. It has vanished repeatedly leaving an empty
    log, which means a signal, not a crash: a traceback would have been written.
    Without knowing which signal, every explanation is a guess."""
    import atexit, signal as _sig

    # SIGPIPE is deliberately NOT handled, and is explicitly ignored.
    #
    # It fires whenever a client goes away mid-response, which is exactly what
    # a browser refresh does. Python's default is to ignore it and raise
    # BrokenPipeError, which the handler code already deals with. Installing an
    # exiting handler for it turned every aborted request into a dead server:
    # "received SIGPIPE, exiting". That is the "hard refresh kills the
    # dashboard" symptom, caused by the very instrumentation added to diagnose
    # it. Never signal-handle SIGPIPE in a server.
    try:
        _sig.signal(_sig.SIGPIPE, _sig.SIG_IGN)
    except Exception:
        pass

    for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT"):
        s = getattr(_sig, name, None)
        if s is None:
            continue
        def handler(signum, frame, _n=name):
            _log("received %s, exiting" % _n)
            os._exit(0)
        try:
            _sig.signal(s, handler)
        except Exception:
            pass
    atexit.register(lambda: _log("atexit: interpreter shutting down"))


if __name__ == "__main__":
    _install_death_logging()
    _log("starting, pid=%d ppid=%d" % (os.getpid(), os.getppid()))
    _reuse_endpoint()
    socketserver.TCPServer.allow_reuse_address = True
    with Server(("127.0.0.1", PORT), H) as srv:
        port = srv.server_address[1]
        # The port is now ephemeral, so it has to be published somewhere the
        # CLI can read it. 0600 because the token is in it.
        fd = os.open(ENDPOINT_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"port": port, "token": TOKEN, "pid": os.getpid(),
                       "started_at": time.time()}, fh)
        # The token is deliberately NOT printed here. stdout is redirected to
        # dashboard.log, so printing the URL put the secret straight back into
        # the file the redaction just cleaned. Callers read the real URL from
        # dashboard.json, which is 0600.
        print("revive dashboard -> http://127.0.0.1:%d/  (token in %s)"
              % (port, os.path.basename(ENDPOINT_FILE)))
        sys.stdout.flush()
        # Deliberately NOT deleted on exit: the next start reads it to reclaim
        # the same port and token, which is what keeps an open tab working.
        srv.serve_forever()
