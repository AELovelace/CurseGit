"""Thin wrapper around the `git` executable. Every operation is a subprocess call.

Nothing in this module touches curses, so it is testable on its own.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

from . import parse
from .model import Branch, Commit, FileChange, RepoState, Section, Stash

# Keep git from ever waiting on a prompt or opening a pager/editor underneath curses.
BASE_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "GIT_EDITOR": "true",
    "GIT_SEQUENCE_EDITOR": "true",
}
GLOBAL_FLAGS = ["-c", "color.ui=never", "-c", "core.quotepath=false"]

LOG_FORMAT = "--format=%H%x1f%h%x1f%an%x1f%ar%x1f%s%x1e"
REF_FORMAT = ("--format=%(refname:short)%1f%(HEAD)%1f%(upstream:short)%1f"
              "%(upstream:track)%1f%(objectname:short)%1f%(contents:subject)")
STASH_FORMAT = "--format=%gd%x1f%s"


class GitError(Exception):
    def __init__(self, args: list[str], returncode: int, stdout: str = "", stderr: str = ""):
        self.args_ = list(args)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(self.message)

    @property
    def message(self) -> str:
        for line in reversed((self.stderr or self.stdout).splitlines()):
            if line.strip():
                return line.strip()
        verb = self.args_[0] if self.args_ else "?"
        return f"git {verb} failed (exit {self.returncode})"

    @property
    def output(self) -> str:
        return (f"$ git {' '.join(self.args_)}\n(exit {self.returncode})\n"
                f"{self.stderr}\n{self.stdout}").rstrip()


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def combined(self) -> str:
        return (self.stdout.rstrip() + "\n" + self.stderr.rstrip()).strip()


def git_executable() -> str:
    exe = shutil.which("git")
    if not exe:
        raise FileNotFoundError("git was not found on PATH")
    return exe


def _decode(b: bytes) -> str:
    return b.decode("utf-8", "replace")


def _run_raw(git: str, args: list[str], cwd: str | None, env: dict, stdin_text: str | None = None) -> Result:
    kwargs: dict = {}
    if stdin_text is not None:
        kwargs["input"] = stdin_text.encode("utf-8")
    else:
        kwargs["stdin"] = subprocess.DEVNULL
    cp = subprocess.run(
        [git, *GLOBAL_FLAGS, *args],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,
    )
    return Result(cp.returncode, _decode(cp.stdout), _decode(cp.stderr))


class GitRepo:
    def __init__(self, root: str):
        self.root = root
        self.git = git_executable()
        # When True, network ops run attached to the terminal (curses suspended by the caller)
        # so ssh passphrases and similar prompts can be answered.
        self.tty_mode = False
        # Extra `-c key=value` flags for every call (e.g. the GitHub credential helper after sign-in).
        self.extra_flags: list[str] = []

    # -- construction -------------------------------------------------------

    @classmethod
    def discover(cls, path: str = ".") -> "GitRepo | None":
        git = git_executable()
        r = _run_raw(git, ["rev-parse", "--show-toplevel"], path, cls._env())
        if r.returncode != 0:
            return None
        return cls(os.path.normpath(r.stdout.strip()))

    @classmethod
    def init(cls, path: str = ".") -> "GitRepo":
        git = git_executable()
        os.makedirs(path, exist_ok=True)
        r = _run_raw(git, ["init"], path, cls._env())
        if r.returncode != 0:
            raise GitError(["init"], r.returncode, r.stdout, r.stderr)
        return cls(os.path.abspath(path))

    @staticmethod
    def clone(url: str, dest: str | None) -> int:
        """Run `git clone` in the foreground (progress and prompts go to the terminal)."""
        args = [git_executable(), "clone", url] + ([dest] if dest else [])
        return subprocess.call(args)

    @staticmethod
    def _env(allow_prompts: bool = False) -> dict:
        env = dict(os.environ)
        if allow_prompts:
            env["GIT_PAGER"] = "cat"
            env["PAGER"] = "cat"
        else:
            env.update(BASE_ENV)
        return env

    # -- low level ----------------------------------------------------------

    def run_full(self, *args: str, check: bool = True, stdin_text: str | None = None,
                 env_extra: dict | None = None) -> Result:
        env = self._env()
        if env_extra:
            env.update(env_extra)
        r = _run_raw(self.git, [*self.extra_flags, *args], self.root, env, stdin_text)
        if check and r.returncode != 0:
            raise GitError(list(args), r.returncode, r.stdout, r.stderr)
        return r

    def run(self, *args: str, check: bool = True, **kw) -> str:
        return self.run_full(*args, check=check, **kw).stdout

    def net(self, *args: str) -> str:
        """Run a network command; returns combined output. Honors tty_mode."""
        if self.tty_mode:
            rc = subprocess.call([self.git, *GLOBAL_FLAGS, *self.extra_flags, *args], cwd=self.root,
                                 env=self._env(allow_prompts=True))
            if rc != 0:
                raise GitError(list(args), rc, "", "see terminal output above")
            return f"git {args[0]} finished"
        r = self.run_full(*args)
        return r.combined or f"git {args[0]} finished"

    def run_tty(self, cmd: str) -> int:
        """Run an arbitrary shell command attached to the terminal (editor, etc.)."""
        return subprocess.call(cmd, shell=True, cwd=self.root, env=self._env(allow_prompts=True))

    # -- state --------------------------------------------------------------

    @property
    def name(self) -> str:
        return os.path.basename(self.root.rstrip("\\/")) or self.root

    def status(self) -> RepoState:
        out = self.run("status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all",
                       env_extra={"GIT_OPTIONAL_LOCKS": "0"})
        branch, files = parse.parse_status(out)
        return RepoState(branch, files, self.stashes())

    def has_commits(self) -> bool:
        return self.run_full("rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0

    # -- diffs --------------------------------------------------------------

    def diff(self, fc: FileChange, context: int = 3) -> str:
        if fc.section is Section.UNTRACKED:
            return self._untracked_diff(fc.path)
        if fc.section is Section.STAGED:
            return self.run("diff", "--cached", "-M", f"-U{context}", "--", *fc.paths)
        return self.run("diff", "-M", f"-U{context}", "--", *fc.paths)

    def _untracked_diff(self, path: str) -> str:
        r = self.run_full("diff", "--no-index", "--", os.devnull, path, check=False)
        if r.returncode in (0, 1) and r.stdout:
            return r.stdout
        full = os.path.join(self.root, path)
        try:
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError as e:
            return f"(cannot read {path}: {e})"
        if b"\0" in data[:8000]:
            return f"Binary file {path} ({len(data)} bytes)"
        text = _decode(data)
        lines = text.splitlines()
        head = [f"diff --git a/{path} b/{path}", "new file", "--- /dev/null", f"+++ b/{path}",
                f"@@ -0,0 +1,{len(lines)} @@"]
        return "\n".join(head + ["+" + ln for ln in lines])

    def show(self, sha: str) -> str:
        return self.run("show", "--stat", "-p", "-M", sha)

    # -- staging ------------------------------------------------------------

    def stage(self, paths: list[str]) -> None:
        self.run("add", "-A", "--", *paths)

    def stage_all(self) -> None:
        self.run("add", "-A")

    def unstage(self, paths: list[str]) -> None:
        if self.has_commits():
            self.run("restore", "--staged", "--", *paths)
        else:
            self.run("rm", "-r", "-q", "--cached", "--", *paths)

    def unstage_all(self) -> None:
        if self.has_commits():
            self.run("reset", "-q")
        else:
            self.run("rm", "-r", "-q", "--cached", "--", ".")

    def discard(self, fc: FileChange) -> None:
        if fc.section is Section.UNTRACKED:
            self.run("clean", "-f", "-d", "--", fc.path)
        else:
            self.run("restore", "--", *fc.paths)

    def discard_all(self) -> None:
        self.run("restore", "--", ".")
        self.run("clean", "-f", "-d")

    # -- commits ------------------------------------------------------------

    def commit(self, message: str, amend: bool = False) -> str:
        fd, tmp = tempfile.mkstemp(prefix="cursegit-", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(message.rstrip("\n") + "\n")
            args = ["commit", "-F", tmp, "--cleanup=strip"]
            if amend:
                args.append("--amend")
            return self.run(*args)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def last_commit_message(self) -> str:
        return self.run("log", "-1", "--format=%B", check=False).strip()

    def log(self, n: int = 300, ref: str | None = None) -> list[Commit]:
        args = ["log", f"-n{n}", LOG_FORMAT]
        if ref:
            args.append(ref)
        r = self.run_full(*args, check=False)
        if r.returncode != 0:
            return []
        return parse.parse_log(r.stdout)

    # -- remotes ------------------------------------------------------------

    def remotes(self) -> list[str]:
        return [ln.strip() for ln in self.run("remote").splitlines() if ln.strip()]

    def remote_url(self, name: str = "origin") -> str:
        return self.run("remote", "get-url", name, check=False).strip()

    def remote_web_url(self) -> str | None:
        remotes = self.remotes()
        if not remotes:
            return None
        name = "origin" if "origin" in remotes else remotes[0]
        return parse.to_web_url(self.remote_url(name))

    def fetch(self) -> str:
        return self.net("fetch", "--all", "--prune")

    def pull(self) -> str:
        return self.net("pull")

    def push(self, set_upstream: str | None = None) -> str:
        if set_upstream:
            return self.net("push", "-u", set_upstream, "HEAD")
        return self.net("push")

    def sync(self, set_upstream: str | None = None) -> str:
        if set_upstream:
            return self.push(set_upstream)
        return self.pull() + "\n" + self.push()

    # -- branches -----------------------------------------------------------

    def branches(self) -> list[Branch]:
        local = self.run("for-each-ref", REF_FORMAT, "--sort=-HEAD", "--sort=refname", "refs/heads")
        remote = self.run("for-each-ref", REF_FORMAT, "--sort=refname", "refs/remotes")
        return parse.parse_branches(local, remote)

    def switch(self, name: str) -> str:
        return self.run_full("switch", name).combined

    def create_branch(self, name: str, start: str | None = None) -> str:
        args = ["switch", "-c", name] + ([start] if start else [])
        return self.run_full(*args).combined

    def delete_branch(self, name: str, force: bool = False) -> str:
        return self.run("branch", "-D" if force else "-d", name)

    def merge(self, name: str) -> str:
        return self.run_full("merge", "--no-edit", name).combined

    # -- stashes ------------------------------------------------------------

    def stashes(self) -> list[Stash]:
        r = self.run_full("stash", "list", STASH_FORMAT, check=False)
        return parse.parse_stashes(r.stdout) if r.returncode == 0 else []

    def stash_push(self, message: str = "", include_untracked: bool = False) -> str:
        args = ["stash", "push"]
        if include_untracked:
            args.append("-u")
        if message.strip():
            args += ["-m", message.strip()]
        return self.run_full(*args).combined

    def stash_apply(self, index: int) -> str:
        return self.run_full("stash", "apply", f"stash@{{{index}}}").combined

    def stash_pop(self, index: int) -> str:
        return self.run_full("stash", "pop", f"stash@{{{index}}}").combined

    def stash_drop(self, index: int) -> str:
        return self.run_full("stash", "drop", f"stash@{{{index}}}").combined

    def stash_show(self, index: int) -> str:
        return self.run("stash", "show", "-p", "--stat", f"stash@{{{index}}}")

    # -- misc ---------------------------------------------------------------

    def editor(self) -> str:
        for var in ("VISUAL", "EDITOR"):
            if os.environ.get(var):
                return os.environ[var]
        cfg = self.run("config", "core.editor", check=False).strip()
        if cfg:
            return cfg
        return "notepad" if sys.platform == "win32" else "vi"
