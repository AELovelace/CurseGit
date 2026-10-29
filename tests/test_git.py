"""Integration tests: drive GitRepo against throwaway repositories. Requires `git` on PATH."""
import os
import shutil
import subprocess
import tempfile
import unittest

from cursegit.git import GitError, GitRepo
from cursegit.model import Section

HAVE_GIT = shutil.which("git") is not None


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _write(root, rel, text):
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


@unittest.skipUnless(HAVE_GIT, "git not installed")
class RepoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cursegit-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # Isolate from the user's global config and identity.
        self._old_env = dict(os.environ)
        os.environ["GIT_CONFIG_GLOBAL"] = os.path.join(self.tmp, "gitconfig")
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
            os.environ[k] = "Test"
        for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
            os.environ[k] = "test@example.com"
        self.addCleanup(self._restore_env)
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        _git(self.work, "init", "-q", "-b", "main")
        self.repo = GitRepo(self.work)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._old_env)

    def _commit_file(self, rel, text, msg):
        _write(self.work, rel, text)
        self.repo.stage([rel])
        return self.repo.commit(msg)

    # -- basics --------------------------------------------------------------

    def test_discover_from_subdir(self):
        os.makedirs(os.path.join(self.work, "a", "b"))
        repo = GitRepo.discover(os.path.join(self.work, "a", "b"))
        self.assertIsNotNone(repo)
        self.assertEqual(os.path.normcase(repo.root), os.path.normcase(self.work))
        self.assertIsNone(GitRepo.discover(self.tmp))

    def test_unborn_status_stage_unstage(self):
        st = self.repo.status()
        self.assertTrue(st.branch.unborn)
        self.assertEqual(st.files, [])

        _write(self.work, "a.txt", "hello\n")
        st = self.repo.status()
        self.assertEqual([(f.section, f.path) for f in st.files], [(Section.UNTRACKED, "a.txt")])
        diff = self.repo.diff(st.files[0])
        self.assertIn("+hello", diff)

        self.repo.stage(["a.txt"])
        st = self.repo.status()
        self.assertEqual(st.files[0].section, Section.STAGED)
        self.assertEqual(st.files[0].code, "A")
        self.assertIn("+hello", self.repo.diff(st.files[0]))

        self.repo.unstage(["a.txt"])  # no HEAD yet: exercises the rm --cached fallback
        self.assertEqual(self.repo.status().files[0].section, Section.UNTRACKED)

        self.repo.stage_all()
        self.repo.unstage_all()
        self.assertEqual(self.repo.status().files[0].section, Section.UNTRACKED)

    def test_commit_log_amend(self):
        self._commit_file("a.txt", "one\n", "first commit\n\nbody line")
        self.assertTrue(self.repo.has_commits())
        log = self.repo.log()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0].subject, "first commit")
        self.assertEqual(self.repo.last_commit_message(), "first commit\n\nbody line")

        self.repo.commit("renamed subject", amend=True)
        log = self.repo.log()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0].subject, "renamed subject")
        self.assertIn("+one", self.repo.show(log[0].sha))

    def test_modified_both_sections_and_discard(self):
        self._commit_file("a.txt", "one\n", "c1")
        _write(self.work, "a.txt", "one\ntwo\n")
        self.repo.stage(["a.txt"])
        _write(self.work, "a.txt", "one\ntwo\nthree\n")
        st = self.repo.status()
        sections = sorted(f.section.name for f in st.files)
        self.assertEqual(sections, ["STAGED", "UNSTAGED"])
        staged = next(f for f in st.files if f.section is Section.STAGED)
        unstaged = next(f for f in st.files if f.section is Section.UNSTAGED)
        self.assertIn("+two", self.repo.diff(staged))
        self.assertNotIn("+three", self.repo.diff(staged))
        self.assertIn("+three", self.repo.diff(unstaged))

        self.repo.discard(unstaged)
        st = self.repo.status()
        self.assertEqual([f.section for f in st.files], [Section.STAGED])
        self.assertEqual(st.staged_count, 1)

        _write(self.work, "junk.txt", "x")
        self.repo.discard_all()
        self.assertFalse(os.path.exists(os.path.join(self.work, "junk.txt")))
        self.assertEqual([f.section for f in self.repo.status().files], [Section.STAGED])

    def test_rename_detection(self):
        self._commit_file("old.txt", "same content\n" * 5, "c1")
        _git(self.work, "mv", "old.txt", "new.txt")
        st = self.repo.status()
        self.assertEqual(len(st.files), 1)
        fc = st.files[0]
        self.assertEqual(fc.code, "R")
        self.assertEqual(fc.orig_path, "old.txt")
        self.assertEqual(fc.path, "new.txt")
        self.assertIn("rename from old.txt", self.repo.diff(fc))

    # -- branches / stash --------------------------------------------------------

    def test_branches_switch_merge_delete(self):
        self._commit_file("a.txt", "one\n", "c1")
        self.repo.create_branch("feature")
        self._commit_file("b.txt", "two\n", "c2")
        branches = self.repo.branches()
        self.assertEqual(sorted(b.name for b in branches), ["feature", "main"])
        self.assertTrue(next(b for b in branches if b.name == "feature").is_current)

        self.repo.switch("main")
        self.assertEqual(self.repo.status().branch.head, "main")
        self.repo.merge("feature")
        self.assertTrue(os.path.exists(os.path.join(self.work, "b.txt")))
        self.repo.delete_branch("feature")
        self.assertEqual([b.name for b in self.repo.branches()], ["main"])

    def test_delete_unmerged_requires_force(self):
        self._commit_file("a.txt", "one\n", "c1")
        self.repo.create_branch("wip")
        self._commit_file("w.txt", "w\n", "wip commit")
        self.repo.switch("main")
        with self.assertRaises(GitError) as ctx:
            self.repo.delete_branch("wip")
        self.assertIn("not fully merged", ctx.exception.stderr)
        self.repo.delete_branch("wip", force=True)

    def test_merge_conflict_shows_in_conflicts(self):
        self._commit_file("a.txt", "base\n", "c1")
        self.repo.create_branch("other")
        self._commit_file("a.txt", "theirs\n", "c2")
        self.repo.switch("main")
        self._commit_file("a.txt", "ours\n", "c3")
        with self.assertRaises(GitError):
            self.repo.merge("other")
        st = self.repo.status()
        self.assertTrue(st.has_conflicts)
        self.assertEqual(st.files[0].code, "UU")
        with open(os.path.join(self.work, "a.txt"), encoding="utf-8") as fh:
            self.assertIn("<<<<<<<", fh.read())

    def test_stash_roundtrip(self):
        self._commit_file("a.txt", "one\n", "c1")
        _write(self.work, "a.txt", "changed\n")
        _write(self.work, "new.txt", "untracked\n")
        self.repo.stash_push("my stash", include_untracked=True)
        st = self.repo.status()
        self.assertEqual(st.files, [])
        self.assertEqual(len(st.stashes), 1)
        self.assertIn("my stash", st.stashes[0].message)
        self.assertIn("changed", self.repo.stash_show(0))
        self.repo.stash_pop(0)
        st = self.repo.status()
        self.assertEqual(st.stashes, [])
        self.assertEqual(sorted(f.path for f in st.files), ["a.txt", "new.txt"])

    # -- remotes -----------------------------------------------------------------

    def test_push_pull_with_bare_remote(self):
        bare = os.path.join(self.tmp, "origin.git")
        _git(self.tmp, "init", "-q", "--bare", "-b", "main", bare)
        _git(self.work, "remote", "add", "origin", bare)
        self._commit_file("a.txt", "one\n", "c1")

        st = self.repo.status()
        self.assertIsNone(st.branch.upstream)
        self.assertEqual(self.repo.remotes(), ["origin"])

        self.repo.push(set_upstream="origin")
        st = self.repo.status()
        self.assertEqual(st.branch.upstream, "origin/main")
        self.assertEqual((st.branch.ahead, st.branch.behind), (0, 0))

        self._commit_file("b.txt", "two\n", "c2")
        self.assertEqual(self.repo.status().branch.ahead, 1)
        out = self.repo.sync()
        self.assertEqual(self.repo.status().branch.ahead, 0)
        self.assertIsInstance(out, str)

        # Someone else pushes: we should see "behind" after a fetch, then pull it in.
        clone2 = os.path.join(self.tmp, "clone2")
        _git(self.tmp, "clone", "-q", bare, clone2)
        _write(clone2, "c.txt", "three\n")
        _git(clone2, "add", "c.txt")
        _git(clone2, "commit", "-q", "-m", "c3")
        _git(clone2, "push", "-q")
        self.repo.fetch()
        self.assertEqual(self.repo.status().branch.behind, 1)
        self.repo.pull()
        self.assertEqual(self.repo.status().branch.behind, 0)
        self.assertTrue(os.path.exists(os.path.join(self.work, "c.txt")))
        self.assertTrue(any(b.is_remote and b.name == "origin/main" for b in self.repo.branches()))

    def test_remote_web_url(self):
        self.assertIsNone(self.repo.remote_web_url())
        _git(self.work, "remote", "add", "origin", "git@github.com:someone/thing.git")
        self.assertEqual(self.repo.remote_web_url(), "https://github.com/someone/thing")

    def test_git_error_message(self):
        with self.assertRaises(GitError) as ctx:
            self.repo.run("switch", "does-not-exist")
        self.assertIn("does-not-exist", ctx.exception.message)
        self.assertIn("$ git switch", ctx.exception.output)


if __name__ == "__main__":
    unittest.main()
