"""Low-level drawing and input helpers that keep curses from throwing on us.

- Text is measured in terminal cells (wide East-Asian chars count 2) so columns line up.
- `put` never raises: it clips to the window and swallows the bottom-right-cell error.
- `read_key` works with or without `get_wch` (windows-curses may lack it).
"""
from __future__ import annotations

import curses
import sys
import unicodedata

ELLIPSIS = "…"


def cell_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    cat = unicodedata.category(ch)
    if cat in ("Cc", "Cf", "Mn", "Me"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(s: str) -> int:
    return sum(cell_width(c) for c in s)


def sanitize(s: str, tab: int = 4) -> str:
    out = []
    for ch in s:
        if ch == "\t":
            out.append(" " * tab)
        elif ch in "\r\n":
            continue
        elif unicodedata.category(ch) == "Cc":
            out.append("?")
        else:
            out.append(ch)
    return "".join(out)


def clip(s: str, width: int, ellipsis: bool = False) -> str:
    """Truncate `s` to at most `width` cells."""
    if width <= 0:
        return ""
    if display_width(s) <= width:
        return s
    if ellipsis and width > 1:
        width -= 1
    out = []
    used = 0
    for ch in s:
        w = cell_width(ch)
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out) + (ELLIPSIS if ellipsis else "")


def skip_cells(s: str, n: int) -> str:
    """Drop the first `n` cells of `s` (for horizontal scrolling)."""
    if n <= 0:
        return s
    used = 0
    for i, ch in enumerate(s):
        if used >= n:
            return s[i:]
        used += cell_width(ch)
    return ""


def fit(s: str, width: int, ellipsis: bool = False) -> str:
    s = clip(s, width, ellipsis)
    return s + " " * max(0, width - display_width(s))


def put(win, y: int, x: int, s: str, attr: int = 0, width: int | None = None, ellipsis: bool = False) -> None:
    """Write `s` at (y, x) padded/clipped to `width` cells (default: to the right edge)."""
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    avail = w - x
    if width is None or width > avail:
        width = avail
    if width <= 0:
        return
    text = fit(sanitize(s), width, ellipsis)
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        # Writing into the bottom-right cell raises after the text is drawn. Harmless.
        pass


def hline(win, y: int, x: int, width: int, attr: int = 0, ch: str = "─") -> None:
    put(win, y, x, ch * max(0, width), attr, width)


def vline(win, y: int, x: int, height: int, attr: int = 0, ch: str = "│") -> None:
    for i in range(height):
        put(win, y + i, x, ch, attr, 1)


# -- input ------------------------------------------------------------------

def read_key(win):
    """Return a key: str for printable/control chars, int for curses special keys, None on timeout."""
    get_wch = getattr(win, "get_wch", None)
    if get_wch is not None:
        try:
            return get_wch()
        except curses.error:
            return None
    k = win.getch()
    if k == -1:
        return None
    if k < 0x80:
        return chr(k)
    if k < 0x100:
        # PDCurses (windows-curses) returns Unicode code points; ncurses returns UTF-8 bytes.
        if sys.platform == "win32":
            return chr(k)
        return _decode_utf8_tail(win, k)
    return k


def _decode_utf8_tail(win, first: int) -> str:
    if 0xC2 <= first <= 0xDF:
        need = 1
    elif 0xE0 <= first <= 0xEF:
        need = 2
    elif 0xF0 <= first <= 0xF4:
        need = 3
    else:
        return "?"
    buf = bytes([first])
    win.nodelay(True)
    try:
        for _ in range(need):
            b = win.getch()
            if b == -1 or b >= 0x100:
                break
            buf += bytes([b])
    finally:
        win.nodelay(False)
    return buf.decode("utf-8", "replace")


_SPECIAL = {
    "KEY_UP": "up", "KEY_DOWN": "down", "KEY_LEFT": "left", "KEY_RIGHT": "right",
    "KEY_NPAGE": "pgdn", "KEY_PPAGE": "pgup", "KEY_HOME": "home", "KEY_END": "end",
    "KEY_ENTER": "enter", "KEY_BACKSPACE": "backspace", "KEY_DC": "delete", "KEY_IC": "insert",
    "KEY_RESIZE": "resize", "KEY_BTAB": "shift-tab", "KEY_F5": "f5", "KEY_F1": "f1",
    "KEY_MOUSE": "mouse",
}

# Mouse button state bits. windows-curses translates PDCurses wheel events into the ncurses
# BUTTON4/BUTTON5 "pressed" bits; raw PDCurses builds use 0x10000/0x20000 for the wheel.
WHEEL_UP = getattr(curses, "BUTTON4_PRESSED", 0) | (0x10000 if sys.platform == "win32" else 0)
WHEEL_DOWN = getattr(curses, "BUTTON5_PRESSED", 0) | (0x20000 if sys.platform == "win32" else 0)
CLICK = getattr(curses, "BUTTON1_CLICKED", 0) | getattr(curses, "BUTTON1_PRESSED", 0) \
    | getattr(curses, "BUTTON1_RELEASED", 0)
DOUBLE_CLICK = getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0)


def enable_mouse() -> bool:
    """Ask curses for mouse events. Returns False when the terminal has no mouse support."""
    try:
        avail, _old = curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    except (curses.error, AttributeError):
        return False
    return bool(avail)


def disable_mouse() -> None:
    try:
        curses.mousemask(0)
    except (curses.error, AttributeError):
        pass


def read_mouse():
    """Return (x, y, bstate) for the pending mouse event, or None."""
    try:
        _id, x, y, _z, bstate = curses.getmouse()
    except curses.error:
        return None
    return x, y, bstate
_SPECIAL_CODES = {getattr(curses, name): short for name, short in _SPECIAL.items() if hasattr(curses, name)}
if hasattr(curses, "KEY_F0"):
    _SPECIAL_CODES[curses.KEY_F0 + 5] = "f5"
    _SPECIAL_CODES[curses.KEY_F0 + 1] = "f1"


def key_name(k) -> str:
    """Normalise a key from read_key into a short name used by the keymap."""
    if k is None:
        return ""
    if isinstance(k, str):
        if k in ("\n", "\r"):
            return "enter"
        if k == "\t":
            return "tab"
        if k == "\x1b":
            return "esc"
        if k == " ":
            return "space"
        o = ord(k) if len(k) == 1 else -1
        if o in (8, 127):
            return "backspace"
        if 0 <= o < 32:
            return "ctrl-" + chr(o + 96)
        return k
    return _SPECIAL_CODES.get(k, f"key{k}")
