# Contributing to CurseGit

Develop CurseGit in its own checkout. The local checkout for this integration is
C:\Scripts\CurseGit; Sakura-Claude remains a separate repository.

Install this checkout in editable mode using the Python environment that launches it:

    python -m pip install -e .

For Sakura-Claude's F3 launcher, run from C:\Scripts\Sakura-Claude:

    .venv/Scripts/python.exe -m pip install -e C:/Scripts/CurseGit

This installation imports the checkout directly. Close existing CurseGit windows and press
F3 again after edits. No source port into Sakura-Claude is required for local development.
Sakura-Claude's normal requirements still pin the published snapshot; reinstalling those
requirements replaces the editable install. Update that pin only after publishing a reviewed
CurseGit revision.

Keep curses access on the UI thread. Preview jobs receive captured file data, and their
results are applied only when the request serial and repository epoch still match. At most
one preview is running/queued and one pending selection is retained. Periodic refreshes are
also coalesced. Explicit mutations and initial tab loading remain synchronous.

Mouse buttons and menus must call handle_action so existing busy checks and confirmations
still apply. Ignore button releases for activation, and keep mouse motion from requesting
redraws. Use immediate presses and app-level double-click detection.

Put new Python diagnostics and regression scripts in python/ and PowerShell scripts in ps/.
Run the checks in TESTING_GUIDE.md. Do not edit the installed site-packages copy.
