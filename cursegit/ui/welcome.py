"""The screen shown when CurseGit is started outside a git repository.

Mirrors VS Code's empty Source Control view: Initialize Repository, Clone Repository, or leave.
"""
from __future__ import annotations

import curses
import os

from ..git import GitError, GitRepo
from . import popups
from .draw import display_width, key_name, put, read_key
from .panes import ListPane, Row
from .theme import A, init_theme

OPTIONS = [
    ("init", "i", "Initialize Repository here"),
    ("clone", "c", "Clone Repository"),
    ("quit", "q", "Quit"),
]


def default_clone_dest(url: str, parent: str) -> str:
    name = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return os.path.join(parent, name or "repo")


def welcome(scr, path: str) -> GitRepo | None:
    """Return a repository to open, or None if the user chose to quit."""
    init_theme()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    scr.keypad(True)
    scr.timeout(-1)
    target = os.path.abspath(path)
    menu = ListPane()
    menu.set_rows([Row(f"[{key}]  {label}", data=action, key=action) for action, key, label in OPTIONS])
    message = ""
    message_err = False

    while True:
        _draw(scr, target, menu, message, message_err)
        name = key_name(read_key(scr))
        if name == "resize":
            scr.clear()
            continue
        action = None
        if name in ("down", "j"):
            menu.move(1)
        elif name in ("up", "k"):
            menu.move(-1)
        elif name == "enter":
            action = menu.selected.data if menu.selected else None
        elif name == "esc":
            action = "quit"
        else:
            action = next((a for a, k, _ in OPTIONS if k == name), None)

        if action == "quit":
            return None
        if action == "init":
            try:
                return GitRepo.init(target)
            except GitError as e:
                message, message_err = e.message, True
        elif action == "clone":
            repo, message = _clone_flow(scr, target)
            message_err = repo is None and bool(message)
            if repo is not None:
                return repo


def _clone_flow(scr, parent: str) -> tuple[GitRepo | None, str]:
    url = popups.prompt(scr, "Clone: repository URL")
    if not url or not url.strip():
        return None, ""
    url = url.strip()
    dest = popups.prompt(scr, "Clone into", default_clone_dest(url, parent))
    if dest is None:
        return None, ""
    dest = os.path.abspath(dest.strip() or default_clone_dest(url, parent))
    if os.path.exists(dest) and os.listdir(dest):
        return None, f"{dest} already exists and is not empty"

    curses.endwin()
    print(f"\n== CurseGit: git clone {url} {dest} ==\n", flush=True)
    rc = GitRepo.clone(url, dest)
    if rc != 0:
        try:
            input("\n-- clone failed; press Enter to return --")
        except EOFError:
            pass
    scr.clear()
    scr.refresh()
    if rc != 0:
        return None, f"git clone exited with {rc}"
    repo = GitRepo.discover(dest)
    if repo is None:
        return None, f"clone finished but {dest} is not a repository"
    return repo, ""


def _draw(scr, target: str, menu: ListPane, message: str, message_err: bool) -> None:
    scr.erase()
    H, W = scr.getmaxyx()
    put(scr, 0, 0, "", A["header"])
    put(scr, 0, 0, " CurseGit", A["header"])
    lines = [
        "No git repository here:",
        target,
        "",
        "Pick one (arrows + Enter, or the letter):",
    ]
    width = min(W - 4, max(60, display_width(target) + 6))
    x0 = max(0, (W - width) // 2)
    y = max(1, (H - (len(lines) + len(OPTIONS) + 4)) // 2)
    for i, ln in enumerate(lines):
        attr = A["title"] if i == 0 else (A["dim"] if i == 3 else 0)
        put(scr, y + i, x0, ln, attr, width, ellipsis=True)
    menu.draw(scr, y + len(lines), x0, len(OPTIONS), width, True)
    if message:
        put(scr, y + len(lines) + len(OPTIONS) + 1, x0, message, A["error"] if message_err else A["ok"], width, ellipsis=True)
    put(scr, H - 1, 1, "cursegit init [path]  /  cursegit clone <url> [dest] also work from the command line", A["dim"])
    scr.refresh()
