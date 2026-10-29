"""Modal popups: confirm, single-line prompt, multiline editor, scrollable text, picker.

Each popup blocks until the user answers, then returns. The caller redraws the screen afterwards.
"""
from __future__ import annotations

import curses
import textwrap

from .draw import (WHEEL_DOWN, WHEEL_UP, cell_width, display_width, fit, key_name, put, read_key,
                   read_mouse, sanitize)
from .panes import ListPane, Row, TextPane
from .theme import A


def _set_cursor(visible: bool) -> None:
    try:
        curses.curs_set(1 if visible else 0)
    except curses.error:
        pass


def _geometry(scr, want_h: int, want_w: int) -> tuple[int, int, int, int]:
    H, W = scr.getmaxyx()
    h = max(3, min(want_h, H - 2))
    w = max(10, min(want_w, W - 2))
    return h, w, (H - h) // 2, (W - w) // 2


def _box(scr, want_h: int, want_w: int, title: str):
    h, w, y, x = _geometry(scr, want_h, want_w)
    win = curses.newwin(h, w, y, x)
    win.keypad(True)
    win.timeout(-1)
    win.erase()
    try:
        win.attrset(A["border"])
        win.box()
        win.attrset(0)
    except curses.error:
        pass
    if title:
        t = f" {title} "
        put(win, 0, 2, t, A["title"], min(display_width(t), w - 4), ellipsis=True)
    return win, h, w


def _footer(win, h: int, w: int, text: str) -> None:
    put(win, h - 1, 2, f" {text} ", A["dim"], min(display_width(text) + 2, w - 4))


def _wrap(text: str, width: int) -> list[str]:
    out: list[str] = []
    for para in text.splitlines() or [""]:
        out.extend(textwrap.wrap(para, width) or [""])
    return out


# -- confirm ----------------------------------------------------------------

def confirm(scr, title: str, text: str, danger: bool = False) -> bool:
    """Yes/no. With danger=True only an explicit 'y' confirms (Enter does not)."""
    while True:
        H, W = scr.getmaxyx()
        width = min(max(44, min(len(text) + 6, 76)), W - 2)
        lines = _wrap(text, width - 4)
        win, h, w = _box(scr, len(lines) + 4, width, title)
        for i, ln in enumerate(lines):
            put(win, 1 + i, 2, ln, 0, w - 4)
        hint = "[y] yes   [n] no" if danger else "[y/Enter] yes   [n/Esc] no"
        put(win, h - 2, 2, hint, A["error"] if danger else A["hint"], w - 4)
        win.refresh()
        name = key_name(read_key(win))
        if name in ("y", "Y") or (name == "enter" and not danger):
            return True
        if name in ("n", "N", "esc", "q"):
            return False
        if name == "resize":
            continue


# -- text editor -------------------------------------------------------------

def edit_text(scr, title: str, initial: str = "", single_line: bool = False,
              hint: str | None = None, secret: bool = False) -> str | None:
    """Multiline editor. Ctrl-S saves (Enter saves in single-line mode). Esc cancels.

    With secret=True the text is shown as asterisks (for tokens).
    """
    lines = initial.split("\n") if initial else [""]
    cy, cx = len(lines) - 1, len(lines[-1])
    if single_line:
        lines = [lines[0]]
        cy, cx = 0, len(lines[0])
    if hint is None:
        hint = "Enter: save   Esc: cancel" if single_line else "Ctrl-S: save   Enter: newline   Esc: cancel"
    top = 0
    _set_cursor(True)
    try:
        while True:
            H, W = scr.getmaxyx()
            want_h = 4 if single_line else min(max(10, len(lines) + 4), H - 2)
            want_w = min(max(60, W * 7 // 10), W - 2)
            win, h, w = _box(scr, want_h, want_w, title)
            rows, cols = h - 3, w - 4
            if cy < top:
                top = cy
            elif cy >= top + rows:
                top = cy - rows + 1
            # horizontal scroll by display cells
            cur_cells = display_width(lines[cy][:cx])
            left = 0
            if cur_cells >= cols:
                left = cur_cells - cols + 1
            for i in range(rows):
                idx = top + i
                if idx >= len(lines):
                    break
                shown = "*" * len(lines[idx]) if secret else sanitize(lines[idx])
                visible = _skip_cells_str(shown, left)
                put(win, 1 + i, 2, visible, 0, cols)
            _footer(win, h, w, hint)
            try:
                win.move(1 + (cy - top), 2 + min(cols - 1, cur_cells - left))
            except curses.error:
                pass
            win.refresh()

            k = read_key(win)
            name = key_name(k)
            if name == "esc":
                return None
            if name == "ctrl-s" or (name == "enter" and single_line):
                return "\n".join(lines)
            if name == "resize":
                continue
            if name == "enter":
                lines.insert(cy + 1, lines[cy][cx:])
                lines[cy] = lines[cy][:cx]
                cy, cx = cy + 1, 0
            elif name == "backspace":
                if cx > 0:
                    lines[cy] = lines[cy][:cx - 1] + lines[cy][cx:]
                    cx -= 1
                elif cy > 0:
                    cx = len(lines[cy - 1])
                    lines[cy - 1] += lines[cy]
                    del lines[cy]
                    cy -= 1
            elif name == "delete":
                if cx < len(lines[cy]):
                    lines[cy] = lines[cy][:cx] + lines[cy][cx + 1:]
                elif cy + 1 < len(lines):
                    lines[cy] += lines[cy + 1]
                    del lines[cy + 1]
            elif name == "left":
                if cx > 0:
                    cx -= 1
                elif cy > 0:
                    cy -= 1
                    cx = len(lines[cy])
            elif name == "right":
                if cx < len(lines[cy]):
                    cx += 1
                elif cy + 1 < len(lines):
                    cy, cx = cy + 1, 0
            elif name == "up" and cy > 0:
                cy -= 1
                cx = min(cx, len(lines[cy]))
            elif name == "down" and cy + 1 < len(lines):
                cy += 1
                cx = min(cx, len(lines[cy]))
            elif name == "home":
                cx = 0
            elif name == "end":
                cx = len(lines[cy])
            elif name == "ctrl-k":
                lines[cy] = lines[cy][:cx]
            elif isinstance(k, str) and len(k) >= 1 and not name.startswith("ctrl-") and name not in ("tab",):
                if k == "\t":
                    k = "    "
                lines[cy] = lines[cy][:cx] + k + lines[cy][cx:]
                cx += len(k)
    finally:
        _set_cursor(False)


def _skip_cells_str(s: str, n: int) -> str:
    used = 0
    for i, ch in enumerate(s):
        if used >= n:
            return s[i:]
        used += cell_width(ch)
    return "" if n > 0 else s


def prompt(scr, title: str, default: str = "", secret: bool = False) -> str | None:
    return edit_text(scr, title, default, single_line=True, secret=secret)


def overlay(scr, title: str, lines: list[str], footer: str = "") -> None:
    """Draw a message box over the current screen without waiting for input (caller keeps looping)."""
    H, W = scr.getmaxyx()
    width = min(max(50, max((display_width(ln) for ln in lines), default=0) + 6), W - 2)
    win, h, w = _box(scr, len(lines) + 4, width, title)
    for i, ln in enumerate(lines):
        put(win, 1 + i, 2, ln, A["title"] if ln.strip() and ln == ln.upper() and len(ln) <= 12 else 0,
            w - 4, ellipsis=True)
    if footer:
        _footer(win, h, w, footer)
    win.refresh()


# -- scrollable text ---------------------------------------------------------

def show_text(scr, title: str, text: str, colorize: bool = True) -> None:
    pane = TextPane()
    pane.set_text(text or "(empty)")
    while True:
        H, W = scr.getmaxyx()
        win, h, w = _box(scr, H - 2, W - 2, title)
        rows = h - 2
        pane.draw(win, 1, 2, rows, w - 4, colorize)
        _footer(win, h, w, f"{pane.position(rows)}   j/k scroll   PgUp/PgDn   g/G   q/Esc close")
        win.refresh()
        name = key_name(read_key(win))
        if name in ("q", "esc", "enter"):
            return
        if name == "mouse":
            ev = read_mouse()
            if ev is not None:
                if ev[2] & WHEEL_UP:
                    pane.scroll(-3, rows)
                elif ev[2] & WHEEL_DOWN:
                    pane.scroll(3, rows)
            continue
        if name in ("j", "down"):
            pane.scroll(1, rows)
        elif name in ("k", "up"):
            pane.scroll(-1, rows)
        elif name in ("pgdn", "space", "ctrl-f", "ctrl-d"):
            pane.scroll(rows - 1, rows)
        elif name in ("pgup", "ctrl-b", "ctrl-u"):
            pane.scroll(-(rows - 1), rows)
        elif name in ("g", "home"):
            pane.home()
        elif name in ("G", "end"):
            pane.end(rows)
        elif name in ("h", "left"):
            pane.hscroll(-8)
        elif name in ("l", "right"):
            pane.hscroll(8)


# -- picker --------------------------------------------------------------------

def pick(scr, title: str, options: list[str]) -> int | None:
    pane = ListPane()
    pane.set_rows([Row(o, key=i, data=i) for i, o in enumerate(options)])
    while True:
        H, W = scr.getmaxyx()
        width = min(max(40, max((display_width(o) for o in options), default=0) + 8), W - 2)
        win, h, w = _box(scr, len(options) + 3, width, title)
        rows = h - 2
        pane.draw(win, 1, 1, rows, w - 2, True)
        _footer(win, h, w, "Enter: choose   Esc: cancel")
        win.refresh()
        name = key_name(read_key(win))
        if name in ("esc", "q"):
            return None
        if name == "enter":
            sel = pane.selected
            return sel.data if sel else None
        if name in ("j", "down"):
            pane.move(1)
        elif name in ("k", "up"):
            pane.move(-1)
        elif name in ("g", "home"):
            pane.home()
        elif name in ("G", "end"):
            pane.end()
        elif name.isdigit() and 1 <= int(name) <= len(options):
            return int(name) - 1


__all__ = ["confirm", "edit_text", "prompt", "show_text", "pick", "fit"]
