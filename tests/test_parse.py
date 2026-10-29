import unittest

from cursegit import parse
from cursegit.model import Section

NUL = "\0"
F = parse.FIELD
R = parse.RECORD


class StatusParsing(unittest.TestCase):
    def test_branch_headers(self):
        raw = NUL.join([
            "# branch.oid 1234567890abcdef",
            "# branch.head main",
            "# branch.upstream origin/main",
            "# branch.ab +2 -1",
            "",
        ])
        branch, files = parse.parse_status(raw)
        self.assertEqual(branch.head, "main")
        self.assertEqual(branch.upstream, "origin/main")
        self.assertEqual((branch.ahead, branch.behind), (2, 1))
        self.assertFalse(branch.detached)
        self.assertEqual(files, [])

    def test_unborn_and_detached(self):
        branch, _ = parse.parse_status(NUL.join(["# branch.oid (initial)", "# branch.head main", ""]))
        self.assertTrue(branch.unborn)
        branch, _ = parse.parse_status(NUL.join(["# branch.oid abc", "# branch.head (detached)", ""]))
        self.assertTrue(branch.detached)
        self.assertIsNone(branch.upstream)

    def test_ordinary_entries_split_into_sections(self):
        raw = NUL.join([
            "1 M. N... 100644 100644 100644 aaa bbb staged.py",
            "1 .M N... 100644 100644 100644 aaa aaa unstaged.py",
            "1 MM N... 100644 100644 100644 aaa bbb both.py",
            "1 A. N... 000000 100644 100644 000 ccc added.py",
            "1 .D N... 100644 100644 000000 aaa aaa gone.py",
            "? new file with spaces.txt",
            "! ignored.log",
            "",
        ])
        _, files = parse.parse_status(raw)
        by = {(f.section, f.path): f for f in files}
        self.assertEqual(by[(Section.STAGED, "staged.py")].code, "M")
        self.assertEqual(by[(Section.UNSTAGED, "unstaged.py")].code, "M")
        self.assertIn((Section.STAGED, "both.py"), by)
        self.assertIn((Section.UNSTAGED, "both.py"), by)
        self.assertEqual(by[(Section.STAGED, "added.py")].code, "A")
        self.assertEqual(by[(Section.UNSTAGED, "gone.py")].code, "D")
        self.assertEqual(by[(Section.UNTRACKED, "new file with spaces.txt")].code, "??")
        self.assertNotIn("ignored.log", [f.path for f in files])

    def test_rename_consumes_following_token(self):
        raw = NUL.join([
            "2 R. N... 100644 100644 100644 aaa aaa R100 new/name.py",
            "old/name.py",
            "? after.txt",
            "",
        ])
        _, files = parse.parse_status(raw)
        self.assertEqual(len(files), 2)
        rename = files[0]
        self.assertEqual(rename.section, Section.STAGED)
        self.assertEqual(rename.code, "R")
        self.assertEqual(rename.path, "new/name.py")
        self.assertEqual(rename.orig_path, "old/name.py")
        self.assertEqual(rename.display, "old/name.py -> new/name.py")
        self.assertEqual(rename.paths, ["old/name.py", "new/name.py"])
        self.assertEqual(files[1].path, "after.txt")

    def test_conflicts_sort_first(self):
        raw = NUL.join([
            "? zzz.txt",
            "1 M. N... 100644 100644 100644 aaa bbb a.py",
            "u UU N... 100644 100644 100644 100644 a b c conflict.py",
            "",
        ])
        _, files = parse.parse_status(raw)
        self.assertEqual([f.section for f in files], [Section.CONFLICT, Section.STAGED, Section.UNTRACKED])
        self.assertEqual(files[0].code, "UU")

    def test_unicode_paths(self):
        raw = NUL.join(["? café/日本.txt", ""])
        _, files = parse.parse_status(raw)
        self.assertEqual(files[0].path, "café/日本.txt")


class LogParsing(unittest.TestCase):
    def test_records(self):
        raw = (F.join(["a" * 40, "aaaaaaa", "Ada", "2 days ago", "first: with" + F + "separator"]) + R + "\n"
               + F.join(["b" * 40, "bbbbbbb", "Bob", "now", "second"]) + R + "\n")
        commits = parse.parse_log(raw)
        self.assertEqual(len(commits), 2)
        self.assertEqual(commits[0].author, "Ada")
        self.assertEqual(commits[0].subject, "first: with" + F + "separator")
        self.assertEqual(commits[1].short, "bbbbbbb")

    def test_empty(self):
        self.assertEqual(parse.parse_log(""), [])


class BranchParsing(unittest.TestCase):
    def test_local_and_remote(self):
        local = "\n".join([
            F.join(["main", "*", "origin/main", "[ahead 1]", "abc1234", "tip"]),
            F.join(["feature", " ", "", "", "def5678", "wip"]),
        ])
        remote = "\n".join([
            F.join(["origin/HEAD", " ", "", "", "abc1234", "tip"]),
            F.join(["origin/main", " ", "", "", "abc1234", "tip"]),
        ])
        branches = parse.parse_branches(local, remote)
        names = [(b.name, b.is_remote, b.is_current) for b in branches]
        self.assertEqual(names, [("main", False, True), ("feature", False, False), ("origin/main", True, False)])
        self.assertEqual(branches[0].track, "[ahead 1]")
        self.assertIsNone(branches[1].upstream)


class StashParsing(unittest.TestCase):
    def test_list(self):
        raw = "stash@{0}" + F + "WIP on main: abc first\nstash@{1}" + F + "On main: named\n"
        stashes = parse.parse_stashes(raw)
        self.assertEqual([s.index for s in stashes], [0, 1])
        self.assertEqual(stashes[1].message, "On main: named")
        self.assertEqual(stashes[1].ref, "stash@{1}")


class WebUrl(unittest.TestCase):
    def test_forms(self):
        cases = {
            "git@github.com:owner/repo.git": "https://github.com/owner/repo",
            "git@github.com:owner/repo": "https://github.com/owner/repo",
            "ssh://git@github.com/owner/repo.git": "https://github.com/owner/repo",
            "https://github.com/owner/repo.git": "https://github.com/owner/repo",
            "https://user@github.com/owner/repo": "https://github.com/owner/repo",
            "https://gitlab.example.com/group/sub/repo.git": "https://gitlab.example.com/group/sub/repo",
            "": None,
        }
        for remote, expected in cases.items():
            with self.subTest(remote=remote):
                self.assertEqual(parse.to_web_url(remote), expected)


if __name__ == "__main__":
    unittest.main()
