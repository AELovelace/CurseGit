# Testing CurseGit

From this checkout, using the environment where CurseGit is installed:

    python -m unittest discover -s tests -v
    python -m unittest discover -s python -v
    python -m python.benchmark_render

The original suite exercises Git workflows in temporary repositories, including stage,
commit, branches, stash, and dialog handling. New tests cover immediate mouse presses,
release suppression, double-clicks, right-click targeting, picker/confirmation/editor
buttons, background read coalescing, cache invalidation, stale-result rejection, and
navigation during a blocked diff. No test needs network access or changes your project.

The rendering benchmark draws 20 frames, 35 rows each, containing 10,000-character diff
lines into a dummy 120-column window. On the development machine the original took 2.45 s;
bounded rendering took about 0.08 s. This is a CPU-only rendering comparison, not a claim
about all repository operations.

Windows console input check (run inside an existing repository):

    python -m python.smoke_mouse

This injects mouse records into its own console, switches tabs, scrolls, and quits. It
does not stage, commit, or run network operations. Running it through a ConPTY also checks
the same console input path used when CurseGit opens from Sakura-Claude.

Manual checks:
- Click tabs and file rows; wheel-scroll over each pane.
- Double-click a file to open its diff, then click Close.
- Click Actions or right-click a file; choose an action and cancel its confirmation.
- Use a temporary test repository for stage/unstage/commit checks.
- Click the Save/Cancel controls and reposition the caret in a text dialog.
- Resize to a narrow terminal: Actions must still expose commands hidden from the button row.
- Scroll quickly through many files: selection should remain responsive and only the latest
  preview should appear. Refresh after an external edit and verify the preview changes.
- Press M to disable/re-enable mouse support, and verify keyboard shortcuts still work.
