#!/usr/bin/env python3
"""Curses multi-select for /revive.  fzf/gum are not installed on this machine,
so this is stdlib-only."""
import curses

STATE_TAG = {"CRASHED": "crash", "TERMINATED": "killed", "BACKFILL": "history"}


def run(items, ago, short):
    sel = set(range(len(items)))       # default: everything ticked
    return curses.wrapper(_loop, items, sel, ago, short)


def _loop(scr, items, sel, ago, short):
    curses.curs_set(0)
    try:
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
    except Exception:
        pass
    pos, top = 0, 0
    while True:
        scr.erase()
        h, w = scr.getmaxyx()
        scr.addnstr(0, 0, "Revive Claude Code sessions - %d found" % len(items), w - 1,
                    curses.A_BOLD)
        scr.addnstr(1, 0, "space toggle   a all   n none   enter restore   q quit",
                    w - 1, curses.A_DIM)
        body = h - 4
        if pos < top:
            top = pos
        if pos >= top + body:
            top = pos - body + 1
        row = 3
        last_group = None
        for i in range(top, min(len(items), top + body)):
            c = items[i]
            g = c.get("window_root") or c.get("cwd") or ""
            if g != last_group:
                last_group = g
                if row < h - 1:
                    scr.addnstr(row, 0, "  window: %s" % short(g, w - 14), w - 1,
                                curses.color_pair(1))
                    row += 1
                if row >= h - 1:
                    break
            mark = "[x]" if i in sel else "[ ]"
            folder = (c.get("cwd") or "").rstrip("/").split("/")[-1][:18]
            tag = STATE_TAG.get(c.get("_state"), "")
            line = " %s %-18s %-5s %-9s %s" % (
                mark, folder, ago(c.get("last_seen")), tag,
                (c.get("last_prompt") or "").replace("\n", " ")[:max(10, w - 46)])
            attr = curses.A_REVERSE if i == pos else curses.A_NORMAL
            if i in sel and i != pos:
                attr |= curses.color_pair(2)
            scr.addnstr(row, 0, line, w - 1, attr)
            row += 1
        scr.addnstr(h - 1, 0, " %d selected " % len(sel), w - 1, curses.A_BOLD)
        scr.refresh()

        k = scr.getch()
        if k in (curses.KEY_DOWN, ord("j")):
            pos = min(len(items) - 1, pos + 1)
        elif k in (curses.KEY_UP, ord("k")):
            pos = max(0, pos - 1)
        elif k == ord(" "):
            sel.symmetric_difference_update({pos})
        elif k == ord("a"):
            sel = set(range(len(items)))
        elif k == ord("n"):
            sel = set()
        elif k in (ord("q"), 27):
            return []
        elif k in (10, 13, curses.KEY_ENTER):
            return [items[i] for i in sorted(sel)]
