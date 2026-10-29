"""A git credential helper: `python -m cursegit.credential get`.

Git writes `key=value` lines on stdin and a blank line; we answer with username/password for
github.com when CurseGit holds a token. `store` and `erase` are accepted and ignored, because the
token is managed by the sign-in flow, not by git.
"""
from __future__ import annotations

import sys

from .github import load_auth


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    op = argv[0] if argv else "get"
    attrs: dict[str, str] = {}
    for line in sys.stdin:
        line = line.rstrip("\r\n")
        if not line:
            break
        key, _, value = line.partition("=")
        attrs[key] = value
    if op != "get":
        return 0
    if attrs.get("host") != "github.com" or attrs.get("protocol", "https") != "https":
        return 0
    auth = load_auth()
    if not auth.signed_in:
        return 0
    if attrs.get("username") and attrs["username"] not in (auth.login, "x-access-token", "oauth2"):
        return 0  # git is asking for a different account; let other helpers answer
    sys.stdout.write(f"username={auth.login or 'x-access-token'}\npassword={auth.token}\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
