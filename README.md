# CurseGit

A terminal Git client in Python + curses that does what the VS Code Source Control panel
does: see changes, stage and unstage, commit, pull, push, sync, switch branches, stash,
and read diffs. No dependencies beyond `git` itself (and `windows-curses` on Windows).

```
pip install -e .          # or: pipx install .
cursegit [path]           # open a repo (defaults to the current directory)
cursegit init [path]      # git init, then open it
cursegit clone <url>      # git clone, then open it
```

Run it without installing: `python -m cursegit`.

Start it somewhere that is not a repository and you get the same choice VS Code offers in an
empty Source Control view: **Initialize Repository** here, or **Clone Repository** (asks for a
URL and a destination, then opens the clone).

## Keys

Press `?` inside the app for the full list. The essentials:

| Key | Action |
|-----|--------|
| `1` `2` `3` `4`, `[` / `]` | Status, Branches, Stash, Log tabs; previous / next tab |
| `↑` `↓` (or `j`/`k`), `PgUp`/`PgDn`, `Home`/`End` | Move in the list, or scroll the diff when it has focus |
| `→` / `←` (or `l`/`h`, `Tab`) | Focus the diff pane / back to the list. On narrow terminals `→` opens the diff full-screen |
| `<` / `>` | Scroll the diff horizontally |
| `s` / `u` / `Space` | Stage / unstage / toggle the selected file |
| `S` / `U` | Stage all / unstage all |
| `d` / `D` | Discard file / discard all working tree changes (asks first) |
| `c` / `C` | Commit / amend (Ctrl-S saves the message, Esc cancels) |
| `f` `p` `P` `y` | Fetch, pull, push, sync. Push publishes the branch if it has no upstream |
| `z` / `Z` | Stash / stash including untracked |
| `e` | Open the file in `$EDITOR` (use this to resolve conflicts, then `s`) |
| `o` | Open the remote in your browser |
| `!` | Show the last git output or error in full |
| `T` | Toggle running network commands in the terminal (for ssh passphrases) |
| `@` | GitHub menu: sign in, publish this repo, sign out |
| `M` | Toggle mouse support |
| `q` | Quit |

Mouse: click a tab to switch, click a row to select it, double-click for Enter, click the diff
pane to focus it, and scroll the wheel over either pane. Start with `--no-mouse` to leave your
terminal's native text selection alone.

## GitHub sign-in

Press `@` (or Ctrl-G) for the GitHub menu. Three ways to sign in, all ending in the same place:

1. **Browser (OAuth device flow).** CurseGit shows a short code, opens github.com in your
   browser, you type the code, done. This is the flow VS Code and the GitHub CLI use. It needs
   an OAuth App client ID the first time: create one at github.com/settings/developers with
   "Enable Device Flow" ticked and paste the Client ID when asked. No client secret is involved.
2. **GitHub CLI.** If `gh auth login` already happened on this machine, reuse that token.
3. **Personal access token.** Paste a token with the `repo` scope.

Once signed in, the header shows `@you`, HTTPS pushes and pulls to github.com authenticate with
the token through a small credential helper, and the token is also handed to your normal git
credential helper so plain `git push` in another terminal works too. **Publish this repository**
in the same menu creates a repo under your account and pushes the current branch with an
upstream, like the VS Code button. **Sign out** forgets the token everywhere.

The token is stored in `%APPDATA%\cursegit\github.json` on Windows or
`~/.config/cursegit/github.json` elsewhere, readable only by your user account.

## How it works

Every action is a `git` subprocess with machine-readable flags (`status --porcelain=v2 -z`,
`for-each-ref --format`, and so on). Git is told never to prompt, page or open an editor.
Authentication goes through your normal credential helper, exactly as it does for VS Code.
Fetch, pull and push run on a background thread so the UI stays responsive; everything
else is fast enough to run inline. After any change the app re-reads `git status`, so what
you see is always what git sees.

## Development

```
python -m unittest discover -s tests -v
```

See `PLAN.md` for the design and roadmap.
