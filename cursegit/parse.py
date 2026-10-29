"""Parsers for git's machine-readable output. Pure functions, unit-tested."""
from __future__ import annotations

import re

from .model import Branch, BranchStatus, Commit, FileChange, Section, Stash

FIELD = "\x1f"   # unit separator used in our --format strings
RECORD = "\x1e"  # record separator


def parse_status(raw: str) -> tuple[BranchStatus, list[FileChange]]:
    """Parse `git status --porcelain=v2 --branch -z` output."""
    tokens = raw.split("\0")
    branch = BranchStatus()
    files: list[FileChange] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if not tok:
            continue
        if tok.startswith("# "):
            _parse_header(tok[2:], branch)
            continue
        kind = tok[0]
        if kind == "1":
            parts = tok.split(" ", 8)
            files.extend(_ordinary(parts[1], parts[8], None))
        elif kind == "2":
            parts = tok.split(" ", 9)
            orig = tokens[i] if i < len(tokens) else None
            i += 1
            files.extend(_ordinary(parts[1], parts[9], orig))
        elif kind == "u":
            parts = tok.split(" ", 10)
            files.append(FileChange(parts[10], Section.CONFLICT, parts[1]))
        elif kind == "?":
            files.append(FileChange(tok[2:], Section.UNTRACKED, "??"))
        # "!" (ignored) entries are skipped
    order = {Section.CONFLICT: 0, Section.STAGED: 1, Section.UNSTAGED: 2, Section.UNTRACKED: 3}
    files.sort(key=lambda f: (order[f.section], f.path.lower()))
    return branch, files


def _parse_header(line: str, branch: BranchStatus) -> None:
    key, _, value = line.partition(" ")
    if key == "branch.oid":
        branch.oid = value
    elif key == "branch.head":
        branch.head = value
    elif key == "branch.upstream":
        branch.upstream = value
    elif key == "branch.ab":
        m = re.match(r"\+(\d+) -(\d+)", value)
        if m:
            branch.ahead, branch.behind = int(m.group(1)), int(m.group(2))


def _ordinary(xy: str, path: str, orig: str | None) -> list[FileChange]:
    x, y = xy[0], xy[1]
    out = []
    if x != ".":
        out.append(FileChange(path, Section.STAGED, x, orig if x in "RC" else None))
    if y != ".":
        out.append(FileChange(path, Section.UNSTAGED, y, orig if y in "RC" else None))
    return out


def parse_log(raw: str) -> list[Commit]:
    """Parse `git log --format=%H%x1f%h%x1f%an%x1f%ar%x1f%s%x1e`."""
    commits = []
    for rec in raw.split(RECORD):
        rec = rec.strip("\r\n")
        if not rec:
            continue
        parts = rec.split(FIELD)
        if len(parts) < 5:
            continue
        commits.append(Commit(parts[0], parts[1], parts[2], parts[3], FIELD.join(parts[4:])))
    return commits


def parse_branches(local_raw: str, remote_raw: str) -> list[Branch]:
    """Parse two for-each-ref runs (refs/heads and refs/remotes).

    Format per line: refname:short, HEAD marker, upstream:short, upstream:track, objectname:short, subject,
    joined by FIELD. Two runs keep local/remote unambiguous without parsing full ref names.
    """
    out = []
    for raw, is_remote in ((local_raw, False), (remote_raw, True)):
        for line in raw.splitlines():
            parts = line.split(FIELD)
            if len(parts) < 6:
                continue
            name, head, upstream, track, oid = parts[0], parts[1], parts[2], parts[3], parts[4]
            subject = FIELD.join(parts[5:])
            if is_remote and name.endswith("/HEAD"):
                continue
            out.append(Branch(name, head.strip() == "*", is_remote, upstream or None, track, oid, subject))
    return out


def parse_stashes(raw: str) -> list[Stash]:
    """Parse `git stash list --format=%gd%x1f%s`."""
    stashes = []
    for line in raw.splitlines():
        ref, _, msg = line.partition(FIELD)
        m = re.match(r"stash@\{(\d+)\}", ref)
        if not m:
            continue
        stashes.append(Stash(int(m.group(1)), ref, msg))
    return stashes


def to_web_url(remote: str) -> str | None:
    """Turn a git remote URL into something a browser can open."""
    remote = remote.strip()
    if not remote:
        return None
    m = re.match(r"^(?:ssh://)?(?:[\w.-]+@)?([\w.-]+)[:/](.+?)(?:\.git)?/?$", remote)
    if remote.startswith(("http://", "https://")):
        m2 = re.match(r"^(https?://)(?:[^@/]+@)?([^/]+)/(.+?)(?:\.git)?/?$", remote)
        if not m2:
            return None
        return f"{m2.group(1)}{m2.group(2)}/{m2.group(3)}"
    if m:
        host, path = m.group(1), m.group(2)
        return f"https://{host}/{path}"
    return None
