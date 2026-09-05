---
name: revive
description: Find and relaunch Claude Code sessions lost to a Cursor crash, back into their own folders, tabs and windows. Use when Cursor crashed or was force-quit and terminal tabs with running claude sessions were lost, or when the user asks to restore/resume/recover lost sessions or terminals.
---

# /revive: bring back sessions lost to a Cursor crash

## Paths

Resolve these from the skill itself, never from a machine-specific path. When
this skill loads, its own location is stated above as "Base directory for this
skill".

- SCRIPTS: `<base directory>/scripts`
- PYTHON: `python3`
- REGISTRY: `~/.claude/session-registry/`

`python3` is deliberate. An earlier version pinned Xcode's Command Line Tools
interpreter, which does not exist on a Homebrew-only machine, so the very first
command failed. The scripts are stdlib only and run on any Python 3.9 or newer.

## How it decides what to restore

A Cursor crash and a closed terminal tab deliver the **same** signal (SIGHUP)
and both fire `SessionEnd` with reason `other`. The discriminator is whether
the **Cursor main process** was alive at the moment the session ended,
recorded by the `SessionEnd` hook.

| State | Meaning | Restored? |
|---|---|---|
| `CRASHED` | Cursor died under it, or it was SIGKILLed | yes |
| `TERMINATED` | hung up or killed, tab closed | yes |
| `EXITED` | `/exit`, logout, `/clear` | no |
| `RUNNING` | still alive right now | no (would duplicate) |

There is no fifth state. An earlier build had a `STALE` rule that hid any
session started before the last boot, which is exactly the set a crash-then-
reboot produces. It is gone and D1-D5 in the logic tests hold the line.

Within `TERMINATED`, the card also says HOW it ended, because closing a window
and closing a tab both leave Cursor running and the main-pid check cannot tell
them apart:

| Signal at SessionEnd | Detail shown |
|---|---|
| Cursor main pid gone | `app crash` |
| Cursor alive, this window's SSE port no longer listening | `window closed` |
| Cursor alive, window's port still listening | `tab closed` |
| no SessionEnd fired at all | `killed outright` |

Every Cursor window runs its own Claude extension server on its own port, so the
listening socket is the window's liveness. The lock FILE is not usable for this.
All of these stay restorable; the detail is for you, not for the decision.

Only `/exit` and logout express intent to finish. Everything else, including
closing a tab or killing the process, is loss and stays restorable. The
crash-vs-terminated distinction is only a label; both are offered.

A run in which you never submitted a prompt records nothing, so restoring a
batch and then killing it cannot bury those sessions.

`/clear` and `/compact` keep the process and mint a NEW session id, so the old
id would otherwise be a dead end. The SessionStart hook links them: a start with
`source=clear` on a pid that ended seconds earlier records `predecessor` on the
new record and `successor` on the old one. Cards show `abc12345 -> this` and
`this -> def67890`, so a cleared session with no prompt of its own still points
at the one that has the content.

Sessions that started before the hooks existed have no record at all. Those are
reconstructed from `~/.claude/history.jsonl` and by walking every project dir
under `~/.claude/projects/`, filtered by the `/exit` rule
(a typed `/exit` means a deliberate close). That filter is precise but only
~65% recall, so it excludes, never detects.

## Subcommands

Run with `{PYTHON} {SCRIPTS}/revive.py <cmd>`.

- `ui`: **default.** Serves the dashboard on an ephemeral loopback port and
  opens it as a tab inside Cursor via the companion extension. Pick sessions
  across folders, click Restore. A dashboard left over from a previous run is
  reused when it still answers, and killed when it is alive but wedged; the
  port and capability token are published to `dashboard.json`, which is the
  only place the URL should ever be read from.
- `list`: what is recoverable and why
- `pick`: interactive multi-select, then restore (space toggle, `a` all, enter)
- `restore <session-id>...`: restore specific sessions
- `backfill [--days N]`: candidates with no registry record (default: no limit)
- `version`: compares the extension on disk against the version Cursor is
  actually executing. Cursor loads extension code once per window, so after any
  extension change this will say STALE until the window is reloaded.
- `doctor`: version check, registry health, open Cursor windows

## Dashboard behaviour

Every known session is listed, with no day limit. A status filter row (All,
Crashed, Terminated, Running, Exited) and a date filter (Today, Yesterday,
7/15/30 days, All time) sits above the folder rail; folder counts
follow the active status. Selection survives both filters, so you can gather
sessions across folders and statuses before restoring.

Nothing is pre-selected: with the day limit lifted that would arm 150+ sessions,
and restoring that many at once recreates the memory spike this tool exists to
recover from. `Select all N` selects everything currently in view.

Cards show the session's own name where one was set with `/rename`, its
lifetime, age, and which window it will return to.

## Restoring

Selected sessions are grouped by the Cursor **window** they lived in (Chrome
rebuilds window structure rather than scattering tabs, and so does this).
`sse_port` is the window identity; sessions with none go to the window you are
in, never to an invented root.

Delivery is a **job file per window** under
`~/.claude/session-registry/pending/`, not a `cursor://` URI: a URI reaches
whichever window owns the handler, which is fine for one window and wrong for
several. Each window claims only jobs naming its own workspace root, atomically,
so two windows on the same folder cannot both run one. Windows that are not open
are opened first. No VS Code tasks and no generated workspace file are involved;
both were tried and both regressed.

## Default behaviour

When the user invokes `/revive` with no arguments, run:

    {PYTHON} {SCRIPTS}/revive.py ui

That opens the dashboard in a Cursor tab. Tell the user it is open and that
clicking Restore executes the restore. Do not restore on their behalf; the
Restore button is the go-ahead. Use `list` instead if they only want to see
what is recoverable, or `pick` for the terminal picker when there is no GUI.

## Window grouping (Chrome-style)

Sessions are NOT scattered into one window each. `sse_port` is the true Cursor
window identity: every window runs its own Claude extension server on its own
port, so sessions that shared a port shared a window and are restored together
into one window. No port recorded falls back to the workspace root; nothing
recorded at all (backfilled sessions) goes into a single shared window rather
than many.

## The dashboard

Real shadcn/ui (new-york, neutral) on Vite + React + Tailwind in `app/`.
Components live in `app/src/components/ui/` (card, checkbox, tabs, button,
badge, input, separator, scroll-area) on Radix primitives.

`serve.py` serves the built `app/dist/`, falling back to the dependency-free
`ui/index.html` if the app has not been built. After editing the React source:

    cd app && bun run build

Behaviour worth keeping: selection persists across folder switches, each folder
shows a filled circle with how many you ticked (outlined circle with the total
when none), running sessions are shown but locked, and every card names the
window it will be restored into.

## Companion extension

`~/.cursor/extensions/revive-browser/` registers
`cursor://local.revive-browser/open?url=...` and forwards to `simpleBrowser.show`.
It only accepts loopback URLs. Pane rule: with fewer than 2 editor groups it
opens `Beside`, otherwise in the first group that is NOT the active one. Both
simpler rules were wrong. "Rightmost" resolved to the active pane when the
terminal was already rightmost, and "always Beside" created a third pane when a
split already existed.

The extension also owns restore: it creates terminals with
`location: TerminalLocation.Editor` so sessions come back in the editor area
rather than pinned to the bottom panel, and claims job files atomically.

This exists because Cursor has no CLI route to the Simple Browser: `--open-url`
ignores http URLs and `command:` URIs only fire from trusted webviews. Both were
verified empirically.

Cursor caches extension code, so after editing `extension.js` the window must be
reloaded (Cmd+Shift+P, "Developer: Reload Window") before the change takes
effect.

## Obsidian

Obsidian has no terminal of its own. It has one only because the third-party
`terminal` plugin is installed and enabled in a vault, and that plugin opens
terminals through a command that takes NO arguments. So a terminal cannot be
told which session to resume; it has to find out after it starts.

Two pieces, installed by `{PYTHON} {SCRIPTS}/install_obsidian.py`:

- **Bridge plugin** (`revive-bridge`, copied into the vault). Watches
  `pending/`, atomically claims a job whose root is this vault, writes one
  ticket per session, then fires `terminal:open-terminal.integrated.root` once
  per session. Also claims on layout-ready, which is the case that matters:
  revive writes the job, opens Obsidian, and the plugin finds it on load.
- **Wrapper shell**, installed as the Terminal plugin's default `integrated`
  profile. Each terminal runs `revive.py claim <vault>`; the winner execs
  `claude --resume <sid>` in the session's own cwd, everyone else execs
  `/bin/zsh --login`. With nothing queued it is byte-identical in behaviour to
  the profile it replaced.

Claiming is `os.rename`, which is atomic, so N terminals starting at once take
N distinct sessions and none is resumed twice. Tickets expire after 5 minutes.

`install_obsidian.py uninstall` restores the original profile from the recorded
executable and args, disables the bridge and deletes it. A copy of the Terminal
plugin's `data.json` is kept as `data.json.revive-backup`.

Command ids were read out of the plugin's own registration loop, which builds
them as `open-terminal.${type}.${context}` and namespaces them with the plugin
id. Do not guess these; they are generated, not literals.

## Install / uninstall
- `{PYTHON} {SCRIPTS}/install.py`: installs hooks (preserves existing hooks)
- `{PYTHON} {SCRIPTS}/install.py uninstall`
- Tests: `tests/test_logic.py` (unit), `tests/test_e2e.sh` (real processes),
  `tests/test_cleanroom.sh` (clone into a scratch HOME and install as a new
  user would; the only test that covers the first-run path)
