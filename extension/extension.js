const vscode = require('vscode');
const fs = require('fs');
const os = require('os');
const path = require('path');

const VERSION = '0.0.12';
const LOG = path.join(os.homedir(), '.claude', 'session-registry', 'extension.log');
// Cursor loads extension code once per window, so a stale extension host silently
// runs an old build. Logging the version on every call makes that visible instead
// of looking like "restore did nothing".
function log(msg) {
  try { fs.appendFileSync(LOG, new Date().toISOString() + '  v' + VERSION + '  ' + msg + '\n'); }
  catch { /* noop */ }
}

const LOCAL_RE = /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?(\/|$)/;

// Resuming a large session otherwise stops on a "Resume from summary /
// Resume full session as-is" prompt. Raising both thresholds suppresses it and
// resumes the full session as-is, which is the wanted default. Verified: the
// prompt appears without these and does not appear with them.
const RESUME_ENV = {
  CLAUDE_CODE_RESUME_TOKEN_THRESHOLD: '999999999',
  CLAUDE_CODE_RESUME_THRESHOLD_MINUTES: '99999999',
  // null REMOVES the variable from the terminal's environment.
  //
  // Cursor itself inherits whatever launched it, and revive launches new
  // windows from inside a Claude Code session, so its terminals were born with
  // CLAUDE_CODE_CHILD_SESSION=1. That turns TRANSCRIPT SAVING OFF: the restored
  // session records nothing and could never be revived again. Seen as
  // "Transcript saving is off - inherited CLAUDE_CODE_CHILD_SESSION marker".
  // A restored session must never inherit the identity of the one restoring it.
  CLAUDE_CODE_CHILD_SESSION: null,
  CLAUDECODE: null,
  CLAUDE_CODE_SESSION_ID: null,
  CLAUDE_CODE_BRIDGE_SESSION_ID: null,
  CLAUDE_CODE_ENTRYPOINT: null,
  CLAUDE_CODE_MESSAGING_SOCKET: null,
  CLAUDE_CODE_MESSAGING_TOKEN: null,
  CLAUDE_PID: null,
  CLAUDE_CODE_EXECPATH: null,
};

// Where to put the dashboard.
//
//   fewer than 2 panes  -> Beside, which creates the second one
//   2 or more panes     -> the first pane that is NOT the active one
//
// Always using Beside was wrong once a split existed: it opened a THIRD pane
// instead of reusing the one already sitting next to the terminal. Always
// using "rightmost" was wrong too, because the active pane is often the
// rightmost, which put the dashboard on top of the terminal.
function targetColumn() {
  let groups = [];
  try { groups = vscode.window.tabGroups.all; } catch { /* older API */ }

  if (groups.length < 2) return vscode.ViewColumn.Beside;

  let active;
  try { active = vscode.window.tabGroups.activeTabGroup?.viewColumn; } catch { /* noop */ }

  const other = groups.find(g => g.viewColumn !== active);
  return other ? other.viewColumn : vscode.ViewColumn.Beside;
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

// A Simple Browser tab is a WEBVIEW, so identify it by viewType. Matching on
// the tab LABEL was wrong twice over: it matched any file called revive.py, so
// the code both misreported where the browser landed and then moved somebody
// else's editor out of the group.
function browserTabs() {
  const out = [];
  let groups = [];
  try { groups = vscode.window.tabGroups.all; } catch (e) { return out; }
  for (const g of groups) {
    for (const t of (g.tabs || [])) {
      const input = t.input;
      const viewType = input && (input.viewType || '');
      if (typeof viewType === 'string' && /simplebrowser/i.test(viewType)) {
        out.push({ tab: t, column: g.viewColumn });
      }
    }
  }
  return out;
}

// Open the dashboard NEXT TO the pane you invoked it from, never on top of it.
//
// Measured: `simpleBrowser.api.open` RESOLVES successfully and then ignores the
// viewColumn it was given (groups=3 active=1 target=2 -> landed=1). So the
// column cannot be requested; it has to be made true before opening. The only
// reliable lever is which group is ACTIVE at the moment the command runs, so
// focus the target group first and let the browser open into it.
// Hot path: the pane logic lives in the skill directory and is re-read on
// every invocation with the require cache cleared, so changing it takes effect
// WITHOUT a window reload. Only edits to this shim still need one.
const LOGIC = path.join(os.homedir(), '.claude', 'skills', 'revive',
                        'extension', 'browser.js');

async function openBrowser(url) {
  try {
    delete require.cache[require.resolve(LOGIC)];
    const mod = require(LOGIC);
    if (mod && typeof mod.openBrowser === 'function') {
      return await mod.openBrowser({ vscode, log, sleep, browserTabs, LOCAL_RE, url });
    }
  } catch (e) {
    log('hot logic unavailable (' + (e && e.message) + '), using built-in');
  }
  return builtinOpenBrowser(url);
}

async function builtinOpenBrowser(url) {
  if (!LOCAL_RE.test(url)) {
    vscode.window.showErrorMessage('revive: refused non-local URL');
    return;
  }

  // An already-open Simple Browser is REVEALED in place rather than reopened,
  // which pins it to wherever it happens to live. Close them first so the
  // focus below actually decides the column.
  const existing = browserTabs();
  if (existing.length) {
    log('closing ' + existing.length + ' existing browser tab(s)');
    try { await vscode.window.tabGroups.close(existing.map(e => e.tab), false); }
    catch (e) { log('close failed: ' + (e && e.message)); }
    await sleep(120);
  }

  let groups = [];
  let active;
  try {
    groups = vscode.window.tabGroups.all;
    active = vscode.window.tabGroups.activeTabGroup &&
             vscode.window.tabGroups.activeTabGroup.viewColumn;
  } catch (e) { /* older API */ }

  if (groups.length < 2) {
    // Nothing to sit beside yet. Make the second pane, then use it.
    log('only ' + groups.length + ' group(s), splitting first');
    try { await vscode.commands.executeCommand('workbench.action.splitEditorRight'); }
    catch (e) { log('split failed: ' + (e && e.message)); }
    await sleep(200);
    try { groups = vscode.window.tabGroups.all; } catch (e) { /* noop */ }
  }

  const other = groups.find(g => g.viewColumn !== active);
  const target = other ? other.viewColumn : active;
  log('openBrowser groups=' + groups.length + ' active=' + active +
      ' target=' + target);

  const FOCUS = [
    'workbench.action.focusFirstEditorGroup',
    'workbench.action.focusSecondEditorGroup',
    'workbench.action.focusThirdEditorGroup',
    'workbench.action.focusFourthEditorGroup',
    'workbench.action.focusFifthEditorGroup',
  ];
  if (target >= 1 && target <= FOCUS.length) {
    try {
      await vscode.commands.executeCommand(FOCUS[target - 1]);
      await sleep(120);
    } catch (e) { log('focus failed: ' + (e && e.message)); }
  }

  let via = 'api.open';
  try {
    await vscode.commands.executeCommand(
      'simpleBrowser.api.open', vscode.Uri.parse(url), { preserveFocus: false });
  } catch (e) {
    via = 'show';
    try { await vscode.commands.executeCommand('simpleBrowser.show', url); }
    catch (e2) { log('both open commands failed: ' + (e2 && e2.message)); return; }
  }

  await sleep(250);
  const landed = browserTabs().map(b => b.column);
  log('via=' + via + ' target=' + target + ' landed=' + JSON.stringify(landed));

  if (landed.length === 1 && landed[0] === active && target !== active) {
    log('still on the invoking pane, moving it out');
    try {
      await vscode.commands.executeCommand('workbench.action.moveEditorToRightGroup');
      await sleep(150);
      log('after move landed=' + JSON.stringify(browserTabs().map(b => b.column)));
    } catch (e) { log('move failed: ' + (e && e.message)); }
  }
}

// Restore sessions as EDITOR-AREA terminals.
//
// The first implementation used VS Code tasks. That was wrong: task terminals
// are pinned to the bottom panel, so twenty restored sessions were squeezed
// into unreadable columns, every tab was suffixed "Task", and the task
// processes lingered after the session exited. createTerminal with
// TerminalLocation.Editor gives a normal editor tab per session, a clean name,
// and no task machinery at all.
function restoreSessions(payload) {
  let items;
  try {
    items = JSON.parse(Buffer.from(payload, 'base64').toString('utf8'));
  } catch {
    vscode.window.showErrorMessage('revive: could not read restore payload');
    return;
  }
  if (!Array.isArray(items) || items.length === 0) return;

  log('restore payload: ' + items.length + ' item(s)');
  let opened = 0;
  for (const it of items) {
    const cwd = String(it.cwd || '');
    const sid = String(it.sid || '');
    // Only ever a plain absolute path and a uuid-ish id reach a shell.
    if (!/^\/[^\0\n\r`$]*$/.test(cwd) || !/^[A-Za-z0-9-]{6,64}$/.test(sid)) continue;
    // Run the command as the shell's ARGUMENT rather than typing it in.
    // sendText echoed the text before zsh had drawn its prompt, so the line
    // appeared twice; awaiting processId was not enough, because the pty
    // existing is not the same as the shell being ready. Passing it via
    // shellArgs removes the race entirely: nothing is ever typed. The trailing
    // `exec zsh -l` leaves you at a normal shell when the session ends, instead
    // of the tab closing under you.
    const term = vscode.window.createTerminal({
      name: String(it.name || 'revive').slice(0, 40),
      cwd,
      location: vscode.TerminalLocation.Editor,
      env: RESUME_ENV,
      shellPath: '/bin/zsh',
      shellArgs: ['-lc', `claude --resume ${sid}; exec /bin/zsh -l`],
    });
    opened++;
  }
  log('created ' + opened + ' terminal(s)');
  if (opened) {
    vscode.window.showInformationMessage(
      `revive: reopened ${opened} session${opened === 1 ? '' : 's'}.`);
  }
}

// ---------------------------------------------------------------- pending
// Restoring into ANOTHER window cannot go through the cursor:// handler: the
// URI is delivered to whichever window owns the handler, not the one we mean.
// Instead the CLI drops a job file naming a target folder, and each window
// claims only the jobs matching its own workspace root. Deterministic, and it
// works whether the window already existed or was just opened for the job.
const PENDING = path.join(os.homedir(), '.claude', 'session-registry', 'pending');
const JOB_MAX_AGE_MS = 5 * 60 * 1000;

function myRoots() {
  return (vscode.workspace.workspaceFolders || []).map(f => f.uri.fsPath);
}

function claimPending() {
  let files = [];
  try { files = fs.readdirSync(PENDING).filter(f => f.endsWith('.json')); }
  catch { return; }
  const roots = myRoots();
  for (const f of files) {
    const full = path.join(PENDING, f);
    let job;
    try { job = JSON.parse(fs.readFileSync(full, 'utf8')); } catch { continue; }
    if (!roots.includes(job.root)) continue;                 // not for this window
    if (Date.now() - (job.at || 0) > JOB_MAX_AGE_MS) {        // stale, drop it
      try { fs.unlinkSync(full); } catch { /* noop */ }
      continue;
    }
    // Claim atomically so two windows on the same folder cannot both run it.
    const taken = full + '.taken';
    try { fs.renameSync(full, taken); } catch { continue; }
    try {
      // A cursor:// URI is delivered to the APPLICATION, and whichever window
      // owns the handler answers it, so /revive run in window A opened its
      // dashboard in window B. Restore already solved this with per-window job
      // files claimed by workspace root; the browser now uses the same route.
      if (job.url) {
        log('claimed browser job for ' + job.root);
        openBrowser(job.url);
      } else {
        restoreSessions(Buffer.from(JSON.stringify(job.items)).toString('base64'));
      }
    }
    finally { try { fs.unlinkSync(taken); } catch { /* noop */ } }
  }
}

function activate(context) {
  log('activated, workspace=' + ((vscode.workspace.workspaceFolders || [])
      .map(f => f.uri.fsPath).join(',') || 'none'));
  try { fs.mkdirSync(PENDING, { recursive: true }); } catch { /* noop */ }
  claimPending();                                  // jobs waiting at startup
  const watcher = vscode.workspace.createFileSystemWatcher(
    new vscode.RelativePattern(vscode.Uri.file(PENDING), '*.json'));
  watcher.onDidCreate(() => claimPending());       // jobs for an already-open window
  context.subscriptions.push(watcher);

  context.subscriptions.push(vscode.window.registerUriHandler({
    handleUri(uri) {
      const q = new URLSearchParams(uri.query);
      log('handleUri path=' + uri.path);
      // Extension code loads ONCE per window, so every change to this file
      // needed a manual reload. That cost three rounds of "the fix does not
      // work" on code that had never run. Now revive can ask for it.
      if (uri.path === '/reload') {
        vscode.commands.executeCommand('workbench.action.reloadWindow');
        return;
      }
      if (uri.path === '/restore') return restoreSessions(q.get('p') || '');
      openBrowser(q.get('url') || '');
    }
  }));
}
exports.activate = activate;
exports.deactivate = function () {};
