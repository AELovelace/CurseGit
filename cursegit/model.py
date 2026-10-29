"""Plain data types shared by the git layer and the UI. No I/O here."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Section(Enum):
    CONFLICT = "CONFLICTS"
    STAGED = "STAGED"
    UNSTAGED = "CHANGES"
    UNTRACKED = "UNTRACKED"


@dataclass(frozen=True)
class FileChange:
    path: str
    section: Section
    code: str                     # status letter(s): M A D R C T, ?? for untracked, UU/AA/... for conflicts
    orig_path: str | None = None  # set for renames/copies

    @property
    def display(self) -> str:
        if self.orig_path:
            return f"{self.orig_path} -> {self.path}"
        return self.path

    @property
    def paths(self) -> list[str]:
        return [p for p in (self.orig_path, self.path) if p]


@dataclass
class BranchStatus:
    head: str = "(unknown)"       # branch name, or "(detached)"
    oid: str = ""                 # "(initial)" on an unborn branch
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0

    @property
    def detached(self) -> bool:
        return self.head == "(detached)"

    @property
    def unborn(self) -> bool:
        return self.oid == "(initial)"


@dataclass(frozen=True)
class Stash:
    index: int
    ref: str
    message: str


@dataclass
class RepoState:
    branch: BranchStatus = field(default_factory=BranchStatus)
    files: list[FileChange] = field(default_factory=list)
    stashes: list[Stash] = field(default_factory=list)

    def in_section(self, section: Section) -> list[FileChange]:
        return [f for f in self.files if f.section is section]

    @property
    def has_conflicts(self) -> bool:
        return any(f.section is Section.CONFLICT for f in self.files)

    @property
    def staged_count(self) -> int:
        return sum(1 for f in self.files if f.section is Section.STAGED)

    @property
    def changed_count(self) -> int:
        return sum(1 for f in self.files if f.section in (Section.UNSTAGED, Section.UNTRACKED))

    @property
    def conflict_count(self) -> int:
        return sum(1 for f in self.files if f.section is Section.CONFLICT)


@dataclass(frozen=True)
class Branch:
    name: str
    is_current: bool
    is_remote: bool
    upstream: str | None
    track: str            # e.g. "[ahead 1, behind 2]" or ""
    short_oid: str
    subject: str


@dataclass(frozen=True)
class Commit:
    sha: str
    short: str
    author: str
    when: str
    subject: str
