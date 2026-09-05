#!/usr/bin/env python3
"""/revive -- find Claude Code sessions lost to a Cursor crash and bring them back.

  revive.py list                 what is restorable, and why
  revive.py pick                 interactive multi-select, then restore
  revive.py restore <sid>...     restore specific sessions
  revive.py backfill [--days N]  reconstruct candidates with no registry record
  revive.py doctor               health check
"""
import base64, json, os, signal, sys, time, subprocess, glob, re
import urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import registry as R

EXIT_RE = re.compile(r"^/(exit|quit)\b|^exit$", re.I)
# A session whose last act was /clear was retired in place: Claude Code issues a
# NEW session id and the old one is a dead end. Same for /logout.
RETIRED_RE = re.compile(r"^/(clear|logout)\b", re.I)
# Slash commands are UI actions, not prompts. They make useless picker labels
# ("/rename BreezePM"), so we keep the last REAL prompt for the label.
SLASH_RE = re.compile(r"^/")
# Session names are not stored in the transcript or ~/.claude.json, but every
# `/rename <name>` you typed is in history.jsonl. That is the only record of a
# session's real name, so recover it from there.
RENAME_RE = re.compile(r"^/rename\s+(.+)$", re.I)
EPHEMERAL = ("/private/tmp/", "/tmp/", "/var/folders/")


def live_claude_cwds():
    """cwd of every claude process running right now.

    Backfill has no pid to check, so this is how it avoids offering a session
    that is still open in another tab -- restoring it would run two claudes
    against one transcript.
    """
    cwds = set()
    try:
        out = subprocess.run(["ps", "-Ao", "pid=,comm="], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return cwds
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pid, comm = parts
        if "claude" not in comm.lower():
            continue
        try:
            lo = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                                capture_output=True, text=True, timeout=5).stdout
            for l in lo.splitlines():
                if l.startswith("n"):
                    cwds.add(l[1:])
        except Exception:
            pass
    return cwds


def ago(ts):
    if not ts:
        return "?"
    s = max(0, int(time.time() - ts))
    if s < 90:
        return "%ds" % s
    if s < 5400:
        return "%dm" % (s // 60)
    if s < 172800:
        return "%dh" % (s // 3600)
    return "%dd" % (s // 86400)


def human_span(sec):
    """How long a session lived, as a compact string."""
    sec = int(sec or 0)
    if sec <= 0:
        return ""
    if sec < 3600:
        return "%dm" % max(1, sec // 60)
    if sec < 86400:
        return "%dh %dm" % (sec // 3600, (sec % 3600) // 60)
    return "%dd %dh" % (sec // 86400, (sec % 86400) // 3600)


def short(p, n=34):
    """Shorten a path for a card. Home becomes ~, then trim from the left.

    There used to be a second rule that abbreviated one hardcoded project
    folder. On any other machine it silently did nothing, which is the worst
    kind of hardcoding: not an error, just a feature that quietly works for one
    person only.
    """
    p = p.replace(os.path.expanduser("~"), "~")
    return p if len(p) <= n else "..." + p[-(n - 3):]


# ------------------------------------------------------------------ sources
def prompt_from_transcript(path, tail_bytes=512 * 1024):
    """Last real user message, read from the transcript itself.

    The registry only learns a prompt from UserPromptSubmit, and history only
    from what you typed at the CLI. A session can therefore have a full
    conversation and still show "no recorded prompt", which is what happened to
    a crashed Breeze session holding five user messages.

    Reads only the tail, because a transcript can be 70 MB.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()              # discard the partial line
            lines = fh.read().decode("utf-8", "ignore").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") != "user":
            continue
        c = (d.get("message") or {}).get("content")
        t = ""
        if isinstance(c, str):
            t = c.strip()
        elif isinstance(c, list):
            t = " ".join(x.get("text", "") for x in c
                         if isinstance(x, dict) and x.get("type") == "text").strip()
        # skip system scaffolding and slash commands, they say nothing useful
        if not t or t.startswith("<") or t.startswith("Caveat") or t.startswith("/"):
            continue
        return t.replace("\n", " ")[:160]
    return ""


def _touch(rec):
    """Best estimate of when a session was last worked on.

    `last_seen` only advances on UserPromptSubmit, so a session that ran for
    hours without a new prompt reads as days old. The transcript's mtime is the
    honest signal, because the file is written as the session works. Measured:
    two crashed sessions showed as 2 days stale while their transcripts had been
    written 6 minutes before the reboot.
    """
    # mtime is not the honest signal when the FILE was moved rather than
    # written: 14 recovered vault transcripts all read "May 20 13:55", the
    # moment I copied them out of the Obsidian git repo, so the newest-first
    # ordering was meaningless for them. The last timestamp INSIDE the
    # transcript is what the session actually did, so prefer it and fall back
    # to mtime only when the file carries no timestamps at all.
    tp = rec.get("transcript_path", "")
    if tp:
        inner = 0.0
        try:
            inner = _scan_transcript_ends(tp)[2]
        except Exception:
            inner = 0.0
        if inner:
            rec["last_seen"] = inner
        else:
            try:
                rec["last_seen"] = max(rec.get("last_seen", 0) or 0,
                                       os.path.getmtime(tp))
            except OSError:
                pass
        if not (rec.get("last_prompt") or "").strip():
            rec["last_prompt"] = prompt_from_transcript(tp)
    return rec


def name_from_history(rec):
    """Fill in the /rename name, which only ever lands in history.jsonl.

    Applied on EVERY path that produces a card. Doing it in one path was not
    enough: running sessions are rebuilt by running_sessions() and overwrite
    the registry record in the merge, so the session you are typing in showed
    its folder name while every other named session showed its name.
    """
    if (rec.get("session_name") or "").strip():
        return rec
    h = history_index().get(rec.get("session_id")) or {}
    if (h.get("name") or "").strip():
        rec["session_name"] = h["name"].strip()
    return rec


def registry_candidates():
    """Records the hooks wrote. Authoritative."""
    out = []
    boot = R.boot_time()
    for rec in R.all_live() + R.all_ended():
        # A record whose transcript is gone cannot be resumed, so showing it is
        # worse than useless: `/resume` mints a NEW session id and the old id
        # keeps its record while the transcript moves to the new one. Those
        # orphans looked exactly like duplicates of the live session.
        _touch(rec)
        v = R.view(rec, boot=boot)
        if not v["offerable"]:          # no transcript, cannot ever be resumed
            continue
        # `/rename` is typed at the CLI, so it lands in history.jsonl and never
        # in the record the hook wrote. Backfilled sessions picked the name up
        # because they are built FROM history; hook-recorded ones showed the
        # folder instead, so a session you had explicitly named displayed as
        # "Repos". Take the name from history when the record has none.
        name_from_history(rec)
        rec["_state"] = v["state"]
        rec["_origin"] = "registry"
        out.append(rec)
    return out


def history_index():
    """sid -> record built from ~/.claude/history.jsonl.

    A dict rather than a tuple: this grew from 3 fields to 6 and the positional
    unpacking broke twice.

      first / last : timestamps, so a session's LIFETIME can be shown
      display      : the final entry, used for the /exit and /clear rules
      label        : the last entry that was not a slash command
      name         : the last `/rename <name>`, the only record of a real name
    """
    global _HISTORY_INDEX
    if _HISTORY_INDEX is not None:
        return _HISTORY_INDEX
    idx = {}
    if not os.path.exists(R.HISTORY):
        return idx           # not cached: history may appear later
    with open(R.HISTORY, errors="ignore") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            sid = d.get("sessionId")
            if not sid:
                continue
            ts = d.get("timestamp", 0) / 1000.0
            disp = (d.get("display") or "").strip()
            e = idx.setdefault(sid, {"first": ts, "last": 0.0, "project": "",
                                     "display": "", "label": "", "name": ""})
            e["first"] = min(e["first"], ts)
            rn = RENAME_RE.match(disp)
            if rn:
                e["name"] = rn.group(1).strip()
            elif disp and not SLASH_RE.match(disp):
                e["label"] = disp
            if ts >= e["last"]:
                e["last"] = ts
                e["project"] = d.get("project", "")
                e["display"] = disp
    _HISTORY_INDEX = idx
    return idx


_TRANSCRIPTS = None
# Memoised per request. It was rebuilt on EVERY call, and once
# _closed_deliberately began consulting it as a fallback that became 432
# rebuilds per dashboard load: 16.9 of 17.8 seconds.
_HISTORY_INDEX = None

# Directory listings used by decode_project_dir. Decoding walks the same few
# ancestors ("/", "/Users", the home directory) once per project folder, and
# there are hundreds of those, so scanning them fresh every time cost ~46 ms
# per decode. Shared within a request, dropped with everything else after it.
_DIR_INDEX = None


def reset_caches():
    """Drop the transcript index.

    It is cached so one request does a single directory scan instead of ~800,
    but it must not outlive the request: transcripts appear and vanish, and a
    stale index silently hides sessions.
    """
    global _TRANSCRIPTS, _HISTORY_INDEX, _DIR_INDEX
    _TRANSCRIPTS = None
    _HISTORY_INDEX = None
    _DIR_INDEX = None


def transcript_for(sid):
    """Path to a session's transcript, or "" if it is gone.

    Built once by walking ~/.claude/projects. The previous version globbed for
    every session id, which is ~800 directory scans when the day limit is
    lifted; one pass turns that into a dict lookup.
    """
    global _TRANSCRIPTS
    if _TRANSCRIPTS is None:
        _TRANSCRIPTS = {}
        for f in glob.glob(os.path.join(R.PROJECTS, "*", "*.jsonl")):
            _TRANSCRIPTS.setdefault(os.path.basename(f)[:-6], f)
    return _TRANSCRIPTS.get(sid, "")


def _dir_index(base, encode):
    """Subdirectories of `base`, keyed by their encoded name. None if unreadable."""
    global _DIR_INDEX
    if _DIR_INDEX is None:
        _DIR_INDEX = {}
    if base in _DIR_INDEX:
        return _DIR_INDEX[base]
    kids = {}
    try:
        with os.scandir(base) as it:
            for e in it:
                try:
                    if e.is_dir():         # d_type, so usually no extra stat
                        kids.setdefault(encode(e.name), []).append(e.name)
                except OSError:
                    pass
    except OSError:
        kids = None
    _DIR_INDEX[base] = kids
    return kids


def decode_project_dir(name):
    """Turn ~/.claude/projects/<encoded> back into a real path.

    The encoding replaces BOTH "/" and "." with "-", so it is doubly lossy. A
    home directory named "first.last" encodes to "first-last", which is
    indistinguishable from two path segments, so a single token cannot be
    resolved in isolation.

    Rather than guess which character each "-" used to be, this runs the
    encoding forwards: it lists what is actually on disk, encodes each real
    directory name the same lossy way, and looks for an exact match. That is
    both cheaper and strictly more correct than guessing. The guessing version
    applied one joiner uniformly across a segment, so it resolved "my-repo" and
    "my.repo" but never "my-repo.git", which mixes the two. Any such folder,
    and there are many ("foo-bar.v2", "site-prod.worktree"), silently failed to
    decode and its sessions went missing from backfill.
    """
    if not name.startswith("-"):
        return ""
    parts = [p for p in name[1:].split("-") if p != ""]
    if not parts:
        return ""

    def encode(seg):
        """The encoder's own transform, applied to one real directory name.

        "." becomes "-", and empty tokens are dropped exactly as they were on
        the way in, so a leading dot and a run of dashes both collapse the same
        way here as they did in the encoded name.
        """
        return "-".join(t for t in seg.replace(".", "-").split("-") if t)

    def walk(base, i, depth=0):
        if i == len(parts):
            return base
        if depth > 24:
            return ""
        kids = _dir_index(base, encode)
        if kids is None:                   # unreadable dir is a dead end
            return ""
        # A real segment may span several tokens. Longest first, so
        # "my-repo" is preferred over "my" + "repo" when both exist.
        for j in range(min(len(parts), i + 8), i, -1):
            for name in kids.get("-".join(parts[i:j]), ()):
                hit = walk(os.path.join(base, name), j, depth + 1)
                if hit:
                    return hit
        return ""

    return walk("/", 0)


def transcript_only_candidates(known_ids):
    """Sessions that exist ONLY as a transcript on disk.

    history.jsonl is not a complete record: a session that never had a prompt
    logged to it leaves a transcript but no history entry, and those were
    invisible. Walking the projects directory finds them, and the transcript
    itself supplies the cwd and the first prompt.
    """
    out = []
    for f in glob.glob(os.path.join(R.PROJECTS, "*", "*.jsonl")):
        sid = os.path.basename(f)[:-6]
        if sid in known_ids:
            continue
        cwd = decode_project_dir(os.path.basename(os.path.dirname(f)))
        if not cwd or not os.path.isdir(cwd) or cwd.startswith(EPHEMERAL):
            continue
        label, first_ts, last_ts = _scan_transcript_ends(f)
        last_user = ""
        mt = os.path.getmtime(f)
        out.append({
            "session_id": sid, "cwd": cwd, "transcript_path": f,
            "last_prompt": label, "session_name": "",
            "last_seen": last_ts or mt, "started_at": first_ts or mt,
            "lifetime": max(0.0, (last_ts or mt) - (first_ts or mt)),
            "window_root": "", "sse_port": "", "pid": 0, "pid_start": "",
            # `/exit` and `/logout` are CLI commands: they never appear as
            # messages in the transcript, so scanning it finds nothing. The
            # deliberate-close evidence lives only in history.jsonl, so look
            # the session up there even though this path found it on disk.
            "_state": _closed_deliberately(sid, f, last_user),
            "_origin": "history",
        })
    return out


COMMAND_BLOCK_RE = re.compile(r"<command-name>\s*/(exit|logout|clear)\s*</command-name>")

# Bookkeeping the CLI writes around a command. None of it means the session
# carried on working, so none of it disqualifies a close.
# A user-typed message never begins with one of these. They are the CLI's own
# scaffolding: the goodbye line a command prints, background task chatter, and
# injected reminders. ab0bf35d ends with /exit, "Catch you later!", queue noise
# and a <task-notification>, and none of that is the session carrying on.
INERT_WRAPPER_RE = re.compile(
    r"\s*<(local-command-stdout|local-command-stderr|local-command-caveat|"
    r"task-notification|system-reminder|command-name|command-message|"
    r"command-args)\b")

_ENDS_CACHE = {}
_TAIL_BYTES = 256 * 1024      # verified equivalent to a full read on all 715

INERT_TYPES = frozenset((
    "system", "file-history-snapshot", "queue-operation", "attachment",
    "summary", "last-prompt",
))


def _transcript_ends_deliberately(path):
    """True only if the transcript's LAST command block is genuinely terminal.

    Presence anywhere is not enough, and assuming it was cost me a wrong rule:
    measured across 715 transcripts, 433 contain such a block but 41 of those
    have it early, and every early one is a `/clear` sitting near the top of a
    session that then ran for hundreds more lines. 46b138c6 has it at line 3
    of 437. Marking those EXITED would bury live work.

    So: find the last block, then require that nothing substantive follows it.
    A trailing `<local-command-stdout>Goodbye!</local-command-stdout>` and the
    CLI's own bookkeeping do not count as work.
    """
    # Reading every transcript in full cost 2.4s and 1.5 GB per dashboard load,
    # twice over, which is most of the 10s the page took. Only the END of a file
    # can hold a TERMINAL command block: one sitting earlier always has work
    # after it and is rejected anyway. So read a tail, and cache on mtime+size.
    try:
        st = os.stat(path)
    except OSError:
        return False
    key = (path, st.st_mtime, st.st_size)
    hit = _ENDS_CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        with open(path, "rb") as fh:
            if st.st_size > _TAIL_BYTES:
                fh.seek(-_TAIL_BYTES, os.SEEK_END)
                blob = fh.read().split(b"\n", 1)[-1]   # drop the partial line
            else:
                blob = fh.read()
        lines = [l for l in blob.decode("utf-8", "ignore").split("\n") if l.strip()]
    except OSError:
        return False
    verdict = _ends_deliberately_lines(lines)
    _ENDS_CACHE[path] = (key, verdict)
    return verdict


def _ends_deliberately_lines(lines):
    hits = [i for i, l in enumerate(lines) if COMMAND_BLOCK_RE.search(l)]
    if not hits:
        return False
    for l in lines[hits[-1] + 1:]:
        try:
            d = json.loads(l)
        except Exception:
            continue
        if d.get("type") in INERT_TYPES:
            continue
        c = (d.get("message") or {}).get("content")
        if isinstance(c, list):
            c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
        c = (c or "").strip()
        if not c or INERT_WRAPPER_RE.match(c):
            continue
        return False            # real content after the command: it carried on
    return True


def _ts(d):
    ts = d.get("timestamp")
    if isinstance(ts, str):
        try:
            return time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            return 0.0
    return ts or 0.0


_SCAN_CACHE = {}
_HEAD_BYTES = 128 * 1024


def _scan_transcript_ends(path):
    """First user message and the first/last timestamps, without reading it all.

    The previous loop json-parsed every line of every transcript: 1.5 GB and
    7.5s on each dashboard load, to extract a label and two numbers. The label
    is the FIRST user message and the timestamps are the first and last lines,
    so a head read and a tail read are sufficient.
    """
    try:
        st = os.stat(path)
    except OSError:
        return "", 0.0, 0.0
    key = (st.st_mtime, st.st_size)
    hit = _SCAN_CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]

    def parse(blob):
        out = []
        for line in blob.decode("utf-8", "ignore").split("\n"):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    # A fixed 128 KB head was wrong: on 36 of 715 transcripts the first user
    # message sits past it, behind large snapshots and attachments, and the
    # label came back empty. Stream from the start and stop the moment both
    # answers are in hand, which is the first line or two for almost every file
    # and only occasionally further.
    label, first_ts, last_ts = "", 0.0, 0.0
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                if not raw.strip():
                    continue
                try:
                    d = json.loads(raw.decode("utf-8", "ignore"))
                except Exception:
                    continue
                t = _ts(d)
                if t and not first_ts:
                    first_ts = t
                if not label and d.get("type") == "user":
                    c = (d.get("message") or {}).get("content")
                    if isinstance(c, str):
                        label = c.strip()[:160]
                    elif isinstance(c, list):
                        label = " ".join(x.get("text", "") for x in c
                                         if isinstance(x, dict)
                                         and x.get("type") == "text").strip()[:160]
                if label and first_ts:
                    break
            fh.seek(-min(_TAIL_BYTES, st.st_size), os.SEEK_END)
            for d in parse(fh.read().split(b"\n", 1)[-1] if st.st_size > _TAIL_BYTES
                           else fh.read()):
                last_ts = max(last_ts, _ts(d))
    except OSError:
        return "", 0.0, 0.0

    res = (label, first_ts, last_ts)
    _SCAN_CACHE[path] = (key, res)
    return res


def _closed_deliberately(sid, transcript_path="", last_user=""):
    """BACKFILL unless something says the session was closed on purpose.

    I previously claimed `/exit` is a CLI command that never reaches the
    transcript. That was wrong. It is written as a user message shaped like
      <command-name>/exit</command-name>
    and 433 of 715 transcripts on this machine (61%) contain one. The earlier
    scan missed it twice over: it read only the LAST user message, which is the
    `<local-command-stdout>Goodbye!</local-command-stdout>` that follows, and it
    matched `/exit` only at the start of a line.

    Order matters: the transcript is the primary source because it is the
    session's own record, and history.jsonl is the fallback.
    """
    if transcript_path and os.path.isfile(transcript_path):
        closed = _transcript_ends_deliberately(transcript_path)
        if closed:
            return "EXITED"
    disp = (history_index().get(sid) or {}).get("display") or ""
    for text in (disp, last_user or ""):
        if text and (EXIT_RE.search(text) or RETIRED_RE.search(text)):
            return "EXITED"
    return "BACKFILL"


def backfill_candidates(days=7, exclude_ids=None):
    reset_caches()
    """Sessions that predate the registry -- the ones already lost.

    The registry cannot know about these; it only starts recording once the
    hooks are installed. This path reconstructs them from history.jsonl.

    Your `/exit` observation is the safety filter here: a session whose last
    history entry is `/exit` was closed deliberately. Measured on this machine,
    64.5% of sessions end that way -- so it is a precise EXCLUSION, but its
    recall is only ~65%, which is exactly why it can never be the detector.
    """
    known = set(exclude_ids or ())
    for rec in R.all_live() + R.all_ended():
        known.add(rec.get("session_id"))
    cutoff = time.time() - days * 86400
    out = []
    for sid, e in history_index().items():
        ts, proj, disp = e["last"], e["project"], e["display"]
        label, name = e["label"], e["name"]
        if sid in known or ts < cutoff:
            continue
        # Deliberate exits used to be dropped outright, which meant a session
        # you finished on purpose simply vanished. Keep it and label it; the UI
        # filters by status, so nothing has to be hidden to stay useful.
        tp0 = transcript_for(sid)
        state = _closed_deliberately(sid, tp0 or "", disp or "")
        if not proj or not os.path.isdir(proj):
            continue                       # folder is gone -> cannot cd there
        if proj.startswith(EPHEMERAL):
            continue                       # scratch/temp dirs are not real work
        # Whether the transcript survived is a PROPERTY, not a state. A session
        # still ended the way it ended; losing the file does not change that.
        # An earlier build made LOST a fifth on-screen category, which is the
        # same mistake as STALE.
        tp = transcript_for(sid)
        out.append(_touch({
            "session_id": sid, "cwd": proj, "transcript_path": tp or "",
            "last_prompt": (label or disp)[:160], "last_seen": ts,
            "started_at": ts, "window_root": "", "pid": 0, "pid_start": "",
            "session_name": name, "lifetime": max(0.0, e["last"] - e["first"]),
            "_state": state, "_origin": "history",
        }))
    return out


def _pids_with_cwd(cwd):
    """Live claude pids whose working directory is cwd."""
    try:
        out = subprocess.run(["ps", "-Ao", "pid=,comm="], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return []
    hits = []
    for line in out.splitlines():
        pid, _, comm = line.strip().partition(" ")
        if "claude" not in comm.lower():
            continue
        try:
            lo = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                                capture_output=True, text=True, timeout=5).stdout
        except Exception:
            continue
        for l in lo.splitlines():
            if l.startswith("n") and l[1:] == cwd:
                hits.append(int(pid))
    return hits


def running_sessions():
    """Sessions alive right now, so the dashboard can SHOW them.

    Hiding them was correct (restoring a live session would run two claudes
    against one transcript) but confusing: a window you still have open simply
    vanished from the list. Chrome shows every tab and greys out the ones it
    will not touch, so we do the same.
    """
    out, seen = [], set()
    boot = R.boot_time()
    for rec in R.all_live():
        if not R.transcript_ok(rec):
            continue
        if R.classify(rec, boot=boot) == "RUNNING":
            rec["_state"] = "RUNNING"
            rec["_origin"] = "registry"
            rec["host_app"] = rec.get("host_app") or R.host_app(rec.get("pid"))
            out.append(rec)
            seen.add(rec.get("cwd"))
    # A session started before the hooks existed has no record, but its claude
    # process is still on screen. Recover its identity from history.
    idx = history_index()
    for cwd in live_claude_cwds():
        if cwd in seen:
            continue
        best = None
        for sid, e in idx.items():
            if e["project"] == cwd and (best is None or e["last"] > best[1]):
                best = (sid, e["last"], e["label"] or e["display"], e["name"],
                        max(0.0, e["last"] - e["first"]))
        if not best:
            continue
        host, livepid = "unknown", 0
        for pid_ in _pids_with_cwd(cwd):
            host, livepid = R.host_app(pid_), pid_
            break
        out.append({"host_app": host, "live_pid": livepid,
                    "session_id": best[0], "cwd": cwd,
                    "transcript_path": transcript_for(best[0]),
                    "last_prompt": (best[2] or "")[:160], "session_name": best[3],
                    "lifetime": best[4], "last_seen": best[1],
                    "started_at": best[1], "window_root": "", "sse_port": "",
                    "pid": 0, "pid_start": "", "_state": "RUNNING",
                    "_origin": "live"})
    return out


def candidates(include_backfill=True, days=7, only_restorable=True):
    """Every known session, or just the restorable ones.

    The dashboard asks for everything and filters by status client-side, so a
    session you exited on purpose is still visible if you go looking for it.
    The CLI keeps the restorable-only default.
    """
    reg = registry_candidates()
    out = [c for c in reg if not only_restorable or c["_state"] in R.RESTORABLE]
    seen = {c["session_id"] for c in out}
    # Only the session that is ACTUALLY running is off limits. Its siblings in
    # the same folder are still recoverable.
    live_ids = {c["session_id"] for c in running_sessions()}
    if include_backfill:
        extra = transcript_only_candidates(set(history_index()) | seen | live_ids)
        for c in backfill_candidates(days, exclude_ids=live_ids) + extra:
            # One gate decides this now. The old form asked RESTORABLE and then
            # re-admitted BACKFILL separately, which is how my own preview once
            # reported 1 restorable session when there were 152.
            if only_restorable and not R.view(c)["restorable"]:
                continue
            # Ask the one gate, not transcript_ok directly: LOST is offerable
            # (you can find it) without being restorable (nothing to resume).
            if c["session_id"] not in seen and R.view(c)["offerable"]:
                out.append(c)
                seen.add(c["session_id"])
    # newest first; dedupe identical cwd keeping the newest
    out.sort(key=lambda c: c.get("last_seen", 0), reverse=True)
    return out


# ------------------------------------------------------------------ restore
def group_key(c):
    """Chrome-style window restoration.

    Chrome remembers which tabs shared a window and rebuilds that structure; it
    does not scatter every tab into its own window. Same idea here:

      1. sse_port is the TRUE window identity. Every Cursor window runs its own
         Claude extension server on its own port (verified: three windows, one
         shared Cursor pid, three distinct ports). Sessions that shared a port
         shared a window, so they go back into one window together.
      2. No port recorded -> fall back to the workspace root.
      3. Nothing recorded (backfill) -> ONE shared window, never N windows.
         Unknown structure means do the least surprising thing.
    """
    # A session lives in an APP first, a window second. A session that was not
    # in Cursor must never be merged into a Cursor window group: verified that
    # a long-lived note-vault session descends from a launchd-owned pty
    # wrapper, not Cursor at all.
    host = R.effective_host(c) or ""
    if host and host not in ("Cursor", "unknown", ""):
        return "app:%s" % host
    if c.get("sse_port"):
        return "port:%s" % c["sse_port"]
    if c.get("window_root"):
        return "root:%s" % c["window_root"]
    return "shared"


TICKETS = os.path.join(R.ROOT, "tickets")
TICKET_MAX_AGE = 300.0          # 5 minutes: a ticket nobody collected is stale


def write_tickets(root, items):
    """One ticket per session, for hosts whose terminal opens with no arguments.

    Cursor can be told what to run because the extension creates the terminal.
    An Obsidian terminal is opened by a command that takes no parameters, so
    the terminal has to work out which session it is AFTER it starts. A ticket
    per session, claimed atomically by whichever shell gets there first, does
    that without any shared counter.
    """
    os.makedirs(TICKETS, exist_ok=True)
    made = []
    for c in items:
        p = os.path.join(TICKETS, "%s.json" % c["session_id"])
        tmp = p + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"root": root, "cwd": c["cwd"],
                       "sid": c["session_id"], "at": time.time()}, fh)
        os.replace(tmp, p)
        made.append(p)
    return made


def claim_ticket(root):
    """Atomically take one ticket for this root. Returns the ticket or None.

    os.rename is atomic on the same filesystem, so exactly one caller can win a
    given ticket no matter how many shells start at once. Losers move on to the
    next file rather than retrying, which is what keeps two terminals from
    resuming the same session.
    """
    try:
        names = sorted(os.listdir(TICKETS))
    except OSError:
        return None
    now = time.time()
    for n in names:
        if not n.endswith(".json"):
            continue
        p = os.path.join(TICKETS, n)
        try:
            with open(p) as fh:
                t = json.load(fh)
        except Exception:
            continue
        if t.get("root") != root:
            continue
        if now - (t.get("at") or 0) > TICKET_MAX_AGE:
            try:
                os.unlink(p)
            except OSError:
                pass
            continue
        taken = p + ".taken"
        try:
            os.rename(p, taken)          # atomic: only one caller succeeds
        except OSError:
            continue
        try:
            os.unlink(taken)
        except OSError:
            pass
        return t
    return None


def _resume_cmd(c):
    """The shell line that brings one session back, with the Claude markers
    stripped. Without the unset, a terminal launched from inside a Claude Code
    session inherits CLAUDE_CODE_CHILD_SESSION=1 and the resumed session records
    NOTHING, so it could never be revived again."""
    unset = " ".join(CLAUDE_ENV_MARKERS)
    return "unset %s; cd %s && claude --resume %s" % (
        unset, shlex_quote(c["cwd"]), c["session_id"])


def _resume_script(c):
    """Write the resume as a tiny script and run THAT.

    `do script` echoes whatever it is given, so passing the env scrub inline
    printed a full screen of `unset CLAUDE_CODE_... TERM_PROGRAM ...` before
    anything useful appeared. A one-line invocation keeps the terminal clean
    while the scrub still happens.
    """
    d = os.path.join(R.ROOT, "resume")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "%s.sh" % c["session_id"])
    with open(p, "w") as fh:
        fh.write("#!/bin/zsh\n"
                 "unset %s\n"
                 "cd %s || exit 1\n"
                 "exec claude --resume %s\n"
                 % (" ".join(CLAUDE_ENV_MARKERS), shlex_quote(c["cwd"]),
                    c["session_id"]))
    os.chmod(p, 0o755)
    return p


def restore_terminal_app(items):
    """Terminal.app really is scriptable: `do script` opens a tab and runs it.

    It was being handed the clipboard instead, which is why "I can't revive a
    session within a terminal". No accessibility permission is needed; verified
    by running a throwaway `do script` that returned `tab 1 of window id 2813`.
    """
    done = []
    for c in items:
        cmd = _resume_script(c)
        script = 'tell application "Terminal"\n activate\n do script "%s"\nend tell' % (
            cmd.replace("\\", "\\\\").replace('"', '\\"'))
        r = subprocess.run(["osascript", "-e", script], capture_output=True,
                           text=True, env=clean_env())
        if r.returncode == 0:
            done.append(c["session_id"])
    return done


def restore_tmux(items):
    """tmux needs no app at all: one window per session inside a `revive` session."""
    done = []
    have = subprocess.run(["tmux", "has-session", "-t", "revive"],
                          capture_output=True, env=clean_env()).returncode == 0
    for c in items:
        cmd = _resume_cmd(c)
        if not have:
            r = subprocess.run(["tmux", "new-session", "-d", "-s", "revive",
                                "-c", c["cwd"], cmd],
                               capture_output=True, env=clean_env())
            have = r.returncode == 0
        else:
            r = subprocess.run(["tmux", "new-window", "-t", "revive",
                                "-c", c["cwd"], cmd],
                               capture_output=True, env=clean_env())
        if r.returncode == 0:
            done.append(c["session_id"])
    return done


ADAPTERS = {"Terminal": restore_terminal_app, "tmux": restore_tmux}


def handoff(host, items):
    """Deliver sessions that belong to an app we cannot drive.

    Cursor is scriptable because I wrote an extension for it. Obsidian's
    terminal comes from a third-party plugin whose command surface is generated
    at runtime inside 1.9 MB of minified code, so there is no reliable way to
    open a terminal in it from outside. The honest failure is to bring that app
    forward and hand over the exact commands, NOT to silently put the session
    somewhere it does not belong.
    """
    # Obsidian is drivable now: the bridge plugin claims a job for its vault
    # and opens one terminal per session. Everything else still falls back to
    # bringing the app forward with the commands on the clipboard.
    if host == "Obsidian":
        vaults = R.obsidian_can_host_a_terminal()
        if vaults:
            os.makedirs(os.path.join(R.ROOT, "pending"), exist_ok=True)
            for n, (root, group) in enumerate(
                    _group_by_vault(items, vaults).items()):
                job = os.path.join(R.ROOT, "pending", "obsidian-%d-%d.json"
                                   % (int(time.time() * 1000), n))
                with open(job, "w") as fh:
                    json.dump({"root": os.path.realpath(root),
                               "at": time.time() * 1000,
                               "items": [{"name": os.path.basename(
                                              c["cwd"].rstrip("/")) or "revive",
                                          "cwd": c["cwd"],
                                          "sid": c["session_id"]}
                                         for c in group]}, fh)
            open_app_clean(host)
            return None, ["queued %d session(s) for Obsidian" % len(items)]

    # Hosts we can actually drive get driven. Only the rest fall back to
    # bringing the app forward with the commands on the clipboard.
    fn = ADAPTERS.get(host)
    if fn:
        started = fn(items)
        return None, ["started %d/%d session(s) in %s"
                      % (len(started), len(items), host)]

    lines = ["cd %s && claude --resume %s" % (shlex_quote(c["cwd"]),
                                              c["session_id"]) for c in items]
    path = os.path.join(R.ROOT, "handoff-%s.txt" % re.sub(r"\W+", "-", host).lower())
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    try:
        pr = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        pr.communicate(("\n".join(lines) + "\n").encode())
    except Exception:
        pass
    subprocess.run(["open", "-a", host], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return path, lines


# Anything that marks this process as living inside a Claude Code session. An
# app launched with these inherited will hand them to every shell it spawns,
# and CLAUDE_CODE_CHILD_SESSION in particular disables transcript saving.
CLAUDE_ENV_MARKERS = (
    "CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_BRIDGE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PID", "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_DISABLE_TERMINAL_TITLE", "CLAUDE_EFFORT",
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS",
    "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "VSCODE_NONCE",
    "VSCODE_GIT_ASKPASS_MAIN", "VSCODE_GIT_ASKPASS_NODE",
    "VSCODE_GIT_ASKPASS_EXTRA_ARGS", "VSCODE_GIT_IPC_HANDLE",
    "VSCODE_GIT_IPC_AUTH_TOKEN",
)


def clean_env():
    return {k: v for k, v in os.environ.items() if k not in CLAUDE_ENV_MARKERS}


def open_app_clean(app):
    """Launch an app WITHOUT this session's Claude Code environment.

    Measured: `open -a Obsidian` from inside a Claude Code session gave Obsidian
    the whole marker set, and Obsidian handed it to every terminal it spawned
    thereafter, not only the revived one. The app is the leak point, so the app
    has to be started clean.
    """
    return subprocess.run(["open", "-a", app], check=False, env=clean_env(),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _group_by_vault(items, vaults):
    """Which vault each session belongs to.

    A session's cwd is usually inside the vault, but not always: you can open a
    terminal in Obsidian and cd anywhere. Longest matching vault wins; anything
    outside every vault goes to the first one, because the terminal has to open
    somewhere and the wrapper cd's to the real cwd regardless.
    """
    groups = {}
    for c in items:
        cwd = os.path.realpath(c.get("cwd") or "")
        best, blen = None, -1
        for v in vaults:
            rv = os.path.realpath(v)
            if (cwd == rv or cwd.startswith(rv + os.sep)) and len(rv) > blen:
                best, blen = v, len(rv)
        groups.setdefault(best or vaults[0], []).append(c)
    return groups


def shlex_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def group_root(key, items):
    """Which folder the restored window should be rooted at."""
    for c in items:
        if c.get("window_root") and os.path.isdir(c["window_root"]):
            return c["window_root"]
    dirs = [c.get("cwd") for c in items if c.get("cwd") and os.path.isdir(c["cwd"])]
    if not dirs:
        return os.path.expanduser("~")
    if len(dirs) == 1:
        return dirs[0]
    common = os.path.commonpath(dirs)          # one window holding them all
    return common if os.path.isdir(common) else dirs[0]


def sh_quote(v):
    return "'" + str(v).replace("'", "'\\''") + "'"


def is_safe_abs(p):
    return bool(p) and p.startswith("/") and not re.search(r"[\0\n\r`$]", p)


def open_window_roots():
    """Workspace roots of every Cursor window open right now."""
    roots = []
    for lock in glob.glob(os.path.join(R.IDE_DIR, "*.lock")):
        d = R.load(lock) or {}
        if not R.pid_alive(d.get("pid")):
            continue
        for f in (d.get("workspaceFolders") or []):
            if f and f not in roots:
                roots.append(f)
    return roots


def current_window_root():
    """The window that launched revive, resolved from our own SSE port."""
    port = os.environ.get("CLAUDE_CODE_SSE_PORT", "")
    if port:
        info = R.window_info(port)
        if info.get("window_root"):
            return info["window_root"]
    roots = open_window_roots()
    return roots[0] if roots else ""


def print_restore_result(res):
    """Report a restore on the terminal.

    Both CLI callers used to unpack this as `for root, ws, n in restore(...)`,
    a three-tuple restore() stopped returning when it began reporting per
    session id instead of per folder. The work still happened and then the
    command died with `ValueError: too many values to unpack`, so `revive.py
    restore` and `revive.py pick` both ended in a traceback. Only the dashboard
    read the dict correctly, which is why it went unseen: nothing exercised the
    CLI restore path end to end.
    """
    for w in res.get("windows", []):
        print("restored %d session(s) into %s" % (w["count"], short(w["root"], 60)))
    for h in res.get("handed_off", []):
        print("handed to %s: %s" % (h["host"], h["id"][:8]))
    for f in res.get("failed", []):
        print("NOT restored: %s (%s)" % (f["id"][:8], f["reason"]))
    n = len(res.get("started", []))
    if n:
        print("%d session(s) confirmed resumed." % n)
    elif not res.get("handed_off"):
        print("Nothing was resumed.")


def restore(items, dry=False):
    """Reopen sessions as editor-area terminals, in the window they came from.

    Chrome rebuilds the window structure it recorded rather than scattering
    tabs, and so do we. Sessions are grouped by window identity (sse_port, then
    workspace root); each group targets one Cursor window.

    Delivery is a job file per window, not a cursor:// URI: a URI is handed to
    whichever window owns the handler, which is fine for one window and wrong
    for several. Each window claims only jobs naming its own root, so the
    routing is deterministic. Windows that are not open are opened first.

    Explicitly NOT reintroduced from the first attempt: no VS Code tasks (they
    are pinned to the bottom panel), and no workspace rooted at $HOME.
    """
    groups = {}
    for c in items:
        cwd = c.get("cwd") or ""
        if not is_safe_abs(cwd) or not os.path.isdir(cwd):
            continue
        groups.setdefault(group_key(c), []).append(c)
    if not groups:
        # Same shape on every path. Returning a bare [] here is what let two
        # callers disagree about the return type in the first place.
        return {"requested": sorted(c["session_id"] for c in items),
                "started": [], "failed": [], "handed_off": [],
                "items": [], "windows": []}

    here = current_window_root()
    pending_dir = os.path.join(R.ROOT, "pending")
    os.makedirs(pending_dir, exist_ok=True)
    open_roots = open_window_roots()

    plan, made, elsewhere = [], [], []
    for key, its in groups.items():
        # A group that belongs to another APP must never be delivered into
        # Cursor. Grouping already separated it; delivery used to merge it back
        # by handing every group to `cursor --new-window`, which is how vault
        # sessions ended up as Cursor tabs.
        if key.startswith("app:"):
            elsewhere.append((key[4:], its))
            continue
        # A group with no recorded window belongs to the window you are in;
        # inventing a root for it is what produced the $HOME workspace before.
        root = here if key == "shared" else group_root(key, its)
        if not root or not os.path.isdir(root):
            root = here or group_root(key, its)
        payload = [{"name": (c.get("session_name") or "").strip()
                            or os.path.basename((c["cwd"]).rstrip("/")) or "revive",
                    "cwd": c["cwd"], "sid": c["session_id"]} for c in its]
        plan.append((root, payload))
        made += [(c["cwd"], p["name"], 1) for c, p in zip(its, payload)]

    if dry:
        return {"requested": sorted(c["session_id"] for c in items),
                "started": [], "failed": [],
                "handed_off": [{"host": h, "id": c["session_id"]}
                               for h, its in elsewhere for c in its],
                "items": [{"cwd": p["cwd"], "name": p["name"], "sid": p["sid"]}
                          for _r, pl in plan for p in pl],
                "windows": [{"root": r, "count": len(pl)} for r, pl in plan]}

    for host, its in elsewhere:
        for c in its:
            R.record_placement(c["session_id"], host)
        handoff(host, its)

    for root, payload in plan:
        for p in payload:
            R.record_placement(p["sid"], "Cursor")
        job = os.path.join(pending_dir, "%s.json" % abs(hash((root, len(payload),
                                                              time.time()))))
        with open(job, "w") as fh:
            json.dump({"root": root, "at": time.time() * 1000,
                       "items": payload}, fh)
        if root not in open_roots:
            # clean_env, or the NEW WINDOW inherits this session's Claude
            # markers and every terminal it opens is born with transcript
            # saving disabled.
            subprocess.run([CURSOR_CLI, "--new-window", root], check=False,
                           env=clean_env(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            open_roots.append(root)

    # Verify PER SESSION, not per folder. Keying on cwd was near-useless here:
    # 145 of 152 restorable sessions share a folder with another, so "a claude
    # appeared in that directory" said nothing about which one started.
    # A resumed session fires SessionStart, and the hook writes a live record
    # under its own id, so that record IS the proof it came back.
    # Sessions handed to an adapter were never verified: `want` covered only the
    # Cursor plan, so tmux and Terminal restores were reported from the
    # LAUNCHER's exit code. Measured: `tmux new-session` returned 0, the resumed
    # claude exited at once ("No conversation found with session ID", because
    # the transcript held no assistant turn), tmux tore the empty session down,
    # and restore still reported "started 1/1 session(s) in tmux".
    #
    # Verify every session by the same evidence: a session that really came back
    # fires SessionStart and writes its own live record. Nothing else survives.
    want = {p["sid"] for _r, pl in plan for p in pl}
    want |= {c["session_id"] for _h, its in elsewhere for c in its}
    started = set()
    for _ in range(40):                       # windows take a moment to boot
        live = {r.get("session_id") for r in R.all_live()
                if R.classify(r) == "RUNNING"}
        started = want & live
        if started == want:
            break
        time.sleep(0.5)

    for c in items:
        if c["session_id"] not in started:
            continue
        p_ = c.get("_path")
        if p_ and os.path.exists(p_):
            rec = R.load(p_) or {}
            rec["restored_at"] = time.time()
            R.save(p_, rec)
    # Audit finding #8. Verification was already per session, then the RESULT
    # collapsed back to cwd: `m[0] in started_cwds`. With 145 of 152 restorable
    # sessions sharing a folder, two sessions in one folder where only one came
    # back reported BOTH as restored. Report by session id, which is the only
    # thing that was ever verified.
    return {"requested": sorted(want),
            "started": sorted(started),
            "failed": [{"id": sid, "reason": "resume_not_observed"}
                       for sid in sorted(want - started)],
            "handed_off": [{"host": h, "id": c["session_id"]}
                           for h, its in elsewhere for c in its],
            "items": [{"cwd": p["cwd"], "name": p["name"], "sid": p["sid"]}
                      for _r, pl in plan for p in pl],
            "windows": [{"root": r, "count": len(pl)} for r, pl in plan]}


# --------------------------------------------------------------------- view
STATE_NOTE = {
    "CRASHED":    "Cursor died under it",
    "TERMINATED": "hung up or killed, not a deliberate exit",
    "BACKFILL":   "no registry record; reconstructed from history",
}


def cmd_list(days=7):
    cs = candidates(days=days)
    if not cs:
        print("Nothing to revive. No crashed or orphaned sessions found.")
        return
    print("%d session(s) look recoverable:\n" % len(cs))
    cur = None
    for i, c in enumerate(cs, 1):
        g = group_key(c)
        if g != cur:
            cur = g
            print("  window: %s" % short(g, 60))
        print("   %2d. %-20s %-5s %-38s %s" % (
            i, os.path.basename((c.get("cwd") or "").rstrip("/"))[:20],
            ago(c.get("last_seen")), (c.get("last_prompt") or "")[:38],
            STATE_NOTE.get(c["_state"], c["_state"])))
    print("\n  revive.py pick        choose interactively")
    print("  revive.py restore <session-id> ...")


def extension_versions():
    """(installed on disk, version Cursor is actually executing).

    Cursor loads extension code once per window, so an install is not enough:
    the running window keeps the old build until it is reloaded. Comparing the
    two is the only reliable way to know whether a fix is live.
    """
    installed = ""
    for f in glob.glob(os.path.expanduser(
            "~/.cursor/extensions/*revive-browser*/package.json")):
        try:
            installed = json.load(open(f)).get("version", "")
        except Exception:
            pass
    # Every window appends to ONE log, so taking the last line reports whatever
    # window logged most recently, not this one. That produced a confident and
    # WRONG "MATCH" while this window was three versions behind. Read the last
    # `activated` line for THIS window's workspace instead: activation is the
    # moment a window loads extension code, so it names the build it is running.
    root = current_window_root() or ""
    running, when = "", ""
    fallback = ("", "")
    log = os.path.join(R.ROOT, "extension.log")
    try:
        for line in open(log, errors="ignore"):
            m = re.search(r"^(\S+)\s+v(\d+\.\d+\.\d+)\s+activated, workspace=(.*)$",
                          line.rstrip("\n"))
            if not m:
                continue
            fallback = (m.group(2), m.group(1))
            if root and os.path.realpath(m.group(3).strip()) == os.path.realpath(root):
                running, when = m.group(2), m.group(1)
    except OSError:
        pass
    if not running:                       # no activation seen for this window
        running, when = fallback
    return installed, running, when


def cmd_version():
    inst, run, when = extension_versions()
    print("extension on disk        : %s" % (inst or "not installed"))
    print("version Cursor is running: %s%s" % (
        run or "unknown (never activated)",
        "   (last seen %s)" % when[:19].replace("T", " ") if when else ""))
    if inst and run and inst == run:
        print("\nMATCH. Cursor is running the current build.")
    elif inst and run:
        print("\nSTALE. Cursor still runs v%s. Reload the window:" % run)
        print("  Cmd+Shift+P -> Developer: Reload Window")
    else:
        print("\nUNKNOWN. Reload the window, then run this again.")
    return inst == run and bool(inst)


def cmd_doctor():
    cmd_version()
    print()
    print("registry root : %s" % R.ROOT)
    print("live records  : %d" % len(R.all_live()))
    print("ended records : %d" % len(R.all_ended()))
    boot = R.boot_time()
    print("last boot     : %s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(boot)))
    counts = {}
    for rec in R.all_live() + R.all_ended():
        s = R.classify(rec, boot=boot)
        counts[s] = counts.get(s, 0) + 1
    for k in sorted(counts):
        print("  %-11s %d" % (k, counts[k]))
    ports = glob.glob(os.path.join(R.IDE_DIR, "*.lock"))
    print("open Cursor windows (ide locks): %d" % len(ports))
    for p in ports:
        d = R.load(p) or {}
        print("  port %-6s pid %-7s alive=%-5s %s" % (
            os.path.basename(p).split(".")[0], d.get("pid"),
            R.pid_alive(d.get("pid")), (d.get("workspaceFolders") or [""])[0]))


CURSOR_CLI = "/Applications/Cursor.app/Contents/Resources/app/bin/cursor"
def _ext_installed():
    """Is the companion extension present?

    Installing it as a .vsix renames the folder to
    local.revive-browser-<version>, so a hardcoded path silently reports
    "not installed" and restore refuses to run. Glob instead.
    """
    return bool(glob.glob(os.path.expanduser(
        "~/.cursor/extensions/*revive-browser*/package.json")))


def _dead(pid):
    """Is this pid really gone?

    `os.kill(pid, 0)` is not enough: a killed child stays as a ZOMBIE until its
    parent reaps it, and signalling a zombie still succeeds. That made the sweep
    report an empty kill list while the process had in fact died, which is the
    kind of wrong report that hides a working fix.
    """
    try:
        st = subprocess.run(["ps", "-o", "state=", "-p", str(pid)],
                            capture_output=True, text=True, timeout=4).stdout.strip()
    except Exception:
        return False
    return (not st) or st.startswith("Z")


def _listen_port(pid):
    """The TCP port this pid is listening on, or 0."""
    try:
        out = subprocess.run(["lsof", "-nP", "-a", "-p", str(pid),
                              "-iTCP", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=6).stdout
    except Exception:
        return 0
    m = re.search(r":(\d+)\s+\(LISTEN\)", out)
    return int(m.group(1)) if m else 0


def _answers(port):
    """Does a dashboard on this port still serve its page?"""
    if not port:
        return False
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=1.5)
        return True
    except Exception:
        return False


def sweep_stray_dashboards(keep_pid=None):
    """Reap dashboards that are DEAD, and only those.

    The first version killed every serve.py that was not the one named in the
    endpoint file. That is wrong the moment /revive is run from two terminals:
    the other server may be perfectly alive with somebody's tab open on it, and
    only one process can be named in a single endpoint file.

    So the test is behavioural, not identity. A stray that still answers its
    page is left alone. One that is alive but no longer serving, or never bound
    a port at all, is wedged and gets reaped. A process that is already gone
    needs nothing.
    """
    me = os.path.join(os.path.dirname(os.path.abspath(__file__)), "serve.py")
    killed, spared = [], []
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="], timeout=6,
                             capture_output=True, text=True).stdout
    except Exception:
        return killed
    for line in out.splitlines():
        pid, _, argv = line.strip().partition(" ")
        if me not in argv:
            continue
        try:
            pid = int(pid)
        except ValueError:
            continue
        if keep_pid and pid == keep_pid:
            continue
        port = _listen_port(pid)
        if _answers(port):
            spared.append((pid, port))          # someone may be looking at it
            continue
        # Escalate. A genuinely wedged process may never get round to handling
        # SIGTERM; measured with a SIGSTOPped server, which survived TERM and
        # needed KILL. SIGCONT first so a stopped process can actually die.
        try:
            os.kill(pid, signal.SIGCONT)
        except Exception:
            pass
        gone = False
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except Exception:
                gone = True
                break
            for _ in range(8):
                time.sleep(0.2)
                if _dead(pid):
                    gone = True
                    break
            if gone:
                break
        if gone:
            killed.append(pid)
    return killed


def _reap_dashboard(pid):
    """Kill a dashboard that is alive but no longer answering. True if it died.

    Only ever called with the pid out of our own 0600 endpoint file, and even
    then the argv is checked: pids get recycled, and port 3000 is popular
    enough that killing on "something is on the port" would take out somebody
    else's dev server.
    """
    try:
        argv = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=5).stdout.decode("utf-8", "replace")
    except Exception:
        return False
    if "serve.py" not in argv:
        return False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except Exception:
            return True                        # gone between check and signal
        for _ in range(12):
            time.sleep(0.25)
            try:
                os.kill(pid, 0)
            except Exception:
                return True
    return False


def open_dashboard_in_this_window(url):
    """Ask THIS window to show the dashboard, by workspace root.

    A cursor:// URI is addressed to the application, not to a window, so the
    window that happens to own the handler answers it. Verified: /revive run in
    one window opened the dashboard in another. A job file names the root, and
    only the window with that root claims it.
    """
    root = current_window_root()
    if not root:
        return None
    d = os.path.join(R.ROOT, "pending")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "browser-%d.json" % int(time.time() * 1000))
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"root": root, "url": url, "at": time.time() * 1000}, fh)
    os.replace(tmp, p)
    return p


def cmd_ui(days=7, port=0):
    """Serve the dashboard and open it as a tab inside Cursor.

    Cursor has no CLI route to its Simple Browser: `--open-url` ignores http
    URLs and command: URIs only fire from trusted webviews (both verified).
    The companion extension registers a cursor:// URI handler, which is the one
    sanctioned path. Without it we fall back to the default browser.
    """
    import urllib.request
    here = os.path.dirname(os.path.abspath(__file__))

    # Nothing else reaps a dashboard. A clean close POSTs /api/shutdown, which
    # unlinks the endpoint file and exits; a SIGKILL or a hard crash leaves the
    # file behind naming a dead pid, and a wedged server keeps the port. So the
    # three states are settled here: reuse one that answers, reap one that is
    # alive but no longer serving, ignore one that is already gone.
    endpoint = os.path.join(R.ROOT, "dashboard.json")
    try:
        with open(endpoint) as fh:
            d = json.load(fh)
    except Exception:
        d = None
    if d:
        stale = True
        try:
            os.kill(int(d["pid"]), 0)          # signal 0 = liveness probe only
        except Exception:
            pass                                # already gone; just clear up
        else:
            live = "http://127.0.0.1:%d/?t=%s" % (d["port"], d["token"])
            try:
                urllib.request.urlopen(live, timeout=1.0)
                sweep_stray_dashboards(keep_pid=int(d["pid"]))
                _ui_open(live)                  # healthy: reuse, do not respawn
                return None
            except Exception:
                stale = _reap_dashboard(int(d["pid"]))
        # Deliberately NOT deleted. serve.py reads this file to reclaim the
        # SAME port and token, which is the only thing that keeps an already
        # open tab working across a restart. Deleting it here is what made every
        # refresh fail with "connection refused": the next server came up on a
        # new port with a new token, orphaning the URL in the browser.
        # Staleness is handled by serve.py probing whether the port is free.


    # 0 = let the OS pick. A fixed 3000 collided with anything else already on
    # that very popular port, and the URL is read back from the endpoint file
    # anyway, so nothing depends on the number being predictable.
    env = dict(os.environ, REVIVE_PORT=str(port), REVIVE_DAYS=str(days))
    # start_new_session puts the server in its OWN process group and detaches it
    # from the controlling terminal. Without it the dashboard was a child of the
    # shell that ran /revive, so it died with that shell or when its process
    # group was signalled, and the browser tab then showed "connection refused"
    # on the next refresh. The dashboard has to outlive the command that started
    # it; that is the whole point of it being a server.
    # Also sweep here: a wedged dashboard should be reaped whether we reuse a
    # healthy one or start a fresh one. Live strays are left alone.
    sweep_stray_dashboards()
    # Keep the server's own output. It has died twice with no explanation, and
    # DEVNULL threw away the only evidence of why.
    # 0600, like dashboard.json. It was world readable while it carried the
    # capability token on every line; the token is redacted now, but a log of
    # what the user is doing is still nobody else's business.
    _logp = os.path.join(R.ROOT, "dashboard.log")
    _fd = os.open(_logp, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(_fd, 0o600)            # tighten a file that already existed
    except OSError:
        pass
    logf = os.fdopen(_fd, "a", buffering=1)
    logf.write("\n--- spawn %s ---\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    proc = subprocess.Popen([sys.executable, os.path.join(here, "serve.py")],
                            env=env, stdout=logf, stderr=logf,
                            start_new_session=True)
    # The server now picks an ephemeral port and mints a token, publishing
    # both to dashboard.json. Assuming :3000 is what made the endpoint
    # trivially reachable by anything else on the machine.
    # dashboard.json outlives the run that wrote it, so a read landing before
    # the new server rewrites it yields the PREVIOUS run's port and token: a
    # URL pointing at a socket nobody is listening on. Accept the file only
    # once it names the pid we just spawned.
    url = None
    for _ in range(40):
        try:
            with open(os.path.join(R.ROOT, "dashboard.json")) as fh:
                d = json.load(fh)
            if d.get("pid") != proc.pid:
                raise ValueError("endpoint file still belongs to an older run")
            url = "http://127.0.0.1:%d/?t=%s" % (d["port"], d["token"])
            break
        except Exception:
            time.sleep(0.25)
    if url is None:
        url = "http://127.0.0.1:%d" % port
    for _ in range(40):                       # wait for the socket to accept
        try:
            urllib.request.urlopen(url, timeout=0.5)
            break
        except Exception:
            time.sleep(0.15)
    _ui_open(url)
    return proc


def in_cursor_terminal():
    """Are we running inside a Cursor terminal right now?

    CLAUDE_CODE_SSE_PORT is injected into the environment of every terminal a
    Cursor window opens, and nowhere else. Without it we are in Terminal.app,
    tmux, Obsidian or an ssh session, and the dashboard belongs in a real
    browser. `current_window_root()` alone is not the test: it falls back to
    ANY open Cursor window, so running /revive from Terminal would have thrown
    the dashboard into somebody else's Cursor window.
    """
    port = os.environ.get("CLAUDE_CODE_SSE_PORT", "")
    if not port:
        return False
    return bool(R.window_info(port).get("window_root"))


def cursor_target_port():
    """The Cursor window the dashboard should open in, or "".

    Normally this is our own window: CLAUDE_CODE_SSE_PORT is injected into every
    terminal a Cursor window opens, and window_info confirms it is real.

    The awkward case is a port that is set but dead. A window reload replaces
    the extension host and its server moves to a new port, while every terminal
    already open keeps the OLD value in its environment forever, because a
    process's environment cannot be updated from outside. So a long-lived
    session ends up naming a window that no longer exists, and the dashboard
    silently went to the browser instead.

    A dead port still tells us something a missing one does not: we WERE in a
    Cursor terminal. If exactly one Cursor window is alive, there is no
    ambiguity about which one meant, so use it. With several open we would be
    guessing at somebody else's window, which is the mistake the browser
    fallback exists to prevent, so stop.
    """
    port = os.environ.get("CLAUDE_CODE_SSE_PORT", "")
    if not port:
        return ""                      # Terminal, tmux, Obsidian, ssh
    if R.window_info(port).get("window_root"):
        return port                    # our own window, still alive
    live = R.live_cursor_windows()
    return live[0] if len(live) == 1 else ""


def _ui_open(url):
    """Hand the dashboard URL to Cursor, or to the default browser."""
    # REVIVE_NO_OPEN exists for the clean-room test, which has to run `ui` for
    # real (that is the only path that creates dashboard.log, and so the only
    # one that can prove the token never reaches it) without throwing a browser
    # window at whoever is running the suite.
    if os.environ.get("REVIVE_NO_OPEN"):
        print("Revive dashboard: %s (not opened, REVIVE_NO_OPEN set)" % url)
        return

    mode = R.open_mode()
    if mode == "browser":
        subprocess.run(["open", url], check=False)
        print("Revive dashboard: %s (browser, by your setting)" % url)
        print("Pick the sessions to bring back, then click Restore.")
        return

    target = cursor_target_port()
    if mode == "cursor" and not target and _ext_installed():
        # Asked for Cursor and we cannot prove which window: say so rather than
        # opening a browser the user has explicitly opted out of.
        print("Revive dashboard: %s" % url)
        print("Could not identify a live Cursor window to open it in "
              "(%d listening). Paste the URL into Cursor, or run:"
              % len(R.live_cursor_windows()))
        print("    revive.py open browser")
        return

    if _ext_installed() and target:
        if target != os.environ.get("CLAUDE_CODE_SSE_PORT", ""):
            print("This terminal names Cursor window %s, which is gone; "
                  "using the one live window %s."
                  % (os.environ.get("CLAUDE_CODE_SSE_PORT", "?"), target))
        # Prefer the per-window job: it names THIS window's root, so no other
        # window can answer it. The URI is the fallback only when the root is
        # unknown, and it is addressed to the application, so it can land in
        # somebody else's window and rearrange their panes.
        if open_dashboard_in_this_window(url):
            print("Revive dashboard opening in this window: %s" % url)
        else:
            uri = "cursor://local.revive-browser/open?url=" + \
                  urllib.parse.quote(url, safe="")
            subprocess.run(["open", uri], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("Revive dashboard opened in a Cursor tab: %s" % url)
    else:
        subprocess.run(["open", url], check=False)
        print("Revive dashboard: %s (opened in your default browser)" % url)
    print("Pick the sessions to bring back, then click Restore.")


def main():
    a = sys.argv[1:] or ["list"]
    cmd = a[0]
    # No day limit by default. A 7-day default here silently overrode the
    # dashboard's all-time default via REVIVE_DAYS and hid older sessions.
    days = 3650
    if "--days" in a:
        days = int(a[a.index("--days") + 1])
    if cmd == "claim":
        # Called by the Obsidian shell wrapper. Prints "<sid>\t<cwd>" and exits
        # 0 when it wins a ticket, exits 1 when there is nothing to claim, which
        # is the normal case for a terminal you opened yourself.
        root = a[a.index("claim") + 1] if len(a) > a.index("claim") + 1 else ""
        t = claim_ticket(os.path.realpath(root)) if root else None
        if not t:
            sys.exit(1)
        sys.stdout.write("%s\t%s\n" % (t["sid"], t["cwd"]))
        sys.exit(0)
    if cmd == "version":
        cmd_version()
    elif cmd == "ui":
        port = int(a[a.index("--port") + 1]) if "--port" in a else 0
        cmd_ui(days, port)
    elif cmd == "open":
        # revive.py open              -> show the current setting
        # revive.py open cursor       -> always a Cursor tab
        # revive.py open browser      -> always the default browser
        # revive.py open auto         -> Cursor when we can prove the window
        if len(a) > 1:
            try:
                R.set_open_mode(a[1])
            except ValueError:
                print("open mode must be one of: %s" % ", ".join(R.OPEN_MODES))
                return
        cur = R.open_mode()
        for m in R.OPEN_MODES:
            print(" %s %-8s %s" % ("*" if m == cur else " ", m, {
                "auto": "Cursor tab when the window is provable, else browser",
                "cursor": "always a Cursor tab",
                "browser": "always the default browser",
            }[m]))
    elif cmd == "list":
        cmd_list(days)
    elif cmd == "doctor":
        cmd_doctor()
    elif cmd == "backfill":
        for c in backfill_candidates(days):
            print("%-38s %-5s %s" % (c["session_id"], ago(c["last_seen"]),
                                     (c["last_prompt"] or "")[:50]))
    elif cmd == "pick":
        import picker
        cs = candidates(days=days)
        if not cs:
            print("Nothing to revive.")
            return
        chosen = picker.run(cs, ago, short)
        if chosen:
            print_restore_result(restore(chosen))
        else:
            print("Nothing selected.")
    elif cmd == "restore":
        ids = [x for x in a[1:] if not x.startswith("--")]
        cs = [c for c in candidates(days=days) if c["session_id"] in ids
              or c["session_id"][:8] in ids]
        if not cs:
            print("No matching restorable sessions.")
            return
        print_restore_result(restore(cs))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
