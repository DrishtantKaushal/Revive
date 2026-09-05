#!/usr/bin/env python3
"""Registry hook for /revive.  Invoked as: hook.py SessionStart|SessionEnd|UserPromptSubmit

Fail-safe by construction: every path is wrapped so a bug here can never break
a Claude Code session.  Always exits 0.
"""
import json, os, sys, time, subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def find_claude_pid():
    """Walk up from this hook to the claude process that spawned it.

    Claude may invoke the hook directly or through a shell, so we climb a few
    levels and take the last ancestor that looks like claude/node.
    """
    pid = os.getppid()
    best = pid
    for _ in range(5):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=2).stdout.strip()
            if not out:
                break
            parts = out.split(None, 1)
            ppid = int(parts[0])
            comm = (parts[1] if len(parts) > 1 else "").lower()
            if "claude" in comm or comm.endswith("node"):
                best = pid
                break
            pid = ppid
            if pid <= 1:
                break
        except Exception:
            break
    return best


def main():
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    raw = sys.stdin.read()
    try:
        d = json.loads(raw)
    except Exception:
        return
    sid = d.get("session_id")
    if not sid:
        return

    import registry as R
    now = time.time()
    live_path = R.path_for(sid)

    if event == "SessionStart":
        # sse_port is recorded even inside tmux, where the variable is inherited
        # from the launching Cursor terminal but the SSE socket never actually
        # establishes. Blanking it there looked right and was wrong: the port is
        # also how the hook finds the window's lock file, and so how it records
        # the Cursor main pid that later separates "Cursor crashed under it"
        # from "the tab was closed". Dropping it downgraded every real crash in
        # a tmux session to TERMINATED (test_e2e.sh E2E-2).
        #
        # Restore does not need it suppressed anyway: a session whose host is
        # tmux is grouped as "app:tmux" and handed to the tmux adapter, so it
        # can never be delivered into the Cursor window this port names.
        port = os.environ.get("CLAUDE_CODE_SSE_PORT", "")
        win = R.window_info(port)
        pid = find_claude_pid()
        prev = R.load(live_path) or {}
        rec = {
            "session_id": sid,
            "cwd": d.get("cwd", ""),
            "transcript_path": d.get("transcript_path", ""),
            "source": d.get("source", ""),
            "pid": pid,
            "pid_start": R.pid_start(pid),
            "pid_comm": R.pid_comm(pid),
            "tty": os.environ.get("TTY", "") or _tty(pid),
            "sse_port": port,
            "window_pid": win["window_pid"],
            "window_pid_start": R.pid_start(win["window_pid"]) if win["window_pid"] else "",
            "window_pid_comm": R.pid_comm(win["window_pid"]) if win["window_pid"] else "",
            "window_root": win["window_root"],
            "window_token": R.window_token(port),
            "ide_name": win["ide_name"],
            "term_program": os.environ.get("TERM_PROGRAM", ""),
            "host_app": R.host_app(pid),
            "started_at": prev.get("started_at", now),
            "last_seen": now,
            "last_prompt": prev.get("last_prompt", ""),
            "context_tokens": d.get("context_tokens", 0),
            "boot": R.boot_time(),
            "end_reason": None,
            "window_alive_at_end": None,
            "restored_at": None,
        }
        # /clear and /compact keep the PROCESS and mint a NEW session id, so
        # the old id becomes a dead end with no way back to what followed.
        # Verified: a141826f ended at 20:29:22 on pid 2897, and 4963cf80 began
        # 0.2s later on the same pid with source="clear". Same pid plus a
        # near-instant start is the link, so record it in both directions.
        if d.get("source") in ("clear", "compact"):
            prev = _predecessor(R, pid, now)
            if prev:
                rec["predecessor"] = prev["session_id"]
                prev["successor"] = sid
                R.save(prev["_path"], {k: v for k, v in prev.items()
                                       if k != "_path"})

        R.save(live_path, rec)
        # A resumed/compacted session may have a stale ended/ record; clear it.
        e = R.path_for(sid, ended=True)
        if os.path.exists(e):
            try:
                os.unlink(e)
            except OSError:
                pass
        # Automatic surfacing: SessionStart stdout becomes context the model
        # sees, so the first session you open after a crash reports the loss
        # without you having to remember to look. Registry-only (no history
        # scan) to keep this off the hot path.
        _notify_orphans(R, sid)

    elif event == "UserPromptSubmit":
        # Hot path: fires on every prompt. Keep it to one read + one write.
        rec = R.load(live_path)
        if not rec:
            return
        p = (d.get("prompt") or "").strip().replace("\n", " ")
        if p:
            rec["last_prompt"] = p[:160]
        rec["last_seen"] = now
        R.save(live_path, rec)

    elif event == "SessionEnd":
        rec = R.load(live_path)
        if not rec:
            return
        # NOTE: an earlier version deleted the record when the run had no user
        # prompt, to stop a mass restore-then-kill from burying sessions as
        # "deliberately closed". The taxonomy change made that unnecessary (a
        # killed session is TERMINATED, which is restorable) and it caused real
        # data loss: a session you restore and then lose before typing vanished
        # from the registry entirely. Always record the end.
        rec["end_reason"] = d.get("reason", "other")
        rec["ended_at"] = now
        # THE discriminator. Tab close -> Cursor still alive. Crash -> gone.
        # Window liveness, independent of app liveness.
        # Not just "is a port listening", but "is it still the same window".
        rec["window_port_alive"] = R.port_listening(rec.get("sse_port"))
        rec["window_same_at_end"] = R.same_window(rec.get("sse_port"),
                                                  rec.get("window_token"))
        wpid = rec.get("window_pid")
        if wpid:
            rec["window_alive_at_end"] = R.same_process(
                wpid, rec.get("window_pid_start"), rec.get("window_pid_comm"))
        else:
            rec["window_alive_at_end"] = None
        R.save(R.path_for(sid, ended=True), rec)
        try:
            os.unlink(live_path)
        except OSError:
            pass


def _predecessor(R, pid, now, window=120.0):
    """The record this session was cleared or compacted out of.

    Matched on the same pid ending moments earlier: a /clear does not restart
    the process, so the pid is stable across the handover.
    """
    best = None
    for rec in R.all_ended():
        if rec.get("pid") != pid:
            continue
        ended = rec.get("ended_at") or 0
        if not ended or not (-5 < now - ended < window):
            continue
        if best is None or ended > (best.get("ended_at") or 0):
            best = rec
    return best


def _notify_orphans(R, current_sid):
    try:
        boot = R.boot_time()
        lost = []
        for rec in R.all_live() + R.all_ended():
            if rec.get("session_id") == current_sid:
                continue
            if R.classify(rec, boot=boot) in R.RESTORABLE and R.transcript_ok(rec):
                lost.append(rec)
        if not lost:
            return
        folders = sorted({os.path.basename((r.get("cwd") or "").rstrip("/"))
                          for r in lost if r.get("cwd")})
        print("[revive] %d Claude Code session(s) were lost to a crash "
              "(%s). Tell the user they can run /revive to restore them."
              % (len(lost), ", ".join(list(folders)[:6])))
    except Exception:
        pass


def _tty(pid):
    try:
        return subprocess.run(["ps", "-o", "tty=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=2).stdout.strip()
    except Exception:
        return ""


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass          # never break a session
    sys.exit(0)
