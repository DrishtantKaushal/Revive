#!/usr/bin/env python3
"""Install/uninstall the /revive hooks in ~/.claude/settings.json.

Deliberately does NOT repeat the lost-update race found in
visul.terminal-sessions (agents/hooks.ts:44-80). That code reads settings.json,
mutates, then atomically renames -- which prevents a TORN file but not a LOST
UPDATE: Claude Code rewrites this same file constantly to append permission
rules, and an install landing mid-write silently drops them.

Fixes applied here:
  1. exclusive lock  - O_CREAT|O_EXCL lockfile serialises writers
  2. re-read in lock - the copy we mutate is read AFTER acquiring the lock
  3. verify          - re-read after write; roll back if it does not parse
  4. one-shot backup - never overwrite an existing .bak with mutated content
  5. tmp cleanup     - temp file removed even when rename fails
"""
import json, os, shutil, sys, time

SETTINGS = os.path.expanduser("~/.claude/settings.json")
LOCK = SETTINGS + ".revive.lock"
# The interpreter written into the hook command. sys.executable is whatever
# python ran this installer, so the hooks are guaranteed to have an interpreter
# that exists. The previous value was pinned to Xcode's Command Line Tools path,
# which is absent on a Homebrew-only machine, and the hooks would then fail
# silently on every session start.
PY = os.environ.get("REVIVE_PYTHON") or sys.executable
HOOKSCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook.py")
EVENTS = ("SessionStart", "UserPromptSubmit", "SessionEnd")


class Lock:
    def __enter__(self):
        for _ in range(100):
            try:
                fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:                       # break a stale lock
                    if time.time() - os.path.getmtime(LOCK) > 30:
                        os.unlink(LOCK)
                        continue
                except OSError:
                    pass
                time.sleep(0.1)
        raise RuntimeError("could not acquire %s" % LOCK)

    def __exit__(self, *a):
        try:
            os.unlink(LOCK)
        except OSError:
            pass


def read_settings():
    """Return the current settings, tolerating the two states a fresh machine
    is legitimately in.

    A user who installs /revive before Claude Code has ever written its config
    has no settings.json at all, and an interrupted write can leave a
    zero-byte one. Neither is corruption: both mean "no hooks configured yet",
    and the installer is the thing that creates the file. Only genuinely
    malformed JSON should raise, because silently discarding a real config
    would take the user's permission rules with it.
    """
    try:
        with open(SETTINGS) as fh:
            raw = fh.read().strip()
    except FileNotFoundError:
        return {}
    return json.loads(raw) if raw else {}


def cmd_for(event):
    return '%s %s %s' % (PY, HOOKSCRIPT, event)


# Which hook entries in settings.json are ours.
#
# This used to be the literal "skills/revive/scripts/hook.py", which quietly
# assumed the tool lives at ~/.claude/skills/revive. Clone the repo anywhere
# else and the assumption fails in the worst possible way: install() stops
# recognising its own entries, so it appends instead of replacing, and
# uninstall removes nothing. Measured in a clean room: three installs left
# nine hook entries, every event firing three times.
#
# Identity is now the hook script's own absolute path, plus any path a previous
# install recorded (so moving the checkout still uninstalls cleanly), plus the
# old literal (so installs made before this change are still recognised).
LEGACY_MARKERS = ("skills/revive/scripts/hook.py",)
INSTALLED = os.path.expanduser("~/.claude/session-registry/install.json")


def recorded_hookscripts():
    try:
        with open(INSTALLED) as fh:
            return [p for p in json.load(fh).get("hookscripts", []) if p]
    except (OSError, ValueError):
        return []


def record_hookscript(path, add=True):
    paths = [p for p in recorded_hookscripts() if p != path]
    if add:
        paths.append(path)
    if not paths:
        try:
            os.unlink(INSTALLED)
        except OSError:
            pass
        return
    os.makedirs(os.path.dirname(INSTALLED), exist_ok=True)
    tmp = INSTALLED + ".tmp.%d" % os.getpid()
    with open(tmp, "w") as fh:
        json.dump({"hookscripts": paths}, fh, indent=2)
    os.replace(tmp, INSTALLED)


def is_ours(c):
    if not isinstance(c, str):
        return False
    return (HOOKSCRIPT in c
            or any(m in c for m in LEGACY_MARKERS)
            or any(p in c for p in recorded_hookscripts()))


def entry_is_ours(e):
    try:
        return any(is_ours(h.get("command")) for h in e.get("hooks", []))
    except Exception:
        return False


def write_verified(data):
    """Write, then read back and confirm. Rolls back on corruption."""
    tmp = SETTINGS + ".tmp.%d" % os.getpid()
    try:
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2)
        with open(tmp) as fh:
            json.load(fh)                  # verify BEFORE it becomes live
        os.replace(tmp, SETTINGS)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    with open(SETTINGS) as fh:             # verify AFTER
        json.load(fh)


def install():
    # ~/.claude may not exist yet; Lock() writes its file in here too.
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with Lock():
        data = read_settings()             # re-read INSIDE the lock
        bak = SETTINGS + ".revive.bak"
        # Nothing to back up when we are the ones creating the file.
        if os.path.exists(SETTINGS) and not os.path.exists(bak):
            shutil.copy2(SETTINGS, bak)
        before_perms = len(data.get("permissions", {}).get("allow", []))
        hooks = data.setdefault("hooks", {})
        for ev in EVENTS:
            arr = [e for e in hooks.get(ev, []) if not entry_is_ours(e)]
            arr.append({"hooks": [{"type": "command", "command": cmd_for(ev),
                                   "timeout": 10}]})
            hooks[ev] = arr
        write_verified(data)
        record_hookscript(HOOKSCRIPT)
        after = json.load(open(SETTINGS))
        after_perms = len(after.get("permissions", {}).get("allow", []))
        assert after_perms == before_perms, "permission rules lost!"
        return before_perms, after_perms, bak if os.path.exists(bak) else None


def uninstall():
    if not os.path.exists(SETTINGS):
        return
    with Lock():
        data = read_settings()
        hooks = data.get("hooks", {})
        for ev in list(hooks):
            arr = [e for e in hooks[ev] if not entry_is_ours(e)]
            if arr:
                hooks[ev] = arr
            else:
                del hooks[ev]
        write_verified(data)
        record_hookscript(HOOKSCRIPT, add=False)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "uninstall":
        uninstall()
        print("revive hooks removed")
    else:
        b, a, bak = install()
        print("revive hooks installed for: %s" % ", ".join(EVENTS))
        print("permission rules preserved: %d -> %d" % (b, a))
        print("one-shot backup: %s" % (bak or "none, settings.json was created"))
