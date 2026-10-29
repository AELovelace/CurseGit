"""End-to-end smoke test: drive the App with a fake curses screen and scripted keys.

This does not test what the screen looks like pixel by pixel; it proves that every tab, popup and
action path runs without raising, against a real temporary git repository.
"""
import contextlib
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import curses

from cursegit.git import GitRepo
from cursegit.ui import app as app_mod

HAVE_GIT = shutil.which("git") is not None
WAIT = object()  # marker: block until the worker is idle, then report a timeout


class FakeWin:
    """Enough of a curses window for App/popups: a character grid and a scripted key queue."""

    def __init__(self, harness, h=30, w=120):
        self.h = harness
        self.rows, self.cols = h, w
        self.grid = [[" "] * w for _ in range(h)]

    # geometry / drawing
    def getmaxyx(self):
        return self.rows, self.cols

    def addstr(self, y, x, s, attr=0):
        if y >= self.rows or x >= self.cols or y < 0 or x < 0:
            raise curses.error("out of bounds")
        for i, ch in enumerate(s):
            if x + i < self.cols:
                self.grid[y][x + i] = ch
        if x + len(s) > self.cols:
            raise curses.error("clipped")

    def text(self):
        return "\n".join("".join(r).rstrip() for r in self.grid)

    def erase(self):
        self.grid = [[" "] * self.cols for _ in range(self.rows)]

    clear = erase

    def refresh(self):
        self.h.frames.append(self.text())

    def box(self):
        pass

    def attrset(self, a):
        pass

    def move(self, y, x):
        pass

    def keypad(self, flag):
        pass

    def timeout(self, ms):
        pass

    def nodelay(self, flag):
        pass

    # input
    def getch(self):
        return self.h.next_key()


class Mouse:
    """Scripted mouse event: delivered as KEY_MOUSE, then returned by the patched getmouse()."""

    def __init__(self, x, y, bstate):
        self.event = (0, x, y, 0, bstate)


class Harness:
    def __init__(self, keys):
        self.keys = list(keys)
        self.frames = []
        self.app = None
        self.pending_mouse = None

    def next_key(self):
        if not self.keys:
            raise AssertionError("script exhausted before the app quit")
        k = self.keys[0]
        if k is WAIT:
            if self.app.worker.busy:
                time.sleep(0.02)
                return -1
            self.keys.pop(0)
            return -1
        self.keys.pop(0)
        if isinstance(k, Mouse):
            self.pending_mouse = k.event
            return curses.KEY_MOUSE
        if isinstance(k, str):
            return ord(k)
        return k

    def getmouse(self):
        if self.pending_mouse is None:
            raise curses.error("no mouse event")
        ev, self.pending_mouse = self.pending_mouse, None
        return ev


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _write(root, rel, text):
    with open(os.path.join(root, rel), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


@unittest.skipUnless(HAVE_GIT, "git not installed")
class AppSmoke(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cursegit-smoke-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._env = dict(os.environ)
        os.environ.update({
            "CURSEGIT_CONFIG_DIR": os.path.join(self.tmp, "config"),
            "GIT_CONFIG_GLOBAL": os.path.join(self.tmp, "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "T", "GIT_COMMITTER_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_EMAIL": "t@example.com",
        })
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        _git(self.work, "init", "-q", "-b", "main")
        _write(self.work, "a.txt", "one\n")
        _git(self.work, "add", "a.txt")
        _git(self.work, "commit", "-q", "-m", "first")
        _write(self.work, "a.txt", "one\ntwo\n")      # modified
        _write(self.work, "b.txt", "new\n")           # untracked
        _git(self.work, "stash", "push", "-q", "-m", "stashed thing")
        _write(self.work, "a.txt", "one\nthree\n")
        _write(self.work, "c.txt", "c\n")
        self.repo = GitRepo(self.work)

    def run_app(self, keys, h=30, w=120):
        harness = Harness(keys)
        scr = FakeWin(harness, h, w)
        application = app_mod.App(self.repo, refresh_interval=0, tty_mode=False)
        harness.app = application
        patches = [
            mock.patch.object(curses, "newwin", lambda *a, **k: FakeWin(harness, a[0], a[1])),
            mock.patch.object(curses, "curs_set", lambda v: None),
            mock.patch.object(curses, "start_color", lambda: None),
            mock.patch.object(curses, "has_colors", lambda: False),
            mock.patch.object(curses, "use_default_colors", lambda: None),
            mock.patch.object(curses, "endwin", lambda: None),
            mock.patch.object(curses, "resize_term", lambda *a: None, create=True),
            mock.patch.object(curses, "mousemask", lambda m: (m, 0)),
            mock.patch.object(curses, "mouseinterval", lambda ms: 0),
            mock.patch.object(curses, "getmouse", harness.getmouse),
            mock.patch("builtins.input", lambda *a: ""),
            mock.patch("webbrowser.open", lambda url: True),
        ]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            application.run(scr)
        return application, harness

    def test_tabs_navigation_and_popups(self):
        keys = [
            "j", "k", "\t", "j", "\t",       # move, focus detail, scroll, focus back
            "?", "q",                        # help popup and close
            "\n", "q",                       # detail full-screen and close
            "2", "j", "3", "4", "1",         # every tab
            curses.KEY_RESIZE,
            "!", "q",                        # last output popup
            "q",
        ]
        application, harness = self.run_app(keys)
        self.assertFalse(application.running)
        joined = "\n".join(harness.frames)
        for word in ("CurseGit", "CHANGES", "UNTRACKED", "LOCAL", "stashed thing", "first"):
            self.assertIn(word, joined)

    def test_stage_commit_flow(self):
        keys = [
            "s",                             # stage a.txt (cursor starts on first file)
            "c", "d", "o", "n", "e", "\x13",  # commit: type "done", Ctrl-S
            "4",                             # log tab shows the new commit
            "q",
        ]
        application, harness = self.run_app(keys)
        log = self.repo.log()
        self.assertEqual(log[0].subject, "done")
        self.assertIn("Committed: done", "\n".join(harness.frames))

    def test_stage_all_unstage_discard_cancel(self):
        keys = ["S", "U", "d", "n", "D", "n", " ", " ", "q"]  # discard prompts answered "no"
        application, _ = self.run_app(keys)
        st = self.repo.status()
        self.assertTrue(os.path.exists(os.path.join(self.work, "c.txt")))
        self.assertGreaterEqual(len(st.files), 2)

    def test_stage_all_from_other_tab(self):
        application, harness = self.run_app(["4", "S", "U", "q"])
        self.assertEqual(application.tab, 0)
        joined = chr(10).join(harness.frames)
        self.assertIn("Staged all changes (3 file(s))", joined)
        self.assertIn("Unstaged all changes", joined)
        self.assertEqual(self.repo.status().staged_count, 0)

    def test_network_without_remote_reports_error(self):
        keys = ["f", WAIT, "p", "P", "n", "q"]   # P with no remote offers Publish to GitHub; answer no
        application, harness = self.run_app(keys)
        joined = "\n".join(harness.frames)
        self.assertIn("fetch", joined.lower())
        self.assertIn("No upstream branch to pull from", joined)
        self.assertIn("Sign in to GitHub to publish it", joined)
        self.assertIn("git remote add origin", application.msg)

    def test_branch_and_stash_actions(self):
        keys = [
            "2", "n", "f", "e", "a", "t", "\n",   # new branch "feat"
            "3", "a",                             # apply the stash (non-conflicting file b.txt)
            "1", "z", "\n",                       # stash tracked changes with empty message
            "3", "D", "y",                        # drop the newest stash
            "q",
        ]
        application, _ = self.run_app(keys)
        st = self.repo.status()
        self.assertEqual(st.branch.head, "feat")
        self.assertEqual(len(st.stashes), 1)

    def test_arrow_keys_and_bracket_tabs(self):
        keys = [
            curses.KEY_DOWN, curses.KEY_DOWN, curses.KEY_UP,     # move in the list
            curses.KEY_RIGHT, curses.KEY_DOWN, curses.KEY_NPAGE,  # focus diff, scroll it
            curses.KEY_LEFT,                                      # back to the list
            "]", "]", "[",                                        # Status -> Branches -> Stash -> Branches
            curses.KEY_HOME, curses.KEY_END,
            "q",
        ]
        application, harness = self.run_app(keys)
        self.assertEqual(application.tab, 1)
        self.assertEqual(application.focus, "list")
        self.assertIn("LOCAL", harness.frames[-1])

    def test_right_arrow_opens_detail_when_narrow(self):
        application, harness = self.run_app([curses.KEY_RIGHT, "q", "q"], h=12, w=60)
        self.assertFalse(application.running)
        self.assertIn("diff --git", "\n".join(harness.frames))

    def test_mouse(self):
        click = curses.BUTTON1_CLICKED
        dbl = curses.BUTTON1_DOUBLE_CLICKED
        keys = [
            Mouse(3, 2 + 3, click),            # click the 4th body line (a file row under CHANGES)
            Mouse(80, 5, click),               # click the detail pane -> focus it
            Mouse(80, 5, curses.BUTTON5_PRESSED),   # wheel down over the diff
            Mouse(3, 4, curses.BUTTON4_PRESSED),    # wheel up over the list
            Mouse(14, 1, click),               # click the "2 Branches" tab
            Mouse(3, 3, dbl),                  # double-click the first branch row -> checkout (already current)
            Mouse(3, 1, click),                # back to Status tab
            "M", "M",                          # toggle mouse off and on
            "q",
        ]
        application, harness = self.run_app(keys)
        self.assertEqual(application.tab, 0)
        self.assertTrue(application.mouse_on)
        joined = "\n".join(harness.frames)
        self.assertIn("Already on main", joined)
        self.assertIn("Mouse on", joined)

    def test_github_menu_cancel(self):
        application, harness = self.run_app(["@", "\x1b", "q"])
        self.assertFalse(application.auth.signed_in)

    def test_github_pat_login_publish_and_sign_out(self):
        from cursegit import github
        bare = os.path.join(self.tmp, "fake-github.git")
        _git(self.tmp, "init", "-q", "--bare", "-b", "main", bare)
        created = {"clone_url": bare.replace("\\", "/"), "html_url": "https://github.com/edith/work"}
        keys = [
            "@", "3", *list("ghp_secret"), "\n", WAIT,     # paste a token -> whoami -> signed in
            "s", "c", *list("pub"), "\x13",                # commit something to publish
            "@", "1", "\n", "1", WAIT,                      # publish: default name, Private
            "@", "3", "y",                                  # sign out
            "q",
        ]
        with mock.patch.object(github, "whoami", lambda tok: "edith" if tok == "ghp_secret" else None), \
                mock.patch.object(github, "create_repo", lambda tok, name, private, description="": created):
            application, harness = self.run_app(keys)
        joined = "\n".join(harness.frames)
        self.assertIn("Signed in to GitHub as @edith", joined)
        self.assertIn("@edith", joined)
        self.assertIn("Published to https://github.com/edith/work", joined)
        self.assertIn("Signed out of GitHub", joined)
        self.assertFalse(application.auth.signed_in)
        self.assertEqual(self.repo.remotes(), ["origin"])
        self.assertEqual(self.repo.status().branch.upstream, "origin/main")
        self.assertEqual(self.repo.extra_flags, [])

    def test_push_without_remote_offers_publish(self):
        from cursegit import github
        bare = os.path.join(self.tmp, "fake-github.git")
        _git(self.tmp, "init", "-q", "--bare", "-b", "main", bare)
        created = {"clone_url": bare.replace("\\", "/"), "html_url": "https://github.com/edith/work"}
        github.save_auth(github.Auth(token="t", login="edith", source="pat"))
        keys = ["P", "y", "\n", "2", WAIT, "q"]   # P -> "publish to GitHub?" yes -> default name -> Public
        with mock.patch.object(github, "create_repo", lambda tok, name, private, description="": created):
            application, harness = self.run_app(keys)
        self.assertIn("Published to https://github.com/edith/work", application.msg)
        self.assertIn("Publish it to GitHub as @edith?", "\n".join(harness.frames))
        self.assertEqual(self.repo.remotes(), ["origin"])
        self.assertTrue(application.has_remote)

    def test_push_without_remote_not_signed_in_offers_login(self):
        application, harness = self.run_app(["P", "n", "q"])
        self.assertIn("P publish to GitHub", "\n".join(harness.frames))
        self.assertIn("Cancelled", application.msg)

    def test_github_device_flow_cancel(self):
        from cursegit import github
        code = github.DeviceCode("dc", "ABCD-1234", "https://github.com/login/device", 900, 1)
        github.save_auth(github.Auth(client_id="cid"))
        with mock.patch.object(github, "request_device_code", lambda cid: code), \
                mock.patch.object(github, "poll_device_token", mock.Mock(side_effect=github.Pending())):
            application, harness = self.run_app(["@", "1", "\x1b", WAIT, "q"])
        self.assertIn("ABCD-1234", "\n".join(harness.frames))
        self.assertIsNone(application.pending_login)
        self.assertFalse(application.auth.signed_in)
        self.assertIn("Sign-in cancelled", "\n".join(harness.frames))

    def test_narrow_terminal(self):
        application, harness = self.run_app(["\t", "j", "2", "1", "q"], h=12, w=60)
        self.assertEqual(application.focus, "list")
        self.assertIn("CurseGit", harness.frames[-1])


if __name__ == "__main__":
    unittest.main()
