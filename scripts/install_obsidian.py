#!/usr/bin/env python3
"""Make Obsidian a revive target.

Two pieces, because Obsidian has no terminal of its own and the plugin that
gives it one opens terminals with no arguments:

  1. a bridge plugin, which notices a queued job for this vault and opens one
     terminal per session;
  2. a wrapper shell, installed as the Terminal plugin's default "integrated"
     profile, which each of those terminals runs. It claims one ticket and
     becomes that session, or falls through to your normal login shell.

The wrapper is transparent: with nothing queued it execs `/bin/zsh --login`,
which is exactly what the profile ran before. The original executable and args
are recorded so `uninstall` restores them byte for byte.
"""
import json, os, shutil, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import registry as R                                            # noqa: E402

STATE = os.path.join(R.ROOT, "obsidian-install.json")
GEN = os.path.join(R.ROOT, "hosts")
PROFILE_KEY = "darwinIntegratedDefault"


def vaults():
    return R.obsidian_can_host_a_terminal()


def _plugin_dir(vault):
    return os.path.join(vault, ".obsidian", "plugins", "revive-bridge")


def _terminal_data(vault):
    return os.path.join(vault, ".obsidian", "plugins", "terminal", "data.json")


def _community(vault):
    return os.path.join(vault, ".obsidian", "community-plugins.json")


def install(vault):
    out = []
    os.makedirs(GEN, exist_ok=True)

    # 1. the wrapper, with this vault and this python baked in
    wrapper = os.path.join(GEN, "obsidian-%s.sh" %
                           os.path.basename(vault.rstrip("/")))
    src = open(os.path.join(SKILL, "hosts", "obsidian-shell")).read()
    src = (src.replace("__VAULT__", os.path.realpath(vault))
              .replace("__PYTHON__", sys.executable)
              .replace("__REVIVE__", os.path.join(HERE, "revive.py")))
    with open(wrapper, "w") as fh:
        fh.write(src)
    os.chmod(wrapper, 0o755)
    out.append("wrapper  %s" % wrapper)

    # 2. the bridge plugin
    dst = _plugin_dir(vault)
    os.makedirs(dst, exist_ok=True)
    for f in ("main.js", "manifest.json"):
        shutil.copy2(os.path.join(SKILL, "hosts", "obsidian-plugin", f),
                     os.path.join(dst, f))
    out.append("plugin   %s" % dst)

    # 3. enable it
    cp = _community(vault)
    try:
        enabled = json.load(open(cp))
    except Exception:
        enabled = []
    if "revive-bridge" not in enabled:
        enabled.append("revive-bridge")
        json.dump(enabled, open(cp, "w"), indent=2)
    out.append("enabled  revive-bridge")

    # 4. repoint the default integrated profile, remembering what was there
    td = _terminal_data(vault)
    data = json.load(open(td))
    prof = (data.get("profiles") or {}).get(PROFILE_KEY)
    if prof is None:
        raise SystemExit("no %s profile in %s" % (PROFILE_KEY, td))
    state = {}
    if os.path.exists(STATE):
        try:
            state = json.load(open(STATE))
        except Exception:
            state = {}
    if vault not in state:                      # never overwrite a real backup
        shutil.copy2(td, td + ".revive-backup")
        state[vault] = {"executable": prof.get("executable"),
                        "args": prof.get("args"),
                        "backup": td + ".revive-backup",
                        "at": time.time()}
        json.dump(state, open(STATE, "w"), indent=2)
    prof["executable"] = wrapper
    prof["args"] = []
    json.dump(data, open(td, "w"), indent=2)
    out.append("profile  %s -> %s" % (PROFILE_KEY, wrapper))
    out.append("backup   %s" % (td + ".revive-backup"))
    return out


def uninstall(vault):
    out = []
    try:
        state = json.load(open(STATE))
    except Exception:
        state = {}
    td = _terminal_data(vault)
    if vault in state:
        data = json.load(open(td))
        prof = (data.get("profiles") or {}).get(PROFILE_KEY)
        if prof is not None:
            prof["executable"] = state[vault]["executable"]
            prof["args"] = state[vault]["args"]
            json.dump(data, open(td, "w"), indent=2)
            out.append("profile restored to %s %s" %
                       (prof["executable"], prof["args"]))
        del state[vault]
        json.dump(state, open(STATE, "w"), indent=2)
    cp = _community(vault)
    try:
        enabled = [p for p in json.load(open(cp)) if p != "revive-bridge"]
        json.dump(enabled, open(cp, "w"), indent=2)
        out.append("plugin disabled")
    except Exception:
        pass
    d = _plugin_dir(vault)
    if os.path.isdir(d):
        shutil.rmtree(d)
        out.append("plugin removed")
    return out


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "install"
    vs = vaults()
    if not vs:
        raise SystemExit("no Obsidian vault with the Terminal plugin enabled")
    for v in vs:
        print("%s: %s" % (cmd, v))
        for line in (install(v) if cmd == "install" else uninstall(v)):
            print("  " + line)
