# CurseGit — plan

**Status (2026-10-29):** M0–M4 built and tested (`python -m unittest discover -s tests`), plus from M5:
arrow/mouse navigation, pink theme, the init/clone welcome screen, and GitHub sign-in (OAuth device flow,
gh CLI token, or PAT) with "Publish to GitHub". Remaining: hunk staging, config file, auto-fetch, PR list.

A terminal Git client in Python + curses that mirrors what the VS Code Source Control
(Git) extension does: see changes, stage/unstage, commit, pull, push, sync, switch
branches, stash, and view diffs. Think lazygit/tig, but small, Python, and ours.

## 0. Ground rules

- **Shell out to `git`.** VS Code does exactly this. No GitPython, no libgit2. Every
  operation is a `subprocess.run(["git", ...])` with machine-readable flags
  (`--porcelain=v2`, `-z`, `--format=`). Zero third-party deps on Linux/macOS.
- **Windows needs `windows-curses`.** CPython on Windows has no `curses` module by
  default. Dependency with a platform marker:
  `windows-curses; sys_platform == "win32"`. (This machine already imports curses fine.)
- **Never let git prompt.** Set `GIT_TERMINAL_PROMPT=0`, `GIT_PAGER=cat`,
  `GIT_EDITOR=true` on every call. Auth goes through the user's credential helper
  (Git Credential Manager on Windows) exactly like VS Code. If a command genuinely
  needs the terminal (SSH passphrase), drop out of curses with `endwin()`, run it
  attached to the TTY, then redraw.
- **Never block the UI.** Network ops (fetch/pull/push) run in a worker thread; the
  main loop uses `getch()` with a timeout and drains a result queue. curses is not
  thread-safe, so only the main thread ever draws.
- **`git` is the source of truth.** No cached model that can drift; after every
  mutation re-run `status`. Cheap, and it's what keeps the UI honest.

## 1. Feature parity map (VS Code SCM -> CurseGit)

| VS Code SCM                         | CurseGit                                   | Milestone |
|-------------------------------------|--------------------------------------------|-----------|
| Changes / Staged / Merge Changes    | Status panel, three sections               | M1        |
| Stage / unstage file (+/-)          | `s` / `u` on a file, `S`/`U` for all       | M1        |
| Discard changes                     | `d` with confirm (restore / clean)         | M1        |
| Open diff                           | `Enter` -> diff pane (`--no-color`)        | M1        |
| Commit message box, commit          | `c` -> multiline input, commit via `-F`    | M2        |
| Amend                               | `C` (commit --amend)                       | M2        |
| Pull / Push / Sync / Fetch          | `p` pull, `P` push, `y` sync, `f` fetch    | M3        |
| Publish branch                      | `P` on a branch with no upstream -> `-u`   | M3        |
| Status-bar branch, ahead/behind     | Header line: repo, branch, up n / down m   | M1/M3     |
| Branch picker (checkout / create)   | Branches panel: `Enter`, `n` new, `D` del  | M4        |
| Stash / pop / apply                 | Stash panel: `z` stash, `Z` pop, `a` apply | M4        |
| Timeline / log                      | Log panel, `Enter` shows commit diff       | M4        |
| Merge conflict markers              | Conflicts section, `Enter` opens $EDITOR   | M5        |
| Clone, Init                         | `cursegit clone <url>`, `cursegit init`    | M5        |
| "Open on GitHub", create PR         | `o` opens remote URL in browser; PR via `gh` if present (optional) | M5 |
| Auto-fetch                          | Background fetch every N min (off default) | M5        |

## 2. Layout

```
+ CurseGit  ~/proj (main  up2 down0)  3 changed -----------------------------------+
| [1] Status   [2] Branches   [3] Stash   [4] Log                                   |
+-----------------------------+-----------------------------------------------------+
| STAGED (1)                  | diff --git a/src/app.py b/src/app.py                |
|   M  src/app.py             | @@ -10,6 +10,9 @@                                   |
| CHANGES (2)                 |  import os                                          |
| > M  README.md              | +import sys                                         |
|   ?? notes.txt              | +                                                   |
| CONFLICTS (0)               | -def main():                                        |
|                             | +def main(argv):                                    |
+-----------------------------+-----------------------------------------------------+
| Pushed main -> origin/main                            s stage  c commit  ? help    |
+-----------------------------------------------------------------------------------+
```

- Left pane: the active tab's list. Right pane: context (diff / commit detail / branch info).
- Bottom line: last result or error, plus key hints. Errors from git stderr shown verbatim.
- Below ~90 columns the right pane collapses and `Enter` opens the diff full-screen.
- Modal popups for commit message, confirms, new-branch name, and `?` help.

## 3. Architecture

```
cursegit/
  __main__.py      # `python -m cursegit [path]`; arg parsing; curses.wrapper(App.run)
  git.py           # GitRepo: thin wrappers around subprocess. Pure functions, no curses.
  model.py         # dataclasses: FileChange, Branch, Stash, Commit, RepoState
  parse.py         # porcelain v2 / -z / log --format parsers (unit-tested, no I/O)
  worker.py        # ThreadPool + result Queue; Job(name, fn) -> Done(name, result|error)
  ui/
    app.py         # main loop, key dispatch, tab switching, resize handling
    panes.py       # ListPane (scroll/select), TextPane (diff viewer, search)
    popups.py      # input box, multiline editor, confirm, help
    theme.py       # color pairs, with a no-color fallback
    keys.py        # keymap table (single place, drives the help popup too)
```

**Data flow:** key -> `App.dispatch` -> either an immediate `GitRepo` call (fast, local)
or `worker.submit(job)` (network). Every mutation ends with `refresh()` =
`git status --porcelain=v2 --branch -z` + `git stash list` re-parsed into `RepoState`.
Main loop: `getch()` (timeout 100 ms) -> handle key -> drain queue -> redraw if dirty.

**Git commands we lean on** (all parse-stable):

- `status --porcelain=v2 --branch -z` (branch, upstream, ahead/behind, every file state incl. conflicts)
- `diff -- <file>` / `diff --cached -- <file>` / `show <sha>`, all with `--no-color`
- `add -- <file>`, `restore --staged -- <file>`, `restore -- <file>`, `clean -f -- <file>`
- `commit -F <tmpfile>` (and `--amend`), `log --format=%H%x00%h%x00%an%x00%ar%x00%s -z`
- `for-each-ref --format=... refs/heads refs/remotes`, `switch`, `switch -c`, `branch -d`
- `stash push/pop/apply/drop/list --format=...`
- `fetch --prune`, `pull`, `push` (`-u origin HEAD` when no upstream)

## 4. Milestones

- **M0 — skeleton (half a day):** package layout, curses boot/teardown, resize, header,
  tab bar, empty panes, `q` quits. Runs on Windows Terminal and a Linux tty.
- **M1 — status + diff (core value):** porcelain v2 parser with tests, status list with
  sections, stage/unstage/discard, diff pane with scroll. Ship this to yourself and use it.
- **M2 — commit:** multiline message popup (Ctrl-S submits, Esc cancels),
  commit / amend, empty-stage guard, result in status line.
- **M3 — remotes:** worker thread, fetch/pull/push/sync, ahead/behind in header, spinner,
  publish-branch path, surface auth failures clearly, `endwin()` fallback for SSH prompts.
- **M4 — branches, stash, log:** three more tabs reusing ListPane.
- **M5 — polish:** conflicts, clone/init, open-in-browser, optional `gh` integration,
  auto-fetch, config file (`~/.config/cursegit/config.toml`: keymap, theme, auto-fetch).

## 5. Testing

- `parse.py` is pure: fixture strings -> dataclasses. Cover renames, conflicts (`u` lines),
  untracked, ignored, detached HEAD, no upstream, filenames with spaces/unicode.
- Integration: pytest builds throwaway repos in `tmp_path` (`git init`, commit, branch,
  a bare "origin" for push/pull) and drives `GitRepo` directly. No curses needed.
- UI: manual, plus a smoke test that imports `ui.app` and runs one draw cycle in a
  fake screen if we want it later. Don't over-invest here.

## 6. Risks / gotchas

- **Windows curses:** `windows-curses` lacks a few things (no `resizeterm`, limited
  mouse). Rely on `KEY_RESIZE` + `getmaxyx()`; keep the mouse optional.
- **Unicode width:** East-Asian and emoji filenames break column math. Truncate by
  display width (`unicodedata.east_asian_width`), not `len()`.
- **Pagers/editors/hooks:** env overrides above handle pager/editor; commit hooks that
  prompt will fail. Document it, show stderr.
- **Large diffs:** cap at N lines loaded, lazy-load more on scroll.
- **Repo detection:** `git rev-parse --show-toplevel` on start; clear error if not a repo,
  offer `init`.
- **Encoding:** decode git output as UTF-8 with `errors="replace"`; `-z` everywhere so
  odd paths don't get quoted.

## 7. Open decisions (make them when we hit them)

- Mouse support at all? Default off, flag to enable.
- Side-by-side diff? Start unified only.
- Hunk-level staging (`git add -p` equivalent)? Nice M6 item; needs our own hunk parser
  and `git apply --cached`.
- Package to PyPI as `cursegit`, or keep it a script? Package; `pipx install` is the win.

## 8. Immediate next steps

1. `pip install windows-curses` on Windows boxes that lack curses (this one is fine).
2. Scaffold M0: `pyproject.toml`, `cursegit/` package, `curses.wrapper` boot, header + tab bar.
3. Write `parse.py` for `status --porcelain=v2 -z` with tests first, then wire M1.
