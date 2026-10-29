"""Welcome screen (no repository here): init and clone flows, driven through the fake screen."""
import contextlib
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import curses

from cursegit.ui import welcome as welcome_mod
from tests.test_app_smoke import FakeWin, Harness

HAVE_GIT = shutil.which("git") is not None


@unittest.skipUnless(HAVE_GIT, "git not installed")
class WelcomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cursegit-welcome-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._env = dict(os.environ)
        os.environ.update({"GIT_CONFIG_GLOBAL": os.path.join(self.tmp, "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"})
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))

    def run_welcome(self, keys, path):
        harness = Harness(keys)
        scr = FakeWin(harness, 24, 100)
        patches = [
            mock.patch.object(curses, "newwin", lambda *a, **k: FakeWin(harness, a[0], a[1])),
            mock.patch.object(curses, "curs_set", lambda v: None),
            mock.patch.object(curses, "start_color", lambda: None),
            mock.patch.object(curses, "has_colors", lambda: False),
            mock.patch.object(curses, "use_default_colors", lambda: None),
            mock.patch.object(curses, "endwin", lambda: None),
            mock.patch("builtins.input", lambda *a: ""),
        ]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            result = welcome_mod.welcome(scr, path)
        return result, harness

    def test_quit(self):
        repo, harness = self.run_welcome(["q"], self.tmp)
        self.assertIsNone(repo)
        self.assertIn("No git repository here", "\n".join(harness.frames))
        repo, _ = self.run_welcome(["\x1b"], self.tmp)
        self.assertIsNone(repo)

    def test_init_with_arrows_and_enter(self):
        target = os.path.join(self.tmp, "fresh")
        os.makedirs(target)
        repo, _ = self.run_welcome([curses.KEY_DOWN, curses.KEY_UP, "\n"], target)
        self.assertIsNotNone(repo)
        self.assertTrue(os.path.isdir(os.path.join(target, ".git")))
        self.assertTrue(repo.status().branch.unborn)

    def test_clone_flow(self):
        src = os.path.join(self.tmp, "source.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", src], check=True)
        work = os.path.join(self.tmp, "seed")
        subprocess.run(["git", "clone", "-q", src, work], check=True, capture_output=True)
        with open(os.path.join(work, "a.txt"), "w") as fh:
            fh.write("a\n")
        env = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@x")
        subprocess.run(["git", "add", "a.txt"], cwd=work, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=work, check=True, env=env)
        subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=work, check=True, capture_output=True)

        parent = os.path.join(self.tmp, "parent")
        os.makedirs(parent)
        url = src.replace("\\", "/")
        keys = ["c", *list(url), "\n", "\n"]   # clone, type URL, accept default destination
        repo, _ = self.run_welcome(keys, parent)
        self.assertIsNotNone(repo)
        self.assertEqual(os.path.basename(repo.root), "source")
        self.assertTrue(os.path.exists(os.path.join(repo.root, "a.txt")))

    def test_clone_cancel_returns_to_menu(self):
        repo, harness = self.run_welcome(["c", "\x1b", "q"], self.tmp)
        self.assertIsNone(repo)

    def test_default_dest(self):
        self.assertEqual(welcome_mod.default_clone_dest("git@github.com:o/repo.git", "p"), os.path.join("p", "repo"))
        self.assertEqual(welcome_mod.default_clone_dest("https://x/y/thing", "p"), os.path.join("p", "thing"))


if __name__ == "__main__":
    unittest.main()
