"""The main curses application: layout, key dispatch, and the actions behind every key."""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import contextlib
import curses
import os
import threading
import time
import webbrowser

from .. import github
from ..git import GitError, GitRepo
from ..model import Branch, Commit, FileChange, RepoState, Section, Stash
from ..worker import Worker
from . import keys, popups
from .draw import (CLICK, DOUBLE_CLICK, RIGHT_CLICK, WHEEL_DOWN, WHEEL_UP, disable_mouse, display_width, enable_mouse,
                   hline, key_name, put, read_key, read_mouse, vline)
from .panes import ListPane, Row, TextPane
from .theme import A, code_attr, init_theme

TAB_TITLES = ("Status", "Branches", "Stash", "Log")
SPINNER = "|/-\\"
SPLIT_MIN_WIDTH = 90

HINTS = {
    "status": "s stage  u unstage  c commit  p pull  P push  y sync  @ github  ? help",
    "branches": "Enter checkout  n new  D delete  m merge  [ ] tabs  ? help",
    "stash": "a apply  A pop  D drop  z stash  [ ] tabs  ? help",
    "log": "Enter show  b branch here  [ ] tabs  ? help",
}


class App:
    def __init__(self, repo: GitRepo, refresh_interval: float = 5.0, tty_mode: bool = False,
                 mouse: bool = True) -> None:
        self.repo = repo
        self.repo.tty_mode = tty_mode
        self.refresh_interval = refresh_interval
        self.want_mouse = mouse
        self.mouse_on = False
        self.geom: dict = {}   # where things were drawn last frame, for mouse hit-testing
        self.auth = github.load_auth()
        self.has_remote = True
        self.pending_login: github.DeviceCode | None = None
        self.login_cancel = threading.Event()
        self._apply_auth()
        self.worker = Worker()
        self.state = RepoState()
        self.tab = 0
        self.lists = [ListPane() for _ in TAB_TITLES]
        self.text = TextPane()
        self.focus = "list"
        self.msg = ""
        self.msg_err = False
        self.last_output = ""
        self.branches: list[Branch] = []
        self.commits: list[Commit] = []
        self.stale = {"branches": True, "log": True}
        self.detail_key: object = None
        self.detail_title = ""
        self.last_refresh = 0.0
        self.need_draw = True
        self.spin = 0
        self.running = True
        self.scr = None
        self.body_h = 10
        self._last_click = (0.0, None)  # Timestamp and row identity for immediate Windows double-clicks.
        self._next_spin = 0.0
        self._reads = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cursegit-read")
        self._detail_future = None
        self._refresh_future = None
        self._pending_detail = None
        self._detail_serial = 0
        self._detail_due = 0.0
        self._epoch = 0
        self._detail_cache = OrderedDict()
        self._displayed_detail_key = None

    # -- properties -----------------------------------------------------------

    @property
    def list(self) -> ListPane:
        return self.lists[self.tab]

    @property
    def scope(self) -> str:
        return keys.SCOPES[self.tab]

    @property
    def split(self) -> bool:
        return self.scr is not None and self.scr.getmaxyx()[1] >= SPLIT_MIN_WIDTH

    # -- main loop ------------------------------------------------------------

    def run(self, scr) -> None:
        """Own the read worker's lifetime so closing the UI never leaves reads behind."""
        try:
            self._run(scr)
        finally:
            self._reads.shutdown(wait=True, cancel_futures=True)

    def _run(self, scr) -> None:
        self.scr = scr
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if hasattr(curses, "set_escdelay"):
            with contextlib.suppress(Exception):
                curses.set_escdelay(25)
        scr.keypad(True)
        scr.timeout(100)
        init_theme()
        if self.want_mouse:
            self.mouse_on = enable_mouse()
        self.refresh()
        while self.running:
            self._poll_reads()
            reading = self._pending_detail or self._detail_future or self._refresh_future
            scr.timeout(30 if reading or self.worker.busy else 250)
            now = time.monotonic()
            if self.need_draw or (self.worker.busy and now >= self._next_spin):
                self.draw()
                self.need_draw = False
                self._next_spin = now + 0.1  # Spinner updates at 10 Hz, regardless of input rate.
            k = read_key(scr)
            if k is not None:
                name = key_name(k)
                if name != "mouse":
                    self.need_draw = True
                if name == "resize":
                    self.on_resize()
                elif name == "mouse":
                    try:
                        self.on_mouse()
                    except GitError as e:
                        self.fail(e)
                else:
                    try:
                        self.handle(name)
                    except GitError as e:
                        self.fail(e)
            for done in self.worker.poll():
                self.on_done(done)
            if (self.refresh_interval > 0 and not self.worker.busy
                    and time.monotonic() - self.last_refresh > self.refresh_interval):
                self._request_refresh()  # Status scans run beside the UI, not inside input dispatch.

    def on_resize(self) -> None:
        if hasattr(curses, "resize_term"):
            with contextlib.suppress(curses.error):
                curses.resize_term(0, 0)
        self.scr.clear()
        if not self.split:
            self.focus = "list"

    # -- messages -------------------------------------------------------------

    def note(self, text: str, err: bool = False) -> None:
        self.msg = text.strip().splitlines()[-1] if text.strip() else ""
        self.msg_err = err
        self.need_draw = True

    def fail(self, e: GitError) -> None:
        self.last_output = e.output
        self.note(e.message + "   (! for details)", err=True)

    # -- data -----------------------------------------------------------------

    def refresh(self) -> None:
        self._epoch += 1
        self._detail_cache.clear()  # Mutations/manual refresh invalidate cached previews and in-flight results.
        try:
            self.state = self.repo.status()
        except GitError as e:
            self.fail(e)
            self.state = RepoState()
        self.last_refresh = time.monotonic()
        try:
            self.has_remote = bool(self.repo.remotes())
        except GitError:
            self.has_remote = True
        self.rebuild_rows()
        self.update_detail(force=True)
        self.need_draw = True

    def mark_stale(self) -> None:
        self.stale["branches"] = True
        self.stale["log"] = True

    def rebuild_rows(self) -> None:
        if self.tab == 0:
            self.list.set_rows(self._status_rows())
        elif self.tab == 1:
            if self.stale["branches"]:
                try:
                    self.branches = self.repo.branches()
                except GitError as e:
                    self.fail(e)
                self.stale["branches"] = False
            self.list.set_rows(self._branch_rows())
        elif self.tab == 2:
            self.list.set_rows(self._stash_rows())
        else:
            if self.stale["log"]:
                self.commits = self.repo.log()
                self.stale["log"] = False
            self.list.set_rows(self._log_rows())

    def _status_rows(self) -> list[Row]:
        rows: list[Row] = []
        for section in (Section.CONFLICT, Section.STAGED, Section.UNSTAGED, Section.UNTRACKED):
            items = self.state.in_section(section)
            if not items and section in (Section.CONFLICT, Section.UNTRACKED):
                continue
            rows.append(Row(f"{section.value} ({len(items)})", A["section"], selectable=False))
            for fc in items:
                rows.append(Row(fc.display, 0, data=fc, key=(section, fc.path), code=fc.code,
                                code_attr=code_attr(fc.code)))
        if not self.state.files:
            rows.append(Row("Working tree clean", A["dim"], selectable=False))
        return rows

    def _branch_rows(self) -> list[Row]:
        rows: list[Row] = []
        local = [b for b in self.branches if not b.is_remote]
        remote = [b for b in self.branches if b.is_remote]
        rows.append(Row(f"LOCAL ({len(local)})", A["section"], selectable=False))
        for b in local:
            extra = f"  {b.track}" if b.track else ("" if b.upstream else "  (no upstream)")
            rows.append(Row(f"{b.name}{extra}", A["ok"] if b.is_current else 0, data=b, key=b.name,
                            code="*" if b.is_current else "", code_attr=A["ok"]))
        if remote:
            rows.append(Row(f"REMOTE ({len(remote)})", A["section"], selectable=False))
            for b in remote:
                rows.append(Row(b.name, A["dim"], data=b, key=b.name))
        if not self.branches:
            rows.append(Row("No branches yet (make a commit first)", A["dim"], selectable=False))
        return rows

    def _stash_rows(self) -> list[Row]:
        rows = [Row(s.message, 0, data=s, key=s.ref, code=str(s.index)) for s in self.state.stashes]
        if not rows:
            rows.append(Row("No stashes", A["dim"], selectable=False))
        return rows

    def _log_rows(self) -> list[Row]:
        rows = [Row(f"{c.subject}  · {c.author}, {c.when}", 0, data=c, key=c.sha, code=c.short[:7])
                for c in self.commits]
        if not rows:
            rows.append(Row("No commits yet", A["dim"], selectable=False))
        return rows

    def update_detail(self, force: bool = False) -> None:
        """Queue only the latest selection; bursts of wheel/arrow input don't run one diff per row."""
        row = self.list.selected
        key = (self.tab, row.key if row else None)
        if key == self.detail_key and not force:
            return
        self.detail_key = key
        self._detail_serial += 1
        self._pending_detail = None
        if force:
            self._detail_cache.pop(key, None)
        if row is None:
            self.text.set_text("")
            self.detail_title = ""
            self._displayed_detail_key = None
        elif key in self._detail_cache:
            text, self.detail_title = self._detail_cache[key]
            self._detail_cache.move_to_end(key)
            self.text.set_text(text)
            self._displayed_detail_key = key
        else:
            if self._displayed_detail_key != key:
                self.text.set_text("Loading preview...")
                self.detail_title = row.text
            self._pending_detail = (self._detail_serial, self._epoch, key, row.data)
            self._detail_due = time.monotonic() + 0.06
        self.need_draw = True

    def _read_state(self, tab):
        """Read Git data only; background jobs never access curses or mutate UI state."""
        state = self.repo.status()
        remotes = bool(self.repo.remotes())
        branches = self.repo.branches() if tab == 1 else None
        commits = self.repo.log() if tab == 3 else None
        return state, remotes, branches, commits

    def _request_refresh(self) -> None:
        """Allow at most one periodic status scan to be queued or running."""
        if self._refresh_future is None:
            self._refresh_future = (self._epoch, self.tab, self._reads.submit(self._read_state, self.tab))
            self.last_refresh = time.monotonic()

    def _poll_reads(self) -> None:
        """Apply finished reads only if their project state and selected row are still current."""
        if self._refresh_future is not None:
            epoch, tab, future = self._refresh_future
            if future.done():
                self._refresh_future = None
                self.last_refresh = time.monotonic()  # Slow scans must leave time for previews before the next scan.
                if epoch == self._epoch and tab == self.tab and not self.worker.busy:
                    try:
                        self.state, self.has_remote, branches, commits = future.result()
                    except Exception as exc:
                        self.note(f"Refresh failed: {exc}", err=True)
                    else:
                        self._epoch += 1
                        self._detail_cache.clear()
                        self.mark_stale()
                        if branches is not None:
                            self.branches, self.stale["branches"] = branches, False
                        if commits is not None:
                            self.commits, self.stale["log"] = commits, False
                        self.rebuild_rows()
                        self.update_detail(force=True)
                        self.need_draw = True
        if self._detail_future is not None:
            serial, epoch, key, future = self._detail_future
            if future.done():
                self._detail_future = None
                if serial == self._detail_serial and epoch == self._epoch:
                    try:
                        text, title = future.result()
                    except GitError as exc:
                        text, title = exc.output, "error"
                    except Exception as exc:
                        text, title = str(exc), "preview error"
                    else:
                        if len(text) <= 1_000_000:
                            self._detail_cache[key] = (text, title)
                            self._detail_cache.move_to_end(key)
                            while len(self._detail_cache) > 16:
                                self._detail_cache.popitem(last=False)
                    self.text.set_text(text, keep_scroll=self._displayed_detail_key == key)
                    self.detail_title, self._displayed_detail_key = title, key
                    self.need_draw = True
        if (self._pending_detail is not None and self._detail_future is None
                and time.monotonic() >= self._detail_due):
            serial, epoch, key, data = self._pending_detail
            self._pending_detail = None
            self._detail_future = (serial, epoch, key, self._reads.submit(self._detail_for, data))

    def _detail_for(self, data) -> tuple[str, str]:
        if isinstance(data, FileChange):
            return self.repo.diff(data) or "(no changes)", f"{data.section.value.lower()}: {data.display}"
        if isinstance(data, Branch):
            commits = self.repo.log(60, data.name)
            lines = [f"{c.short} {c.subject}  · {c.author}, {c.when}" for c in commits]
            head = f"{data.name}  ->  {data.upstream or 'no upstream'}  {data.track}".rstrip()
            return "\n".join([head, ""] + lines), f"branch: {data.name}"
        if isinstance(data, Stash):
            return self.repo.stash_show(data.index), f"stash@{{{data.index}}}: {data.message}"
        if isinstance(data, Commit):
            return self.repo.show(data.sha), f"commit {data.short}: {data.subject}"
        return "", ""

    # -- drawing ------------------------------------------------------------

    def draw(self) -> None:
        scr = self.scr
        scr.erase()
        H, W = scr.getmaxyx()
        self.spin = (self.spin + 1) % len(SPINNER)
        self._draw_header(W)
        self._draw_tabs(W)
        body_y, body_h = 2, H - 4
        self.body_h = max(1, body_h)
        self.geom.update(body_y=body_y, body_h=body_h, width=W, height=H, split=self.split, list_w=W)
        if body_h >= 1:
            if self.split:
                lw = max(30, min(60, W * 2 // 5))
                self.geom["list_w"] = lw
                self.list.draw(scr, body_y, 0, body_h, lw, self.focus == "list")
                vline(scr, body_y, lw, body_h, A["border"])
                tx, tw = lw + 1, W - lw - 1
                title_attr = A["title"] if self.focus == "text" else A["dim"]
                pos = self.text.position(body_h - 1)
                put(scr, body_y, tx, f" {self.detail_title}", title_attr, max(0, tw - len(pos) - 2), ellipsis=True)
                put(scr, body_y, W - len(pos) - 1, pos, A["dim"])
                self.text.draw(scr, body_y + 1, tx + 1, body_h - 1, tw - 1)
            else:
                self.list.draw(scr, body_y, 0, body_h, W, True)
        self._draw_actions(H, W)
        self._draw_status(H, W)
        scr.refresh()
        if self.pending_login is not None:
            code = self.pending_login
            popups.overlay(scr, "Sign in to GitHub", [
                f"1. Open {code.verification_uri}  (your browser should have opened it)",
                "2. Enter this code:",
                "",
                f"      {code.user_code}",
                "",
                f"{SPINNER[self.spin]} Waiting for you to approve the sign-in on GitHub...",
            ], "Esc: cancel")

    def _draw_header(self, W: int) -> None:
        b = self.state.branch
        left = f" CurseGit  {self.repo.name}  "
        if b.unborn:
            branch = f"{b.head} (no commits yet)"
        elif b.upstream:
            branch = f"{b.head}  ↑{b.ahead} ↓{b.behind}"
        elif b.detached:
            branch = f"detached @ {b.oid[:7]}"
        else:
            branch = f"{b.head}  (no upstream)"
        counts = []
        if self.state.conflict_count:
            counts.append(f"{self.state.conflict_count} conflict")
        if self.state.staged_count:
            counts.append(f"{self.state.staged_count} staged")
        if self.state.changed_count:
            counts.append(f"{self.state.changed_count} changed")
        mid = "   ".join(counts)
        right = ""
        if self.worker.busy:
            right = f"{SPINNER[self.spin]} {self.worker.busy}... "
        elif self.repo.tty_mode:
            right = "[tty mode] "
        if self.auth.signed_in:
            right = f"@{self.auth.login}  {right}"
        put(self.scr, 0, 0, "", A["header"])
        put(self.scr, 0, 0, left + branch + ("   " + mid if mid else ""), A["header"], W - display_width(right), ellipsis=True)
        if right:
            put(self.scr, 0, W - display_width(right), right, A["header"])

    def _draw_tabs(self, W: int) -> None:
        x = 1
        spans = []
        for i, title in enumerate(TAB_TITLES):
            label = f" {i + 1} {title} "
            if i == 2 and self.state.stashes:
                label = f" {i + 1} {title} ({len(self.state.stashes)}) "
            attr = A["tab_active"] if i == self.tab else A["tab"]
            put(self.scr, 1, x, label, attr, display_width(label))
            spans.append((x, x + display_width(label), i))
            x += display_width(label) + 1
        self.geom["tabs"] = spans
        hline(self.scr, 1, x, W - x, A["dim"], " ")

    def _draw_status(self, H: int, W: int) -> None:
        hint = HINTS[self.scope]
        if not self.has_remote and self.scope == "status":
            hint = "P publish to GitHub  " + hint
        hint_w = display_width(hint)
        msg_w = 0
        if self.msg:
            # The message has priority; when both do not fit, the hints collapse to "? help".
            msg_w = display_width(self.msg)
            if msg_w + hint_w + 4 > W:
                hint = "? help"
                hint_w = display_width(hint)
                msg_w = min(msg_w, W - hint_w - 4)
            put(self.scr, H - 1, 1, self.msg, A["error"] if self.msg_err else A["ok"], msg_w, ellipsis=True)
        hint_x = W - hint_w - 1
        if hint_x >= (msg_w + 3 if self.msg else 1):
            put(self.scr, H - 1, hint_x, hint, A["dim"], hint_w)

    def _mouse_actions(self) -> list[tuple[str, str]]:
        """Keep buttons and the mouse menu on the same guarded action paths as keys."""
        scoped = {
            "status": [("Stage", "stage"), ("Unstage", "unstage"), ("Commit", "commit"),
                       ("Stage all", "stage_all"), ("Discard", "discard")],
            "branches": [("Checkout", "switch"), ("New", "new_branch"),
                         ("Merge", "merge"), ("Delete", "delete_branch")],
            "stash": [("Apply", "stash_apply"), ("Pop", "stash_pop"), ("Drop", "stash_drop")],
            "log": [("Show", "open_detail"), ("Branch here", "branch_at")],
        }
        return scoped[self.scope] + [
            ("Pull", "pull"), ("Push", "push"), ("Fetch", "fetch"),
            ("Refresh", "refresh"), ("GitHub", "github"), ("Help", "help"), ("Quit", "quit"),
        ]

    def _draw_actions(self, height: int, width: int) -> None:
        """Draw clickable controls and retain their exact cell rectangles for hit testing."""
        self.geom["actions"] = []
        x, y = 1, height - 2
        if y < 2:
            return
        for label, action in [("Actions", "action_menu"), *self._mouse_actions()]:
            text = f"[{label}]"
            size = display_width(text)
            if x + size >= width:
                break
            put(self.scr, y, x, text, A["hint"], size)
            self.geom["actions"].append((x, x + size, y, action))
            x += size + 1

    def act_action_menu(self) -> None:
        """Offer all actions when the window is too narrow for the button row."""
        actions = self._mouse_actions()
        seen = {action for _, action in actions}
        extra = keys.MUTATING_ACTIONS | {"open_detail", "last_output", "open_remote", "toggle_mouse", "toggle_tty"}
        for binding in keys.BINDINGS:
            if binding.scope in (self.scope, "global") and binding.action in extra and binding.action not in seen:
                actions.append((binding.help, binding.action))
                seen.add(binding.action)
        choice = popups.pick(self.scr, "Actions", [label for label, _ in actions])
        if choice is not None:
            self.handle_action(actions[choice][1])
        self.need_draw = True

    # -- mouse ------------------------------------------------------------------

    def on_mouse(self) -> None:
        ev = read_mouse()
        if ev is None or not self.geom or self.pending_login is not None:
            return
        x, y, b = ev
        g = self.geom
        if not (0 <= x < g["width"] and 0 <= y < g["height"]):
            return
        if not (b & (CLICK | DOUBLE_CLICK | RIGHT_CLICK | WHEEL_UP | WHEEL_DOWN)):
            return  # Releases and pointer motion never trigger another action or redraw.
        self.need_draw = True
        if b & (CLICK | DOUBLE_CLICK):
            for x0, x1, ay, action in g.get("actions", []):
                if y == ay and x0 <= x < x1:
                    self.handle_action(action)
                    return
        body_y, body_h = g["body_y"], g["body_h"]
        in_body = body_y <= y < body_y + body_h
        over_detail = in_body and g["split"] and x > g["list_w"]

        if b & WHEEL_UP or b & WHEEL_DOWN:
            step = -3 if b & WHEEL_UP else 3
            if over_detail:
                self.text.scroll(step, max(1, body_h - 1))
            elif in_body:
                self.list.move(step)
                self.update_detail()
            return

        if b & RIGHT_CLICK:
            if in_body and not over_detail:
                idx = self.list.index_at(y - body_y)
                if idx is not None:
                    self.list.select(idx)
                    self.update_detail()
            self.act_action_menu()
            return

        if not (b & CLICK or b & DOUBLE_CLICK):
            return

        if y == 1:  # tab bar
            for x0, x1, idx in g.get("tabs", []):
                if x0 <= x < x1:
                    self.switch_tab(idx)
                    return
            return

        if over_detail:
            self.focus = "text"
            return

        if in_body:
            self.focus = "list"
            idx = self.list.index_at(y - body_y)
            if idx is None:
                return
            self.list.select(idx)
            self.update_detail()
            now = time.monotonic()
            identity = (self.tab, self.list.selected.key, x, y)
            when, previous = self._last_click
            double = bool(b & DOUBLE_CLICK) or (identity == previous and now - when <= 0.4)
            self._last_click = (0.0, None) if double else (now, identity)
            if double:
                action = keys.lookup("enter", self.scope)
                if action:
                    self.handle_action(action)

    def act_toggle_mouse(self) -> None:
        if self.mouse_on:
            disable_mouse()
            self.mouse_on = False
            self.note("Mouse off")
        else:
            self.mouse_on = enable_mouse()
            self.note("Mouse on" if self.mouse_on else "This terminal reports no mouse support", err=not self.mouse_on)

    # -- dispatch -------------------------------------------------------------

    def handle(self, name: str) -> None:
        if self.pending_login is not None and name in ("esc", "q"):
            self.login_cancel.set()
            self.pending_login = None
            self.note("Sign-in cancelled")
            return
        if name in ("1", "2", "3", "4"):
            self.switch_tab(int(name) - 1)
            return
        action = keys.lookup(name, self.scope)
        if action is None:
            return
        self.handle_action(action)

    def handle_action(self, action: str) -> None:
        if action in keys.NAV_ACTIONS:
            self._navigate(action)
            return
        if action in keys.MUTATING_ACTIONS and self.worker.busy:
            self.note(f"Busy: {self.worker.busy} is still running", err=True)
            return
        fn = getattr(self, "act_" + action, None)
        if fn is None:
            self.note(f"Not implemented: {action}", err=True)
            return
        fn()

    def _navigate(self, action: str) -> None:
        h = self.body_h
        if self.focus == "text" or action in ("hleft", "hright"):
            th = max(1, h - 1)
            if action == "down":
                self.text.scroll(1, th)
            elif action == "up":
                self.text.scroll(-1, th)
            elif action == "pgdn":
                self.text.scroll(th - 1, th)
            elif action == "pgup":
                self.text.scroll(-(th - 1), th)
            elif action == "home":
                self.text.home()
            elif action == "end":
                self.text.end(th)
            elif action == "hleft":
                self.text.hscroll(-8)
            elif action == "hright":
                self.text.hscroll(8)
            return
        if action == "down":
            self.list.move(1)
        elif action == "up":
            self.list.move(-1)
        elif action == "pgdn":
            self.list.page(1, h)
        elif action == "pgup":
            self.list.page(-1, h)
        elif action == "home":
            self.list.home()
        elif action == "end":
            self.list.end()
        self.update_detail()

    def switch_tab(self, i: int) -> None:
        self.tab = i
        self.focus = "list"
        self.rebuild_rows()
        self.update_detail(force=True)

    def after_mutation(self, message: str | None = None, ok: bool = True) -> None:
        self.mark_stale()
        self.refresh()
        if message:
            self.note(message, err=not ok)

    # -- generic actions -------------------------------------------------------

    def act_quit(self) -> None:
        self.running = False

    def act_help(self) -> None:
        popups.show_text(self.scr, "Keys", "\n".join(keys.help_lines(self.scope)), colorize=False)

    def act_focus(self) -> None:
        if self.split:
            self.focus = "text" if self.focus == "list" else "list"

    def act_focus_detail(self) -> None:
        if self.split:
            self.focus = "text"
        else:
            self.act_open_detail()

    def act_focus_list(self) -> None:
        self.focus = "list"

    def act_next_tab(self) -> None:
        self.switch_tab((self.tab + 1) % len(TAB_TITLES))

    def act_prev_tab(self) -> None:
        self.switch_tab((self.tab - 1) % len(TAB_TITLES))

    def act_refresh(self) -> None:
        self.mark_stale()
        self.refresh()
        self.note("Refreshed")

    def act_open_detail(self) -> None:
        if self.list.selected is None:
            return
        if self._displayed_detail_key != self.detail_key:
            text, title = self._detail_for(self.list.selected.data)  # Explicit Open waits for that exact row.
        else:
            text, title = "\n".join(self.text.lines), self.detail_title
        popups.show_text(self.scr, title, text)

    def act_last_output(self) -> None:
        popups.show_text(self.scr, "Last git output", self.last_output or "(nothing yet)")

    def act_toggle_tty(self) -> None:
        self.repo.tty_mode = not self.repo.tty_mode
        self.note("Network commands now run " + ("in the terminal" if self.repo.tty_mode else "in the background"))

    def act_open_remote(self) -> None:
        url = self.repo.remote_web_url()
        if not url:
            self.note("No remote configured", err=True)
            return
        webbrowser.open(url)
        self.note(f"Opened {url}")

    # -- status tab --------------------------------------------------------------

    def _sel_file(self) -> FileChange | None:
        row = self.list.selected
        if self.tab == 0 and row is not None and isinstance(row.data, FileChange):
            return row.data
        if self.tab == 0:
            self.note("No file selected", err=True)
        return None

    def act_stage(self) -> None:
        fc = self._sel_file()
        if fc is None:
            return
        if fc.section is Section.STAGED:
            self.note("Already staged")
            return
        self.repo.stage(fc.paths)
        self.after_mutation(f"Staged {fc.path}")

    def act_unstage(self) -> None:
        fc = self._sel_file()
        if fc is None:
            return
        if fc.section is not Section.STAGED:
            self.note("Not staged")
            return
        self.repo.unstage(fc.paths)
        self.after_mutation(f"Unstaged {fc.path}")

    def act_toggle_stage(self) -> None:
        fc = self._sel_file()
        if fc is None:
            return
        if fc.section is Section.STAGED:
            self.act_unstage()
        else:
            self.act_stage()

    def act_stage_all(self) -> None:
        if self.state.changed_count == 0 and not self.state.has_conflicts:
            self.note("Nothing to stage")
            return
        self.repo.stage_all()
        if self.tab != 0:
            self.switch_tab(0)
        self.after_mutation()
        self.note(f"Staged all changes ({self.state.staged_count} file(s))")

    def act_unstage_all(self) -> None:
        if self.state.staged_count == 0:
            self.note("Nothing staged")
            return
        self.repo.unstage_all()
        if self.tab != 0:
            self.switch_tab(0)
        self.after_mutation("Unstaged all changes")

    def act_discard(self) -> None:
        fc = self._sel_file()
        if fc is None:
            return
        if fc.section is Section.STAGED:
            self.note("Unstage it first (u), then discard", err=True)
            return
        if fc.section is Section.CONFLICT:
            self.note("Resolve conflicts in your editor (e), then stage (s)", err=True)
            return
        what = "Delete untracked file" if fc.section is Section.UNTRACKED else "Discard changes to"
        if not popups.confirm(self.scr, "Discard", f"{what} {fc.path}? This cannot be undone.", danger=True):
            self.note("Cancelled")
            return
        self.repo.discard(fc)
        self.after_mutation(f"Discarded {fc.path}")

    def act_discard_all(self) -> None:
        n = self.state.changed_count
        if n == 0:
            self.note("Nothing to discard")
            return
        if not popups.confirm(self.scr, "Discard all",
                              f"Discard all {n} working tree change(s) and delete untracked files? "
                              "Staged changes are kept. This cannot be undone.", danger=True):
            self.note("Cancelled")
            return
        self.repo.discard_all()
        self.after_mutation("Discarded all working tree changes")

    def act_edit(self) -> None:
        fc = self._sel_file()
        if fc is None:
            return
        editor = self.repo.editor()
        with self.suspended(f"Opening {fc.path} in {editor}", wait=False):
            self.repo.run_tty(f'{editor} "{fc.path}"')
        self.after_mutation(f"Edited {fc.path}")

    # -- commits ---------------------------------------------------------------

    def act_commit(self) -> None:
        self._commit(amend=False)

    def act_amend(self) -> None:
        self._commit(amend=True)

    def _commit(self, amend: bool) -> None:
        if self.state.has_conflicts:
            self.note("Resolve conflicts first (e to edit, s to mark resolved)", err=True)
            return
        if amend and not self.repo.has_commits():
            self.note("Nothing to amend", err=True)
            return
        if not amend and self.state.staged_count == 0:
            if self.state.changed_count == 0:
                self.note("Nothing to commit")
                return
            if not popups.confirm(self.scr, "Commit", "There are no staged changes. Stage all changes and commit?"):
                self.note("Cancelled")
                return
            self.repo.stage_all()
            self.refresh()
        initial = self.repo.last_commit_message() if amend else ""
        title = "Amend commit message" if amend else f"Commit message ({self.state.staged_count} file(s))"
        msg = popups.edit_text(self.scr, title, initial)
        if msg is None or not msg.strip():
            self.note("Commit cancelled")
            return
        out = self.repo.commit(msg, amend=amend)
        self.last_output = out
        self.after_mutation(("Amended: " if amend else "Committed: ") + msg.strip().splitlines()[0])

    # -- network -----------------------------------------------------------------

    def _network(self, name: str, fn) -> None:
        if self.repo.tty_mode:
            with self.suspended(f"git {name}"):
                try:
                    out = fn()
                except GitError as e:
                    self.fail(e)
                    out = None
            if out is not None:
                self.last_output = out
                self.note(f"{name}: done")
            self.after_mutation()
            return
        self.worker.submit(name, fn)
        self.note(f"{name}...")

    def on_done(self, done) -> None:
        if done.name == "GitHub sign-in":
            self._finish_login(done)
            return
        if done.error is not None:
            if isinstance(done.error, GitError):
                self.fail(done.error)
            else:
                self.last_output = repr(done.error)
                self.note(f"{done.name} failed: {done.error}", err=True)
        else:
            out = str(done.result or "")
            self.last_output = out
            tail = out.strip().splitlines()[-1] if out.strip() else "done"
            self.note(f"{done.name}: {tail}")
        self.mark_stale()
        self.refresh()
        self.note(self.msg, self.msg_err)

    def act_fetch(self) -> None:
        self._network("fetch", self.repo.fetch)

    def act_pull(self) -> None:
        if not self.state.branch.upstream:
            self.note("No upstream branch to pull from (push first to publish it)", err=True)
            return
        self._network("pull", self.repo.pull)

    def _publish_target(self) -> str | None:
        """Pick a remote when the branch has no upstream yet. Returns None if cancelled/none."""
        b = self.state.branch
        if b.upstream:
            return ""
        if b.detached:
            self.note("Detached HEAD: create a branch first", err=True)
            return None
        remotes = self.repo.remotes()
        if not remotes:
            # VS Code behaviour: with no remote, "Publish Branch" becomes "Publish to GitHub".
            if self.auth.signed_in:
                if popups.confirm(self.scr, "Publish", f"This repository has no remote yet. "
                                  f"Publish it to GitHub as @{self.auth.login}?"):
                    self._publish()
                else:
                    self.note("Cancelled. To use another host: git remote add origin <url>")
            elif popups.confirm(self.scr, "Publish", "This repository has no remote yet. "
                                "Sign in to GitHub to publish it there?"):
                self.act_github()
            else:
                self.note("Cancelled. To use another host: git remote add origin <url>")
            return None
        if len(remotes) == 1:
            return remotes[0]
        if "origin" in remotes:
            return "origin"
        i = popups.pick(self.scr, "Publish branch to which remote?", remotes)
        return remotes[i] if i is not None else None

    def act_push(self) -> None:
        target = self._publish_target()
        if target is None:
            return
        if target:
            self._network("publish", lambda: self.repo.push(set_upstream=target))
        else:
            self._network("push", self.repo.push)

    def act_sync(self) -> None:
        target = self._publish_target()
        if target is None:
            return
        self._network("sync", lambda: self.repo.sync(set_upstream=target or None))

    @contextlib.contextmanager
    def suspended(self, banner: str, wait: bool = True):
        """Leave curses, run something on the real terminal, come back and redraw."""
        curses.endwin()
        print(f"\n== CurseGit: {banner} ==\n", flush=True)
        try:
            yield
        finally:
            if wait:
                try:
                    input("\n-- press Enter to return to CurseGit --")
                except EOFError:
                    pass
            self.scr.clear()
            self.scr.refresh()
            self.need_draw = True

    # -- stash --------------------------------------------------------------------

    def act_stash_push(self) -> None:
        self._stash_push(False)

    def act_stash_push_all(self) -> None:
        self._stash_push(True)

    def _stash_push(self, include_untracked: bool) -> None:
        if self.state.staged_count == 0 and self.state.changed_count == 0:
            self.note("Nothing to stash")
            return
        msg = popups.prompt(self.scr, "Stash message (optional)")
        if msg is None:
            self.note("Cancelled")
            return
        out = self.repo.stash_push(msg, include_untracked)
        self.last_output = out
        self.after_mutation("Stashed changes" + (" (including untracked)" if include_untracked else ""))

    def _sel_stash(self) -> Stash | None:
        row = self.list.selected
        if row is not None and isinstance(row.data, Stash):
            return row.data
        self.note("No stash selected", err=True)
        return None

    def act_stash_apply(self) -> None:
        s = self._sel_stash()
        if s is None:
            return
        self.last_output = self.repo.stash_apply(s.index)
        self.after_mutation(f"Applied stash@{{{s.index}}}")

    def act_stash_pop(self) -> None:
        s = self._sel_stash()
        if s is None:
            return
        self.last_output = self.repo.stash_pop(s.index)
        self.after_mutation(f"Popped stash@{{{s.index}}}")

    def act_stash_drop(self) -> None:
        s = self._sel_stash()
        if s is None:
            return
        if not popups.confirm(self.scr, "Drop stash", f"Drop stash@{{{s.index}}} \"{s.message}\"?", danger=True):
            self.note("Cancelled")
            return
        self.last_output = self.repo.stash_drop(s.index)
        self.after_mutation(f"Dropped stash@{{{s.index}}}")

    # -- branches -------------------------------------------------------------------

    def _sel_branch(self) -> Branch | None:
        row = self.list.selected
        if row is not None and isinstance(row.data, Branch):
            return row.data
        self.note("No branch selected", err=True)
        return None

    def act_switch(self) -> None:
        b = self._sel_branch()
        if b is None:
            return
        if b.is_current:
            self.note(f"Already on {b.name}")
            return
        name = b.name.split("/", 1)[1] if b.is_remote and "/" in b.name else b.name
        self.last_output = self.repo.switch(name)
        self.after_mutation(f"Switched to {name}")

    def act_new_branch(self) -> None:
        name = popups.prompt(self.scr, "New branch name")
        if not name or not name.strip():
            self.note("Cancelled")
            return
        self.last_output = self.repo.create_branch(name.strip())
        self.after_mutation(f"Created and switched to {name.strip()}")

    def act_delete_branch(self) -> None:
        b = self._sel_branch()
        if b is None:
            return
        if b.is_remote:
            self.note("Remote branches: git push <remote> --delete <branch>", err=True)
            return
        if b.is_current:
            self.note("Cannot delete the current branch; switch first", err=True)
            return
        if not popups.confirm(self.scr, "Delete branch", f"Delete local branch {b.name}?", danger=True):
            self.note("Cancelled")
            return
        try:
            self.repo.delete_branch(b.name)
        except GitError as e:
            if "not fully merged" not in e.stderr:
                raise
            if not popups.confirm(self.scr, "Force delete",
                                  f"{b.name} is not fully merged. Force delete and lose those commits?", danger=True):
                self.note("Cancelled")
                return
            self.repo.delete_branch(b.name, force=True)
        self.after_mutation(f"Deleted {b.name}")

    def act_merge(self) -> None:
        b = self._sel_branch()
        if b is None:
            return
        if b.is_current:
            self.note("That is the current branch")
            return
        cur = self.state.branch.head
        if not popups.confirm(self.scr, "Merge", f"Merge {b.name} into {cur}?"):
            self.note("Cancelled")
            return
        try:
            self.last_output = self.repo.merge(b.name)
        except GitError as e:
            self.fail(e)
            self.after_mutation()
            self.note("Merge stopped: resolve conflicts on the Status tab", err=True)
            self.switch_tab(0)
            return
        self.after_mutation(f"Merged {b.name} into {cur}")

    # -- GitHub -----------------------------------------------------------------------

    def _apply_auth(self) -> None:
        self.repo.extra_flags = github.helper_config_args() if self.auth.signed_in else []

    def act_github(self) -> None:
        if self.pending_login is not None:
            self.note("A sign-in is already in progress (Esc cancels it)")
            return
        if self.auth.signed_in:
            options = [f"Publish this repository to GitHub as @{self.auth.login}",
                       "Open the remote in your browser",
                       f"Sign out (@{self.auth.login})"]
            actions = [self._publish, self.act_open_remote, self._sign_out]
        else:
            options = ["Sign in with your browser (OAuth device flow)",
                       "Use the GitHub CLI's login (gh auth token)",
                       "Paste a personal access token",
                       "Publish this repository to GitHub (sign in first)"]
            actions = [self._login_browser, self._login_gh, self._login_pat, self._publish]
        i = popups.pick(self.scr, "GitHub", options)
        if i is None:
            return
        actions[i]()

    def _login_browser(self) -> None:
        if not self.auth.client_id:
            popups.show_text(self.scr, "One-time setup for browser sign-in", github.CLIENT_ID_HELP, colorize=False)
            cid = popups.prompt(self.scr, "OAuth App client ID")
            if not cid or not cid.strip():
                self.note("Sign-in cancelled")
                return
            self.auth.client_id = cid.strip()
            github.save_auth(self.auth)
        try:
            code = github.request_device_code(self.auth.client_id)
        except github.GitHubError as e:
            self.note(f"GitHub: {e}", err=True)
            return
        webbrowser.open(code.verification_uri)
        self.login_cancel = threading.Event()
        cancel = self.login_cancel
        client_id = self.auth.client_id

        def job():
            token = github.wait_for_device_token(client_id, code, cancelled=cancel.is_set)
            return token, github.whoami(token), "oauth"

        self.pending_login = code
        self.worker.submit("GitHub sign-in", job)

    def _login_gh(self) -> None:
        token = github.gh_cli_token()
        if not token:
            self.note("GitHub CLI is not installed or not signed in (run: gh auth login)", err=True)
            return
        self.worker.submit("GitHub sign-in", lambda: (token, github.whoami(token), "gh"))
        self.note("Checking the GitHub CLI token...")

    def _login_pat(self) -> None:
        token = popups.prompt(self.scr, "Personal access token (needs the repo scope)", secret=True)
        if not token or not token.strip():
            self.note("Sign-in cancelled")
            return
        tok = token.strip()
        self.worker.submit("GitHub sign-in", lambda: (tok, github.whoami(tok), "pat"))
        self.note("Checking the token...")

    def _finish_login(self, done) -> None:
        self.pending_login = None
        if done.error is not None:
            if "cancelled" in str(done.error):
                return
            self.last_output = str(done.error)
            self.note(f"GitHub sign-in failed: {done.error}", err=True)
            return
        token, login, source = done.result
        self.auth.token, self.auth.login, self.auth.source = token, login, source
        github.save_auth(self.auth)
        self._apply_auth()
        github.store_git_credential(self.repo.run_full, login, token)
        self.note(f"Signed in to GitHub as @{login}")

    def _sign_out(self) -> None:
        if not popups.confirm(self.scr, "Sign out", f"Sign out of GitHub (@{self.auth.login}) and forget the token?"):
            self.note("Cancelled")
            return
        github.erase_git_credential(self.repo.run_full, self.auth.login)
        self.auth = github.clear_token(self.auth)
        self._apply_auth()
        self.note("Signed out of GitHub")

    def _publish(self) -> None:
        if not self.auth.signed_in:
            self.note("Sign in to GitHub first (@)", err=True)
            return
        if self.repo.remotes():
            self.note("This repository already has a remote; use P to push", err=True)
            return
        if not self.repo.has_commits():
            self.note("Make a commit first, then publish", err=True)
            return
        name = popups.prompt(self.scr, "Repository name on GitHub", self.repo.name)
        if not name or not name.strip():
            self.note("Cancelled")
            return
        vis = popups.pick(self.scr, "Visibility", ["Private", "Public"])
        if vis is None:
            self.note("Cancelled")
            return
        repo_name, private, token = name.strip(), vis == 0, self.auth.token

        def job():
            created = github.create_repo(token, repo_name, private)
            self.repo.run("remote", "add", "origin", created["clone_url"])
            self.repo.push(set_upstream="origin")
            return f"Published to {created['html_url']}"

        self.worker.submit("publish", job)
        self.note(f"Creating {repo_name} on GitHub and pushing...")

    # -- log ------------------------------------------------------------------------

    def act_branch_at(self) -> None:
        row = self.list.selected
        if row is None or not isinstance(row.data, Commit):
            self.note("No commit selected", err=True)
            return
        name = popups.prompt(self.scr, f"New branch at {row.data.short}")
        if not name or not name.strip():
            self.note("Cancelled")
            return
        self.last_output = self.repo.create_branch(name.strip(), start=row.data.sha)
        self.after_mutation(f"Created {name.strip()} at {row.data.short}")
