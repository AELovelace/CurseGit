"""Color pairs and attributes: a pink theme.

Uses 256-color pinks when the terminal offers them, plain magenta on 8-color terminals, and bare
attributes on monochrome ones. Semantic diff/status colors (green added, red removed, yellow modified)
stay conventional so they remain readable.
"""
from __future__ import annotations

import curses

# name -> curses attribute, filled by init_theme()
A: dict[str, int] = {}

_NAMES = [
    "header", "tab", "tab_active", "section", "selected", "selected_blur", "added", "removed", "hunk",
    "meta", "error", "ok", "dim", "hint", "mod", "add", "del", "ren", "untracked", "conflict", "title",
    "border",
]


def _palette() -> dict[str, int]:
    """Pick pink shades available on this terminal."""
    colors = getattr(curses, "COLORS", 8)
    if colors >= 256:
        return {
            "hot": 205,     # hot pink: header / selection background
            "pink": 212,    # orchid: borders, section headers, hunk headers
            "light": 218,   # light pink: titles, hints
            "pale": 225,    # pale pink: inactive tabs, ok messages
            "white": 231,
            "black": 16,
            "green": 114,
            "red": 203,
            "yellow": 222,
            "blue": 147,
        }
    m = curses.COLOR_MAGENTA
    return {
        "hot": m, "pink": m, "light": m, "pale": m,
        "white": curses.COLOR_WHITE, "black": curses.COLOR_BLACK,
        "green": curses.COLOR_GREEN, "red": curses.COLOR_RED,
        "yellow": curses.COLOR_YELLOW, "blue": curses.COLOR_BLUE,
    }


def init_theme() -> None:
    for n in _NAMES:
        A[n] = 0
    has_color = False
    default_bg = curses.COLOR_BLACK
    try:
        curses.start_color()
        has_color = curses.has_colors()
        try:
            curses.use_default_colors()
            default_bg = -1
        except curses.error:
            pass
    except curses.error:
        has_color = False

    bold = curses.A_BOLD
    if has_color:
        p = _palette()
        pairs = {
            # name: (fg, bg) ; None bg means terminal default
            "header": (p["white"], p["hot"]),
            "tab_active": (p["black"], p["hot"]),
            "tab": (p["pale"], None),
            "selected": (p["black"], p["pink"]),
            "selected_blur": (p["light"], None),
            "section": (p["pink"], None),
            "title": (p["light"], None),
            "border": (p["pink"], None),
            "hint": (p["light"], None),
            "ok": (p["pale"], None),
            "hunk": (p["pink"], None),
            "meta": (p["light"], None),
            "added": (p["green"], None),
            "removed": (p["red"], None),
            "error": (p["red"], None),
            "mod": (p["yellow"], None),
            "add": (p["green"], None),
            "del": (p["red"], None),
            "ren": (p["blue"], None),
            "untracked": (p["green"], None),
            "conflict": (p["red"], None),
        }
        for i, (name, (fg, bg)) in enumerate(pairs.items(), start=1):
            try:
                curses.init_pair(i, fg, default_bg if bg is None else bg)
                A[name] = curses.color_pair(i)
            except curses.error:
                A[name] = 0
        A["dim"] = curses.A_DIM
    else:
        A["tab"] = curses.A_DIM
        A["tab_active"] = bold | curses.A_REVERSE
        A["selected"] = curses.A_REVERSE
        A["selected_blur"] = bold | curses.A_UNDERLINE
        A["dim"] = curses.A_DIM

    for n in ("header", "section", "error", "conflict", "meta", "title", "tab_active", "selected_blur"):
        A[n] |= bold


def code_attr(code: str) -> int:
    """Attribute for a file status code column."""
    if code == "??":
        return A["untracked"]
    if len(code) == 2:
        return A["conflict"]
    return {
        "M": A["mod"], "T": A["mod"],
        "A": A["add"],
        "D": A["del"],
        "R": A["ren"], "C": A["ren"],
    }.get(code, 0)


def diff_attr(line: str) -> int:
    if line.startswith(("+++ ", "--- ")):
        return A["meta"]
    if line.startswith("+"):
        return A["added"]
    if line.startswith("-"):
        return A["removed"]
    if line.startswith("@@"):
        return A["hunk"]
    if line.startswith(("diff ", "index ", "new file", "deleted file", "rename ", "similarity",
                        "old mode", "new mode", "commit ", "Author:", "Date:", "Merge:", "Binary files")):
        return A["meta"]
    return 0
