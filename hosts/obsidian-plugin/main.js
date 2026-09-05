'use strict';
const { Plugin, Notice } = require('obsidian');
const fs = require('fs');
const path = require('path');
const os = require('os');

const ROOT = path.join(os.homedir(), '.claude', 'session-registry');
const PENDING = path.join(ROOT, 'pending');
const TICKETS = path.join(ROOT, 'tickets');
const JOB_MAX_AGE_MS = 5 * 60 * 1000;

// Verified by reading the Terminal plugin's own registration loop, which builds
// ids as `open-terminal.${type}.${context}` and namespaces them with the plugin
// id. "root" opens at the vault base path; the shell wrapper cd's from there.
const OPEN_CMD = 'terminal:open-terminal.integrated.root';

module.exports = class ReviveBridge extends Plugin {
  async onload() {
    this.vault = this.app.vault.adapter.getBasePath
      ? this.app.vault.adapter.getBasePath()
      : null;
    if (!this.vault) return;

    try { fs.mkdirSync(PENDING, { recursive: true }); } catch (e) { /* noop */ }

    this.addCommand({
      id: 'claim-now',
      name: 'Revive: check for queued sessions now',
      callback: () => this.claim(true),
    });

    // Jobs that arrived while Obsidian was closed. This is the case that makes
    // restore work when the app is not already running: revive writes the job,
    // opens Obsidian, and the plugin finds it on load.
    this.app.workspace.onLayoutReady(() => setTimeout(() => this.claim(false), 800));

    try {
      this.watcher = fs.watch(PENDING, () => {
        clearTimeout(this._t);
        this._t = setTimeout(() => this.claim(false), 250);   // debounce
      });
      this.register(() => this.watcher.close());
    } catch (e) { /* watching is a bonus; onLayoutReady still covers startup */ }
  }

  async claim(verbose) {
    let files = [];
    try { files = fs.readdirSync(PENDING).filter(f => f.endsWith('.json')); }
    catch (e) { return; }

    let opened = 0;
    for (const f of files) {
      const full = path.join(PENDING, f);
      let job;
      try { job = JSON.parse(fs.readFileSync(full, 'utf8')); } catch (e) { continue; }
      if (path.resolve(job.root || '') !== path.resolve(this.vault)) continue;
      if (Date.now() - (job.at || 0) > JOB_MAX_AGE_MS) {
        try { fs.unlinkSync(full); } catch (e) { /* noop */ }
        continue;
      }
      // Atomic claim, same rule the Cursor extension uses: whoever renames it
      // owns it, so two Obsidian windows on one vault cannot both run the job.
      const taken = full + '.taken';
      try { fs.renameSync(full, taken); } catch (e) { continue; }

      const items = Array.isArray(job.items) ? job.items : [];
      try {
        fs.mkdirSync(TICKETS, { recursive: true });
        // One ticket per session. The terminal command takes no arguments, so
        // each shell discovers which session it is by claiming a ticket.
        for (const it of items) {
          const p = path.join(TICKETS, it.sid + '.json');
          fs.writeFileSync(p + '.tmp', JSON.stringify({
            root: path.resolve(this.vault), cwd: it.cwd,
            sid: it.sid, at: Date.now() / 1000,
          }));
          fs.renameSync(p + '.tmp', p);
        }
        for (let i = 0; i < items.length; i++) {
          // Spaced out: each terminal has to start and claim its ticket before
          // the next one races it.
          await new Promise(r => setTimeout(r, i === 0 ? 0 : 700));
          this.app.commands.executeCommandById(OPEN_CMD);
          opened++;
        }
        if (items.length) {
          new Notice('Revive: reopening ' + items.length + ' session'
                     + (items.length === 1 ? '' : 's'));
        }
      } finally {
        try { fs.unlinkSync(taken); } catch (e) { /* noop */ }
      }
    }
    if (verbose && !opened) new Notice('Revive: nothing queued for this vault');
  }
};
