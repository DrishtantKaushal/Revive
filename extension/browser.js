'use strict';
// Loaded fresh by extension.js on every /revive, so edits need no reload.
//
// Cursor's browser cannot be aimed. Measured, all of it:
//   * `simpleBrowser.api.open` ignores viewColumn, ViewColumn.Beside included.
//   * Cursor's own browser.splitEditorWithNewBrowserTab* commands resolve and
//     do nothing.
//   * `showTextDocument` honours a numeric viewColumn only when that column is
//     a real editor group; Cursor's "New Agent" panel is a column that cannot
//     hold editors, so it falls back into the terminal's pane.
//   * Tab references cannot be closed: "Invalid tab not found".
//   * Tab inputs are opaque ({}, no keys), so the browser cannot be identified
//     by type, and matching labels once grabbed a file named revive.py.
//
// What works: find where it landed by which column GAINED a tab, then move that
// editor out. That succeeds whenever the window has a real second editor pane.
// When the only neighbour is the Agent panel, both moves are refused and the
// dashboard shares the terminal's pane. That case is LOGGED, not papered over.
exports.openBrowser = async function ({ vscode, log, sleep, LOCAL_RE, url }) {
  if (!LOCAL_RE.test(url)) {
    vscode.window.showErrorMessage('revive: refused non-local URL');
    return;
  }

  const countBefore = {};
  const columnsBefore = [];
  try {
    for (const g of vscode.window.tabGroups.all) {
      countBefore[g.viewColumn] = (g.tabs || []).length;
      columnsBefore.push(g.viewColumn);
    }
  } catch (e) { /* older API */ }
  log('openBrowser columns=' + JSON.stringify(columnsBefore));

  let via = 'api.open';
  try {
    await vscode.commands.executeCommand(
      'simpleBrowser.api.open', vscode.Uri.parse(url), { preserveFocus: false });
  } catch (e) {
    via = 'show';
    try { await vscode.commands.executeCommand('simpleBrowser.show', url); }
    catch (e2) { log('both open commands failed: ' + (e2 && e2.message)); return; }
  }

  // Short: Cursor paints the browser in its chosen pane before we can move it,
  // so this delay is the visible flicker. Long enough only for the tab to exist.
  await sleep(120);

  let landedCol;
  let groupsNow = [];
  try {
    groupsNow = vscode.window.tabGroups.all;
    for (const g of groupsNow) {
      if ((g.tabs || []).length > (countBefore[g.viewColumn] || 0)) {
        landedCol = g.viewColumn;
        break;
      }
    }
  } catch (e) { /* noop */ }

  if (landedCol === undefined) {
    log('via=' + via + ' revealed an already-open dashboard, leaving it');
    return;
  }

  const columns = groupsNow.map(g => g.viewColumn).sort((a, b) => a - b);
  const rightmost = columns[columns.length - 1];
  const countAfterOpen = {};
  for (const g of groupsNow) countAfterOpen[g.viewColumn] = (g.tabs || []).length;

  const tryMove = async (cmd) => {
    log('via=' + via + ' landed=' + landedCol +
        ' columns=' + JSON.stringify(columns) + ' -> ' + cmd);
    try {
      await vscode.commands.executeCommand(cmd);
      await sleep(150);
    } catch (e) { log('move threw: ' + (e && e.message)); return false; }
    try {
      for (const g of vscode.window.tabGroups.all) {
        if (g.viewColumn === landedCol) {
          return (g.tabs || []).length < (countAfterOpen[landedCol] || 0);
        }
      }
    } catch (e) { /* noop */ }
    return false;
  };

  const RIGHT = 'workbench.action.moveEditorToRightGroup';
  const LEFT = 'workbench.action.moveEditorToLeftGroup';
  const first = landedCol === rightmost ? LEFT : RIGHT;
  if (await tryMove(first)) return;
  if (await tryMove(first === RIGHT ? LEFT : RIGHT)) return;
  log('BOTH moves refused: this window has no real second editor pane, so the '
      + 'dashboard is sharing the terminal pane.');
};
