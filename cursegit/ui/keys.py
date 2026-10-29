"""The keymap. One table drives both dispatch and the help screen."""
from __future__ import annotations

from dataclasses import dataclass

SCOPES = ("status", "branches", "stash", "log")
SCOPE_TITLES = {"global": "Everywhere", "status": "Status tab", "branches": "Branches tab",
                "stash": "Stash tab", "log": "Log tab"}


@dataclass(frozen=True)
class Binding:
    keys: tuple[str, ...]
    action: str
    help: str
    scope: str = "global"


B = Binding
BINDINGS: list[Binding] = [
    # navigation / app
    B(("q",), "quit", "Quit"),
    B(("?", "f1"), "help", "This help"),
    B(("1", "2", "3", "4"), "tab", "Switch tab: Status / Branches / Stash / Log"),
    B(("]",), "next_tab", "Next tab"),
    B(("[", "shift-tab"), "prev_tab", "Previous tab"),
    B(("tab",), "focus", "Toggle focus between list and detail pane"),
    B(("right", "l"), "focus_detail", "Focus the diff/detail pane (opens it full-screen on narrow terminals)"),
    B(("left", "h"), "focus_list", "Focus the list pane"),
    B(("r", "f5"), "refresh", "Refresh now"),
    B(("down", "j"), "down", "Move down / scroll"),
    B(("up", "k"), "up", "Move up / scroll"),
    B(("pgdn", "ctrl-f", "ctrl-d"), "pgdn", "Page down"),
    B(("pgup", "ctrl-b", "ctrl-u"), "pgup", "Page up"),
    B(("home", "g"), "home", "Jump to top"),
    B(("end", "G"), "end", "Jump to bottom"),
    B(("<",), "hleft", "Scroll detail left"),
    B((">",), "hright", "Scroll detail right"),
    B(("enter",), "open_detail", "Open detail full-screen"),
    # remotes (VS Code: Fetch / Pull / Push / Sync / Publish Branch)
    B(("f",), "fetch", "Fetch all remotes (--prune)"),
    B(("p",), "pull", "Pull"),
    B(("P",), "push", "Push; publishes the branch if it has no upstream, or offers Publish to GitHub if there is no remote"),
    B(("y",), "sync", "Sync: pull, then push (offers Publish to GitHub when there is no remote)"),
    B(("o",), "open_remote", "Open the remote in your browser"),
    B(("!",), "last_output", "Show the last git output / error"),
    B(("T",), "toggle_tty", "Toggle running fetch/pull/push in the terminal (for ssh prompts)"),
    B(("M",), "toggle_mouse", "Toggle mouse support"),
    B(("@", "ctrl-g"), "github", "GitHub: sign in, publish this repo, sign out"),
    # commits / stash from anywhere
    B(("S",), "stage_all", "Stage all changes (from any tab)"),
    B(("U",), "unstage_all", "Unstage all changes (from any tab)"),
    B(("c",), "commit", "Commit staged changes"),
    B(("C",), "amend", "Amend the last commit"),
    B(("z",), "stash_push", "Stash tracked changes"),
    B(("Z",), "stash_push_all", "Stash including untracked files"),
    # status tab
    B(("s",), "stage", "Stage file", "status"),
    B(("u",), "unstage", "Unstage file", "status"),
    B(("space",), "toggle_stage", "Stage / unstage file", "status"),
    B(("d",), "discard", "Discard changes to file (asks first)", "status"),
    B(("D",), "discard_all", "Discard ALL working tree changes (asks first)", "status"),
    B(("e",), "edit", "Open file in your editor (resolve conflicts here)", "status"),
    # branches tab
    B(("enter",), "switch", "Checkout branch (remote branches get a tracking branch)", "branches"),
    B(("n",), "new_branch", "Create a branch from HEAD and switch to it", "branches"),
    B(("D",), "delete_branch", "Delete local branch (asks first)", "branches"),
    B(("m",), "merge", "Merge branch into the current branch", "branches"),
    # stash tab
    B(("a",), "stash_apply", "Apply stash (keeps it)", "stash"),
    B(("A",), "stash_pop", "Pop stash (apply, then drop)", "stash"),
    B(("D",), "stash_drop", "Drop stash (asks first)", "stash"),
    # log tab
    B(("b",), "branch_at", "Create a branch at this commit", "log"),
]

NAV_ACTIONS = {"down", "up", "pgdn", "pgup", "home", "end", "hleft", "hright"}
NETWORK_ACTIONS = {"fetch", "pull", "push", "sync"}
MUTATING_ACTIONS = NETWORK_ACTIONS | {
    "stage", "unstage", "toggle_stage", "stage_all", "unstage_all", "discard", "discard_all", "edit",
    "commit", "amend", "stash_push", "stash_push_all", "switch", "new_branch", "delete_branch", "merge",
    "stash_apply", "stash_pop", "stash_drop", "branch_at",
}


def lookup(key: str, scope: str) -> str | None:
    """Tab-scoped bindings win over global ones."""
    for b in BINDINGS:
        if b.scope == scope and key in b.keys:
            return b.action
    for b in BINDINGS:
        if b.scope == "global" and key in b.keys:
            return b.action
    return None


def help_lines(scope: str) -> list[str]:
    """Render the keymap for the help popup: the current tab first, then global."""
    lines: list[str] = []
    for sc in (scope, "global"):
        lines.append(f"{SCOPE_TITLES[sc]}")
        lines.append("")
        for b in BINDINGS:
            if b.scope != sc:
                continue
            keys = " / ".join(_pretty(k) for k in b.keys)
            lines.append(f"  {keys:<20} {b.help}")
        lines.append("")
    lines += [
        "Mouse",
        "",
        "  click action button  Run the labeled command",
        "  right-click row      Open actions for that row",
        "  click tab            Switch tab",
        "  click row            Select it (and focus the list)",
        "  double-click row     Same as Enter on it",
        "  click detail pane    Focus the detail pane",
        "  wheel                Scroll whichever pane is under the pointer",
        "",
    ]
    return lines


def _pretty(k: str) -> str:
    return {"enter": "Enter", "tab": "Tab", "shift-tab": "Shift-Tab", "space": "Space", "esc": "Esc",
            "up": "↑", "down": "↓", "left": "←", "right": "→", "pgup": "PgUp",
            "pgdn": "PgDn", "home": "Home", "end": "End", "f1": "F1", "f5": "F5",
            }.get(k, k.replace("ctrl-", "Ctrl-"))
