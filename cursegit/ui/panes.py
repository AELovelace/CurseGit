"""Reusable panes: a selectable list and a scrollable text viewer."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .draw import put, sanitize, skip_cells
from .theme import A, diff_attr


@dataclass
class Row:
    text: str
    attr: int = 0
    data: Any = None
    key: Any = None            # stable identity used to keep the cursor across refreshes
    selectable: bool = True
    code: str = ""             # short status column (file status, stash index, ...)
    code_attr: int = 0


class ListPane:
    def __init__(self) -> None:
        self.rows: list[Row] = []
        self.cursor = 0
        self.top = 0
        self._code_width = 2
        self._has_codes = False

    # -- content ------------------------------------------------------------

    def set_rows(self, rows: list[Row]) -> None:
        old = self.selected
        old_key = old.key if old is not None else None
        self.rows = rows
        self._has_codes = any(row.code for row in rows)
        self._code_width = max((len(row.code) for row in rows), default=0) or 2
        idx = None
        if old_key is not None:
            for i, r in enumerate(rows):
                if r.selectable and r.key == old_key:
                    idx = i
                    break
        if idx is None:
            idx = min(self.cursor, len(rows) - 1)
        self.cursor = max(idx, 0)
        self._snap(+1)

    def _snap(self, direction: int) -> None:
        n = len(self.rows)
        if n == 0:
            self.cursor = 0
            return
        i = self.cursor
        while 0 <= i < n and not self.rows[i].selectable:
            i += direction
        if not 0 <= i < n:
            i = self.cursor
            while 0 <= i < n and not self.rows[i].selectable:
                i -= direction
        if 0 <= i < n:
            self.cursor = i

    @property
    def selected(self) -> Row | None:
        if 0 <= self.cursor < len(self.rows) and self.rows[self.cursor].selectable:
            return self.rows[self.cursor]
        return None

    @property
    def has_code_column(self) -> bool:
        return self._has_codes

    @property
    def code_width(self) -> int:
        """Width of the status/hash column: widest code, at least 2."""
        return self._code_width

    # -- navigation ---------------------------------------------------------

    def move(self, delta: int) -> None:
        n = len(self.rows)
        if n == 0:
            return
        step = 1 if delta > 0 else -1
        i = self.cursor
        for _ in range(abs(delta)):
            j = i + step
            while 0 <= j < n and not self.rows[j].selectable:
                j += step
            if not 0 <= j < n:
                break
            i = j
        self.cursor = i

    def page(self, direction: int, height: int) -> None:
        self.move(direction * max(1, height - 1))

    def home(self) -> None:
        self.cursor = 0
        self._snap(+1)

    def end(self) -> None:
        self.cursor = max(0, len(self.rows) - 1)
        self._snap(-1)

    def index_at(self, offset: int) -> int | None:
        """Row index for a screen line `offset` lines below the top of the pane, if it is selectable."""
        idx = self.top + offset
        if 0 <= idx < len(self.rows) and self.rows[idx].selectable:
            return idx
        return None

    def select(self, idx: int) -> None:
        if 0 <= idx < len(self.rows) and self.rows[idx].selectable:
            self.cursor = idx

    # -- drawing ------------------------------------------------------------

    def draw(self, win, y: int, x: int, h: int, w: int, focused: bool) -> None:
        if h <= 0 or w <= 0:
            return
        n = len(self.rows)
        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + h:
            self.top = self.cursor - h + 1
        self.top = max(0, min(self.top, max(0, n - h)))
        code_col = self.has_code_column
        code_w = max(2, self.code_width)
        for i in range(h):
            idx = self.top + i
            if idx >= n:
                break
            r = self.rows[idx]
            sel = idx == self.cursor and r.selectable
            base = (A["selected"] if focused else A["selected_blur"]) if sel else 0
            col = x
            put(win, y + i, col, ">" if sel else " ", base, 1)
            col += 1
            # A selected row is drawn entirely in the selection color: color pairs cannot be OR-ed together.
            if r.selectable and code_col:
                code = f" {r.code:<{code_w}} "
                put(win, y + i, col, code, base if sel else r.code_attr, code_w + 2)
                col += code_w + 2
            else:
                col += 1
            put(win, y + i, col, r.text, base if sel else r.attr, x + w - col, ellipsis=True)


class TextPane:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.top = 0
        self.left = 0

    def set_text(self, text: str, keep_scroll: bool = False) -> None:
        self.lines = text.splitlines() if text else []
        if not keep_scroll:
            self.top = 0
            self.left = 0
        else:
            self.top = max(0, min(self.top, max(0, len(self.lines) - 1)))

    def scroll(self, delta: int, height: int) -> None:
        self.top = max(0, min(self.top + delta, max(0, len(self.lines) - height)))

    def hscroll(self, delta: int) -> None:
        self.left = max(0, self.left + delta)

    def home(self) -> None:
        self.top = 0

    def end(self, height: int) -> None:
        self.top = max(0, len(self.lines) - height)

    def position(self, height: int) -> str:
        n = len(self.lines)
        if n == 0:
            return ""
        last = min(n, self.top + height)
        return f"{self.top + 1}-{last}/{n}"

    def draw(self, win, y: int, x: int, h: int, w: int, colorize: bool = True) -> None:
        if h <= 0 or w <= 0:
            return
        self.top = max(0, min(self.top, max(0, len(self.lines) - h)))
        for i in range(h):
            idx = self.top + i
            if idx >= len(self.lines):
                break
            line = self.lines[idx]
            attr = diff_attr(line) if colorize else 0
            if self.left:
                line = skip_cells(sanitize(line, max_cells=self.left + w + 2), self.left)
            # put() sanitizes only the visible prefix; don't rescan the whole diff line here.
            put(win, y + i, x, line, attr, w)
