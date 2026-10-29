"""Entry point: `cursegit [path]`, `cursegit init [path]`, `cursegit clone [url] [dest]`."""
from __future__ import annotations

import argparse
import os
import sys

os.environ.setdefault("ESCDELAY", "25")  # must be set before curses initialises


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cursegit",
        description="A curses Git client: stage, commit, pull, push, branches, stashes, log.",
        epilog="Subcommands: cursegit init [path]   cursegit clone [url] [dest]",
    )
    p.add_argument("path", nargs="?", default=".", help="repository path (default: current directory)")
    p.add_argument("--refresh", type=float, default=5.0, metavar="SECONDS",
                   help="auto-refresh interval; 0 disables (default: 5)")
    p.add_argument("--tty", action="store_true",
                   help="run fetch/pull/push in the terminal so ssh passphrase prompts work")
    p.add_argument("--no-mouse", action="store_true", help="start with mouse support off (M toggles it)")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    from .git import GitError, GitRepo

    try:
        if argv and argv[0] == "init":
            path = argv[1] if len(argv) > 1 else "."
            repo = GitRepo.init(path)
            print(f"Initialised empty repository in {repo.root}")
            return _launch(repo, ".", 5.0, False)
        if argv and argv[0] == "clone":
            if len(argv) < 2:
                # No URL: open the welcome screen, which has an interactive clone flow.
                return _launch(None, ".", 5.0, False)
            url = argv[1]
            dest = argv[2] if len(argv) > 2 else os.path.basename(url.rstrip("/")).removesuffix(".git")
            rc = GitRepo.clone(url, dest)
            if rc != 0:
                return rc
            repo = GitRepo.discover(dest)
            return _launch(repo, dest, 5.0, False) if repo else 1

        args = _parser().parse_args(argv)
        repo = GitRepo.discover(args.path)
        return _launch(repo, args.path, args.refresh, args.tty, mouse=not args.no_mouse)
    except FileNotFoundError as e:
        print(f"cursegit: {e}", file=sys.stderr)
        return 127
    except GitError as e:
        print(f"cursegit: {e.message}", file=sys.stderr)
        return 1


def _launch(repo, path: str, refresh: float, tty: bool, mouse: bool = True) -> int:
    """Open the app on `repo`; with repo=None show the welcome screen (init/clone) first."""
    import curses

    from .ui.app import App
    from .ui.welcome import welcome

    def run(scr):
        target = repo if repo is not None else welcome(scr, path, mouse=mouse)
        if target is None:
            return
        App(target, refresh_interval=refresh, tty_mode=tty, mouse=mouse).run(scr)

    try:
        curses.wrapper(run)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
