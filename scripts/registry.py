#!/usr/bin/env python3
"""Shared registry model for /revive.

One record per Claude Code session, written by hook.py and read by revive.py.

The central problem this solves: after a Cursor crash you cannot tell which
sessions were live, because closing a terminal tab and crashing Cursor deliver
the SAME signal (SIGHUP) to claude, and both fire SessionEnd with reason
"other".  The discriminator is whether the Cursor MAIN process was still alive
at the moment the session ended -- alive means you closed a tab, dead means
Cursor died under you.  See classify() below.
"""
import json, os, re, shutil, subprocess, threading, time, glob

HOME = os.path.expanduser("~")
ROOT = os.environ.get("REVIVE_ROOT") or os.path.join(HOME, ".claude", "session-registry")
LIVE = os.path.join(ROOT, "live")
ENDED = os.path.join(ROOT, "ended")
IDE_DIR = os.environ.get("REVIVE_IDE_DIR") or os.path.join(HOME, ".claude", "ide")
PROJECTS = os.path.join(HOME, ".claude", "projects")
HISTORY = os.path.join(HOME, ".claude", "history.jsonl")

for d in (LIVE, ENDED):
    os.makedirs(d, exist_ok=True)


# ---------------------------------------------------------------- process id
def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def pid_start(pid):
    """Start time of a pid, used to defeat PID REUSE.

    Liveness alone is not proof of identity. macOS assigns PIDs sequentially and
    wraps at 99999; measured on this machine the highest live PID was 99970, so
    wraparound is imminent, not hypothetical. A reboot also restarts numbering
    from the low end, where recorded PIDs are very likely to collide.

    `ps -o lstart` has 1-second resolution, so it is necessary but not
    sufficient. identity_of() pairs it with the command name.
    """
    if not pid:
        return ""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=3)
        return out.stdout.strip()
    except Exception:
        return ""


def pid_comm(pid):
    """Command name of a pid. Second half of the identity check.

    A reused PID almost never belongs to another claude process, so comparing
    the command catches the collisions that a 1-second timestamp cannot.
    """
    if not pid:
        return ""
    try:
        return subprocess.run(["ps", "-o", "comm=", "-p", str(pid)],
                              capture_output=True, text=True,
                              timeout=3).stdout.strip()
    except Exception:
        return ""


def same_process(pid, recorded_start, recorded_comm=None):
    """True only when pid is alive AND is still the process we recorded.

    Three tiers, cheapest first:
      1. alive at all
      2. same start time  (defeats reuse across seconds and across reboots)
      3. same command     (defeats reuse WITHIN the same second, which tier 2
                           cannot see because ps only reports whole seconds)

    Failing safe matters here: a false "still running" HIDES a recoverable
    session, so every tier is required to agree before we believe it.
    """
    if not pid_alive(pid):
        return False
    if recorded_start and pid_start(pid) != recorded_start:
        return False
    if recorded_comm and pid_comm(pid) != recorded_comm:
        return False
    return bool(recorded_start or recorded_comm)  # no identity recorded -> not trusted


HOSTS_FILE = os.path.join(ROOT, "hosts.json")        # legacy, still honoured
SETTINGS_FILE = os.path.join(ROOT, "settings.json")


def settings():
    try:
        with open(SETTINGS_FILE) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_settings(d):
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(d, fh, indent=2)
    os.replace(tmp, SETTINGS_FILE)      # atomic, so a crash cannot truncate it
    return d


PLACEMENTS = os.path.join(ROOT, "placements.json")


def placements():
    try:
        with open(PLACEMENTS) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def record_placement(sid, host):
    """Remember that WE put this session somewhere, so we never mistake our own
    decision for evidence about where it belongs."""
    d = placements()
    d[sid] = host
    tmp = PLACEMENTS + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(d, fh, indent=2)
    os.replace(tmp, PLACEMENTS)


SORT_MODES = ("label", "date")


def sort_mode():
    """How the card list is ordered. Stored with the other preferences, so it
    survives a reload, an exit and a server restart, exactly like bookmarks."""
    m = (settings().get("sort_mode") or "").strip()
    return m if m in SORT_MODES else "label"


OPEN_MODES = ("auto", "cursor", "browser")


def open_mode():
    """Where the dashboard should be opened.

    "auto" keeps the original behaviour: a Cursor tab when we are demonstrably
    inside a live Cursor window, the default browser otherwise. The other two
    are overrides, because "demonstrably" is not always what you want. Reading
    the dashboard in a real browser gives you devtools, a zoom control and a tab
    that survives a window reload, and some people simply prefer it there.
    """
    m = (settings().get("open_mode") or "").strip()
    return m if m in OPEN_MODES else "auto"


def set_open_mode(mode):
    if mode not in OPEN_MODES:
        raise ValueError("open_mode must be one of %s" % (OPEN_MODES,))
    d = settings()
    d["open_mode"] = mode
    save_settings(d)
    return mode


def live_cursor_windows():
    """Ports of Cursor windows whose extension server is actually listening.

    A lock file is written per window and is NOT removed reliably, so its
    presence proves nothing. The listening socket does.
    """
    out = []
    try:
        names = os.listdir(IDE_DIR)
    except OSError:
        return out
    for n in names:
        if not n.endswith(".lock"):
            continue
        p = n[:-5]
        if p.isdigit() and port_listening(p):
            out.append(p)
    return sorted(out)


def bookmarks():
    """Session ids the user starred. Kept in settings.json, NOT in the browser.

    localStorage would be per browser profile and would vanish with site data,
    and the dashboard is opened fresh on an ephemeral port every time. The
    registry is the only store that survives reload, exit and restart.
    """
    b = settings().get("bookmarks")
    return list(b) if isinstance(b, list) else []


def set_bookmark(sid, on):
    cur = settings()
    have = [x for x in (cur.get("bookmarks") or []) if isinstance(x, str)]
    if on and sid not in have:
        have.append(sid)
    elif not on:
        have = [x for x in have if x != sid]
    cur["bookmarks"] = have
    save_settings(cur)
    return have


def default_host():
    """Where to put a session whose host was never recorded.

    Deliberately opt-in and empty by default. Silently defaulting to Cursor is
    the guess that put vault sessions in the wrong app; defaulting to Cursor
    because the USER chose it is a setting. The difference is consent, and the
    dashboard says which one is in force.
    """
    h = (settings().get("default_host") or "").strip()
    return h or None


def host_pref(cwd):
    """Which app a folder's sessions belong to, as declared by the user.

    host_app() can only answer for a live process. 253 of the restorable
    sessions were reconstructed from transcripts and have no host recorded at
    all, and a folder does not imply an app: the note-vault sessions in this
    registry genuinely ran in Cursor, so nothing could have inferred that the
    user wanted them in Obsidian. It has to be declarable.

    Longest matching path prefix wins, so a subfolder can override its parent.
    """
    m = settings().get("hosts")
    if not isinstance(m, dict):
        try:
            with open(HOSTS_FILE) as fh:
                m = json.load(fh)
        except Exception:
            return None
    cwd = os.path.normpath(cwd or "")
    best, blen = None, -1
    for prefix, app in m.items():
        prefix = os.path.normpath(prefix)
        if (cwd == prefix or cwd.startswith(prefix + os.sep)) and len(prefix) > blen:
            best, blen = app, len(prefix)
    return best


def effective_host(rec):
    """The host to restore into: what the user declared, else what we observed.

    OBSERVED wins. A folder is not an app: the same folder can be opened in
    Cursor today and Obsidian tomorrow, so defaulting from the path is a guess
    dressed as a fact. The declared map survives only to fill gaps the recorder
    could not reach, and an unfilled gap stays None so the caller has to ask
    rather than assume Cursor.

    Reversed on 2026-09-04. The earlier order let a path override a directly
    observed process ancestry, and the evidence I used to justify it was partly
    circular: one of the records "proving" the vault ran in Cursor said so only
    because a bug had restored it there.
    """
    return host_info(rec)["host"]


def host_info(rec):
    """Where this session should reopen, AND on what authority.

    The source matters as much as the answer. A card must be able to say "I
    watched this run in Cursor" versus "you told me to assume Cursor", because
    only the second one is a guess you can correct.
    """
    host = rec.get("host_app")
    placed = placements().get(rec.get("session_id"))
    # "detached" means the ancestry walk hit launchd without passing an app. It
    # records that we could not tell, not a place a session can be reopened, so
    # it must fall through to what the user declared rather than being shown as
    # a host called "detached".
    if host in ("detached",):
        host = None
    # tmux is a host, and the ancestry walk cannot see it: tmux re-parents its
    # panes to the tmux server, which is not an app bundle, so host_app() walks
    # to launchd and reports "detached". The session then had no host at all and
    # the working restore_tmux adapter was unreachable in normal use.
    #
    # TERM_PROGRAM was already being recorded for exactly this and then never
    # read by anything. Reading it costs nothing and repairs old records too.
    if not host and rec.get("term_program") == "tmux":
        return {"host": "tmux", "source": "observed"}
    if host and host not in ("unknown", "") and host != placed:
        return {"host": host, "source": "observed"}
    # An observation that revive itself created is not evidence about where the
    # session BELONGS. 66e89b48 read host=Cursor solely because a broken restore
    # put it there, and observed-wins then made that permanent: the mistake
    # became the record that justified repeating it. A placement is remembered
    # but ranks below anything the user declared.
    declared = host_pref(rec.get("cwd"))
    if declared:
        return {"host": declared, "source": "declared"}
    if placed:
        return {"host": placed, "source": "placed"}
    fallback = default_host()
    if fallback:
        return {"host": fallback, "source": "default"}
    return {"host": None, "source": "unknown"}


# Apps that can hold a shell with a coding agent in it. tmux is listed even
# though it is not an app bundle, because it hosts sessions and is drivable
# without any plugin, which makes it the cheapest adapter after Cursor.
KNOWN_HOSTS = (
    ("Cursor",      "/Applications/Cursor.app"),
    ("Visual Studio Code", "/Applications/Visual Studio Code.app"),
    ("Windsurf",    "/Applications/Windsurf.app"),
    ("Terminal",    "/System/Applications/Utilities/Terminal.app"),
    ("iTerm",       "/Applications/iTerm.app"),
    ("Warp",        "/Applications/Warp.app"),
    ("Ghostty",     "/Applications/Ghostty.app"),
    ("Alacritty",   "/Applications/Alacritty.app"),
    ("kitty",       "/Applications/kitty.app"),
    ("Hyper",       "/Applications/Hyper.app"),
    ("Obsidian",    "/Applications/Obsidian.app"),
)


def obsidian_can_host_a_terminal():
    """Obsidian has no terminal of its own. Verify the plugin, not the app.

    Listing Obsidian merely because Obsidian.app exists was wrong: it is a note
    editor, and it can only hold a shell because the third-party `terminal`
    plugin is installed AND enabled in a vault. Obsidian publishes its vault
    list, so both halves are checkable.
    """
    cfg = os.path.expanduser("~/Library/Application Support/obsidian/obsidian.json")
    try:
        with open(cfg) as fh:
            vaults = (json.load(fh).get("vaults") or {}).values()
    except Exception:
        return []
    ok = []
    for v in vaults:
        root = v.get("path") or ""
        if not os.path.isdir(os.path.join(root, ".obsidian", "plugins", "terminal")):
            continue
        try:
            with open(os.path.join(root, ".obsidian",
                                   "community-plugins.json")) as fh:
                if "terminal" not in json.load(fh):
                    continue
        except Exception:
            continue
        ok.append(root)
    return ok


PTY_SYMBOLS = ("_forkpty", "_openpty", "_posix_openpt", "_grantpt")
APP_DIRS = ("/Applications", "/System/Applications",
            "/System/Applications/Utilities",
            os.path.expanduser("~/Applications"))
HOST_CACHE = os.path.join(ROOT, "host-cache.json")
_SCANNING = False


def _scan_in_background(apps, key):
    global _SCANNING
    try:
        hosts = {n: "pty" for n, p in apps.items() if _links_pty(p)}
        if obsidian_can_host_a_terminal():
            hosts["Obsidian"] = "plugin"
        with open(HOST_CACHE, "w") as fh:
            json.dump({"apps": key, "hosts": hosts}, fh, indent=2)
    except Exception:
        pass
    finally:
        _SCANNING = False


def _links_pty(app):
    """Does any binary in this bundle link the pty syscalls?

    This is the generic capability probe. A program cannot open a terminal
    without a pseudo-terminal, and on macOS that means forkpty/openpty, so the
    symbol has to be there whatever the app is written in. Measured: Terminal
    yes (its own binary), Cursor yes (pty.node), Claude yes (pty.node),
    Obsidian no, Chrome no. No list of app names is involved.

    Scanning is capped: a bundle with hundreds of binaries is a browser, and
    browsers are not terminals.
    """
    scanned = 0
    for root, _dirs, files in os.walk(app):
        for f in files:
            p = os.path.join(root, f)
            if scanned > 300:
                return False
            try:
                if os.path.islink(p) or not os.access(p, os.X_OK):
                    continue
            except OSError:
                continue
            # A bundled interpreter links pty because interpreters do, which
            # says nothing about the app. Blender ships python3.13; its own
            # binary links pty too, so Blender still qualifies. That is the
            # honest limit of this signal: it proves capability, not intent.
            if re.match(r"^(python|ruby|perl|php|tclsh|wish)[\d.]*$",
                        os.path.basename(p)):
                continue
            scanned += 1
            try:
                out = subprocess.run(["nm", "-u", p], capture_output=True,
                                     text=True, timeout=4).stdout
            except Exception:
                continue
            if any(s in out for s in PTY_SYMBOLS):
                return True
    return False


def observed_hosts():
    """Apps seen hosting a shell: the only ground truth, and fully generic.

    No list, no probing. If a shell's ancestry reaches an app bundle then that
    app demonstrably hosts terminals. Covers apps nobody thought to enumerate.
    """
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,comm="],
                             capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return {}
    par, comm = {}, {}
    for line in out.splitlines():
        f = line.split(None, 2)
        if len(f) < 3:
            continue
        try:
            par[int(f[0])], comm[int(f[0])] = int(f[1]), f[2]
        except ValueError:
            continue
    found = {}
    for pid, c in comm.items():
        if os.path.basename(c) not in ("zsh", "bash", "fish", "sh", "tcsh", "nu"):
            continue
        walk, seen = pid, 0
        while walk and walk > 1 and seen < 14:
            seen += 1
            cc = comm.get(walk, "")
            if ".app/" in cc:
                bundle = cc[:cc.find(".app/")] + ".app"
                # Only real installed applications. A framework stub such as
                # .../Python3.framework/Resources/Python.app matches ".app/"
                # but is an interpreter, not something you can open a tab in.
                if any(bundle.startswith(d + os.sep) for d in APP_DIRS):
                    found[os.path.basename(bundle)[:-4]] = "observed"
                break
            walk = par.get(walk, 0)
    return found


# The curated list, in display order. Detection answers "can this app open a
# pseudo-terminal", which is true of Blender and OrbStack and useless: nobody
# resumes a coding session in a 3D editor. So capability is now a filter over
# an explicit list of places you would actually work, not the source of it.
#
# kind: "app" -> a bundle under APP_DIRS
#       "cli" -> an executable on PATH; tmux hosts sessions with no app at all
HOST_ALLOWLIST = (
    ("Cursor",             "app", "Cursor.app"),
    ("Obsidian",           "app", "Obsidian.app"),
    ("Terminal",           "app", "Terminal.app"),
    ("tmux",               "cli", "tmux"),
    ("Visual Studio Code", "app", "Visual Studio Code.app"),
    ("Antigravity",        "app", "Antigravity.app"),
    ("OpenCode",           "cli", "opencode"),
    ("ZCode",              "app", "ZCode.app"),
    ("OpenSwarm",          "app", "OpenSwarm.app"),
)


def _installed_apps():
    apps = {}
    for d in APP_DIRS:
        try:
            for n in os.listdir(d):
                if n.endswith(".app"):
                    apps.setdefault(n, os.path.join(d, n))
        except OSError:
            continue
    return apps


def detect_hosts(refresh=False):
    """Every app on this machine that can hold a shell, and on what evidence.

    Three signals, strongest first:
      observed  a shell is running under it right now. Ground truth.
      pty       its bundle links the pty syscalls. Generic static probe.
      plugin    the app itself cannot, but something installed in it can.
                Obsidian is the case: capability lives in the vault, not the
                bundle, so no amount of scanning the app would ever find it.

    The scan is cached because `nm` over every bundle is slow; the cache is
    keyed on the set of installed apps.
    """
    installed = _installed_apps()
    apps = {}
    for name, kind, ident in HOST_ALLOWLIST:
        if kind == "cli":
            if shutil.which(ident):
                apps[name] = None
        elif ident in installed:
            apps[name] = installed[ident]
    key = sorted(apps)
    if not refresh:
        try:
            with open(HOST_CACHE) as fh:
                c = json.load(fh)
            if c.get("apps") == key:
                cached = c.get("hosts") or {}
                cached.update(observed_hosts())     # observation is always live
                return cached
        except Exception:
            pass
        # Measured at 48s on this machine: `nm` over every bundle. It must
        # never sit in front of a page load, so answer now from the two cheap
        # signals and build the cache behind it. The next load is complete.
        global _SCANNING
        if not _SCANNING:
            _SCANNING = True
            threading.Thread(target=_scan_in_background, args=(apps, key),
                             daemon=True).start()
        quick = {n: "known" for n, p in KNOWN_HOSTS if os.path.isdir(p)}
        if obsidian_can_host_a_terminal():
            quick["Obsidian"] = "plugin"
        elif "Obsidian" in quick:
            del quick["Obsidian"]
        quick.update(observed_hosts())
        return quick
    hosts = {}
    for name, path in apps.items():
        if path is None:
            hosts[name] = "cli"
        elif _links_pty(path):
            hosts[name] = "pty"
        else:
            hosts[name] = "installed"
    if "Obsidian" in hosts:
        hosts["Obsidian"] = ("plugin" if obsidian_can_host_a_terminal()
                             else "no-terminal")
    try:
        with open(HOST_CACHE, "w") as fh:
            json.dump({"apps": key, "hosts": hosts}, fh, indent=2)
    except OSError:
        pass
    hosts.update(observed_hosts())
    return hosts


def available_hosts():
    """Allowlisted hosts present on this machine, in the curated display order.

    Obsidian without its terminal plugin is dropped: installed, but it cannot
    hold a shell, so offering it would be a dead end.
    """
    found = detect_hosts()
    return [n for n, _k, _i in HOST_ALLOWLIST
            if n in found and found[n] != "no-terminal"]


def missing_hosts():
    """Allowlisted hosts NOT on this machine, so the picker can show the rest."""
    found = detect_hosts()
    return [n for n, _k, _i in HOST_ALLOWLIST if n not in found]


def host_app(pid):
    """Which GUI app owns the terminal this session runs in.

    A session is not just "in a window", it is in an APP. Walking the process
    ancestry to the first executable under /Applications/<Name>.app tells us
    whether claude is in a Cursor terminal, some other editor, a standalone
    terminal emulator, or nothing at all.

    Correction, 2026-09-03: an earlier version of this docstring claimed the
    a note-vault session was "verified" to descend from a launchd-owned Python
    pty wrapper. That process was a `python3 -m http.server` I had backgrounded
    myself. No Obsidian-hosted session has ever appeared in this registry. The
    function is still right; the evidence cited for it was not.

    This only works for a LIVE pid. Sessions reconstructed from transcripts have
    no process to walk, so they have no host at all: see host_pref().

    Returns an app name, or "detached" when the chain reaches launchd without
    passing through any app bundle.
    """
    seen = 0
    while pid and pid > 1 and seen < 12:
        seen += 1
        try:
            out = subprocess.run(["ps", "-o", "ppid=,command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=3).stdout.strip()
        except Exception:
            return "unknown"
        if not out:
            return "unknown"
        parts = out.split(None, 1)
        try:
            ppid = int(parts[0])
        except ValueError:
            return "unknown"
        cmd = parts[1] if len(parts) > 1 else ""
        # Take the OUTERMOST .app under a real applications directory. The old
        # pattern required `/Applications/<Name>.app/` with nothing between, so
        # /System/Applications/Utilities/Terminal.app never matched and every
        # session in Terminal was recorded as "detached". Outermost matters too:
        # a Cursor window's helper lives at
        # /Applications/Cursor.app/Contents/Frameworks/Cursor Helper.app/...
        # and the owning app is Cursor, not the helper.
        m = None
        for hit in re.finditer(r"\.app/", cmd):
            bundle = cmd[:hit.end() - 1]
            if any(bundle.startswith(d.rstrip("/") + "/") for d in APP_DIRS):
                m = re.match(r".*/([^/]+)\.app$", bundle)
                break
        if m:
            return m.group(1)
        pid = ppid
    return "detached"


def port_listening(port):
    """Is this Cursor WINDOW still alive?

    Every window runs its own Claude extension server on its own port. Closing
    a window tears that host down, so the port stops listening while Cursor
    itself keeps running. That separates "you closed a window" from "you closed
    a tab", which the main-pid check alone cannot: both leave Cursor alive.

    The lock FILE is not usable for this: it is written per window but is not a
    reliable liveness signal. The listening socket is.
    """
    if not port:
        return None
    try:
        out = subprocess.run(["lsof", "-nP", "-iTCP:%s" % port, "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=4).stdout
        return bool(out.strip())
    except Exception:
        return None


def boot_time():
    """Kernel boot time. No longer used for classification.

    It was the input to the STALE rule that hid crashed sessions. Kept because
    the hook still records it per session, which is genuinely useful evidence:
    a record whose `boot` differs from the current one was alive across a
    reboot, and that is exactly what a machine-killing crash looks like.
    """
    try:
        out = subprocess.run(["sysctl", "-n", "kern.boottime"],
                             capture_output=True, text=True, timeout=3).stdout
        for part in out.replace("{", "").replace("}", "").split(","):
            if "sec" in part:
                return int(part.split("=")[1].strip())
    except Exception:
        pass
    return 0


# ------------------------------------------------------------------- window
def window_token(sse_port):
    """Identity of a window INSTANCE, not just of a port.

    A port alone is unsound as a window identity. Reloading a window tears down
    its extension host and the next one binds a DIFFERENT port, so a recorded
    port can be dead (looks like "window closed" when the window is fine) or,
    worse, reused by an unrelated window (looks like "tab closed" when yours is
    gone). The lock file carries an authToken minted per window instance, so
    comparing tokens answers "is this still MY window", which is the real
    question.
    """
    if not sse_port:
        return ""
    d = load(os.path.join(IDE_DIR, "%s.lock" % sse_port)) or {}
    return d.get("authToken") or ""


def same_window(sse_port, recorded_token):
    """True only when that port is still served by the window we recorded."""
    if not sse_port:
        return None
    if not port_listening(sse_port):
        return False                      # nothing there at all
    if not recorded_token:
        return None                       # nothing to compare, stay honest
    return window_token(sse_port) == recorded_token


def window_info(sse_port):
    """Resolve the Cursor window this session belongs to.

    ~/.claude/ide/<port>.lock is written by the IDE extension and names the
    Cursor MAIN process pid plus the window's workspace folders.
    """
    info = {"window_pid": 0, "window_root": "", "ide_name": ""}
    if not sse_port:
        return info
    lock = os.path.join(IDE_DIR, "%s.lock" % sse_port)
    try:
        with open(lock) as fh:
            d = json.load(fh)
        folders = d.get("workspaceFolders") or []
        info["window_pid"] = d.get("pid", 0)
        info["window_root"] = folders[0] if folders else ""
        info["ide_name"] = d.get("ideName", "")
    except Exception:
        pass
    return info


# ------------------------------------------------------------------- record
def path_for(sid, ended=False):
    return os.path.join(ENDED if ended else LIVE, "%s.json" % sid)


def load(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return None


def save(path, rec):
    """Atomic write: a concurrent reader sees the old or new file, never half."""
    tmp = path + ".tmp.%d" % os.getpid()
    try:
        with open(tmp, "w") as fh:
            json.dump(rec, fh, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def all_live():
    out = []
    for p in sorted(glob.glob(os.path.join(LIVE, "*.json"))):
        r = load(p)
        if r:
            r["_path"] = p
            out.append(r)
    return out


def all_ended():
    out = []
    for p in sorted(glob.glob(os.path.join(ENDED, "*.json"))):
        r = load(p)
        if r:
            r["_path"] = p
            out.append(r)
    return out


# --------------------------------------------------------------- classifier
#   Only YOU can end a session on purpose. Everything else is loss.
#
#   RUNNING     alive right now            -> never restore, would duplicate
#   CRASHED     Cursor died, or SIGKILLed  -> restore
#   TERMINATED  hung up or killed          -> restore (you did not choose this)
#   EXITED      /exit, logout, /clear      -> do not restore, you meant it
#
# There are FOUR states, and no others. An earlier build added a fifth, STALE,
# for records with no exit event that predated the last boot. It was never
# asked for, and it hid the exact sessions this tool exists to recover: a crash
# that reboots the machine kills sessions that all started before that reboot.
# Age is a FILTER, not a state, and the dashboard already has a date filter.
RESTORABLE = ("CRASHED", "TERMINATED")


def view(rec, boot=None):          # boot kept for signature compatibility
    """Everything derived about a session, decided once.

    This exists because the derived facts used to be computed in four separate
    places, and every bug in this area was two of them disagreeing:

      "Running, killed outright"     end_detail did not know liveness, classify did
      ghost cards with no transcript transcript_ok was applied at some call sites only
      8 running when 5 existed       classify checked the pid before the end reason
      restorable vs RESTORABLE       the tuple excluded BACKFILL, the caller re-added it

    Liveness, the end reason and the transcript are read ONCE here, and every
    property is derived from those same three values, so they cannot drift.
    """
    reason = rec.get("end_reason")
    # A record already labelled by the backfill reconstruction keeps its label;
    # there is no hook record to classify.
    preset = rec.get("_state") if rec.get("_origin") == "history" else None

    alive = (reason is None and same_process(rec.get("pid"), rec.get("pid_start"),
                                             rec.get("pid_comm")))
    has_transcript = transcript_ok(rec)

    if preset:
        state = preset
    elif alive:
        state = "RUNNING"
    elif reason is None:
        # No exit event ever fired: SIGKILL, the OOM killer, or power loss.
        # Nothing asked this session to stop, so it is a crash. Age plays no
        # part; that is what the date filter is for.
        state = "CRASHED"
    elif reason in ("prompt_input_exit", "logout", "clear", "resume"):
        state = "EXITED"
    elif rec.get("window_alive_at_end") is False:
        state = "CRASHED"
    else:
        state = "TERMINATED"

    if state == "RUNNING":
        detail = ""                       # it has not ended, so it has no ending
    elif reason is None:
        detail = "killed outright"
    elif reason in ("prompt_input_exit", "logout"):
        detail = "you exited"
    elif reason in ("clear", "resume"):
        detail = "superseded"
    elif rec.get("window_alive_at_end") is False:
        detail = "app crash"
    elif rec.get("window_same_at_end") is False:
        detail = "window closed"
    elif rec.get("window_same_at_end") is True:
        detail = "tab closed"
    else:
        detail = "hung up"

    # One gate. A session with no transcript cannot be resumed, whatever its
    # state says, so restorable is never true without it.
    # Listable either way, so you can find what a session was. Restorable only
    # with a transcript, because `claude --resume` has nothing else to read.
    offerable = True
    # /clear starts a fresh session and leaves the previous conversation
    # resumable: `claude --resume <old-id>` still works. Treating it as
    # non-restorable was a product decision I made without noticing. The state
    # stays EXITED (you did mean to clear), but the session is offerable.
    restorable = has_transcript and (
        state in ("CRASHED", "TERMINATED", "BACKFILL")
        or (state == "EXITED" and reason == "clear"))

    return {"state": state, "detail": detail, "alive": alive,
            "offerable": offerable, "restorable": restorable,
            "has_transcript": has_transcript}


def classify(rec, now=None, boot=None):
    """The state alone. Thin wrapper so there is still one derivation."""
    return view(rec, boot=boot)["state"]


def end_detail(rec):
    """How it ended, in the terms you see on screen."""
    return view(rec)["detail"]


def transcript_ok(rec):
    """A session is only offerable if its transcript still exists."""
    tp = rec.get("transcript_path", "")
    return bool(tp) and os.path.exists(tp)
