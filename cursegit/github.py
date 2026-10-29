"""GitHub sign-in and the few REST calls CurseGit needs. Standard library only.

Three ways to get a token, all producing the same stored credential:
  1. OAuth device flow (browser login). Needs an OAuth App client ID with device flow enabled.
  2. The GitHub CLI's existing login (`gh auth token`).
  3. A pasted personal access token.

The token lives in <config dir>/github.json. `cursegit.credential` serves it to git as a credential
helper for github.com, and `store_git_credential` also hands it to the user's own helper so plain
`git push` in a terminal works too.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

API = "https://api.github.com"
DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
SCOPES = "repo read:user"
USER_AGENT = "cursegit"
CLIENT_ID_HELP = """\
Browser sign-in uses GitHub's OAuth device flow, which needs an OAuth App client ID.
Creating one takes a minute and is free:

  1. Open https://github.com/settings/developers  ->  OAuth Apps  ->  New OAuth App
  2. Application name: anything (e.g. CurseGit). Homepage URL: anything.
     Authorization callback URL: http://localhost (unused by the device flow, but required).
  3. After creating it, tick "Enable Device Flow" and save.
  4. Copy the Client ID (no client secret is needed) and paste it on the next screen.

The ID is saved in your CurseGit config, so this is a one-time step.
Alternatively: sign in with the GitHub CLI (gh auth login) and pick "Use GitHub CLI login",
or paste a personal access token with the `repo` scope.
"""


class GitHubError(Exception):
    pass


class Pending(Exception):
    """Device flow: the user has not finished authorising yet."""

    def __init__(self, slow_down: bool = False):
        self.slow_down = slow_down
        super().__init__("authorization pending")


@dataclass
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass
class Auth:
    token: str = ""
    login: str = ""
    client_id: str = ""
    source: str = ""   # "oauth" | "gh" | "pat"

    @property
    def signed_in(self) -> bool:
        return bool(self.token)


# -- storage --------------------------------------------------------------------

def config_dir() -> str:
    override = os.environ.get("CURSEGIT_CONFIG_DIR")
    if override:
        return override
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "cursegit")


def auth_path() -> str:
    return os.path.join(config_dir(), "github.json")


def load_auth() -> Auth:
    try:
        with open(auth_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return Auth()
    return Auth(token=data.get("token", ""), login=data.get("login", ""),
                client_id=data.get("client_id", ""), source=data.get("source", ""))


def save_auth(auth: Auth) -> None:
    os.makedirs(config_dir(), exist_ok=True)
    path = auth_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"token": auth.token, "login": auth.login, "client_id": auth.client_id,
                   "source": auth.source}, fh, indent=2)
    if hasattr(os, "chmod"):
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)


def clear_token(auth: Auth) -> Auth:
    """Sign out but keep the client ID so the next browser login is one step."""
    auth = Auth(client_id=auth.client_id)
    save_auth(auth)
    return auth


# -- HTTP ----------------------------------------------------------------------

def _request(url: str, data: dict | None = None, token: str | None = None, method: str | None = None,
             form: bool = False, timeout: float = 20.0) -> dict:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    req = urllib.request.Request(url, data=body, headers=headers, method=method or ("POST" if body else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except ValueError:
            payload = {}
        if isinstance(payload, dict) and "error" in payload:
            return payload  # OAuth-style error body; caller interprets it
        msg = payload.get("message") if isinstance(payload, dict) else None
        raise GitHubError(f"GitHub returned {e.code}: {msg or e.reason}") from None
    except urllib.error.URLError as e:
        raise GitHubError(f"Network error: {e.reason}") from None
    try:
        return json.loads(raw.decode("utf-8", "replace")) if raw else {}
    except ValueError:
        raise GitHubError("GitHub returned something that is not JSON") from None


# -- device flow -----------------------------------------------------------------

def request_device_code(client_id: str, scope: str = SCOPES) -> DeviceCode:
    if not client_id.strip():
        raise GitHubError("No OAuth client ID configured")
    d = _request(DEVICE_CODE_URL, {"client_id": client_id.strip(), "scope": scope}, form=True)
    if "error" in d:
        raise GitHubError(_oauth_error(d))
    try:
        return DeviceCode(d["device_code"], d["user_code"], d["verification_uri"],
                          int(d.get("expires_in", 900)), max(int(d.get("interval", 5)), 1))
    except KeyError as e:
        raise GitHubError(f"Unexpected device-code response (missing {e})") from None


def poll_device_token(client_id: str, device_code: str) -> str:
    """One poll. Returns the token, or raises Pending (keep polling) or GitHubError (stop)."""
    d = _request(TOKEN_URL, {
        "client_id": client_id.strip(),
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }, form=True)
    err = d.get("error")
    if not err:
        token = d.get("access_token")
        if not token:
            raise GitHubError("GitHub returned no access token")
        return token
    if err == "authorization_pending":
        raise Pending()
    if err == "slow_down":
        raise Pending(slow_down=True)
    raise GitHubError(_oauth_error(d))


def wait_for_device_token(client_id: str, code: DeviceCode, cancelled=None, sleep=time.sleep) -> str:
    """Poll until authorised. `cancelled` is an optional callable returning True to abort."""
    interval = code.interval
    deadline = time.monotonic() + code.expires_in
    while True:
        if cancelled is not None and cancelled():
            raise GitHubError("Sign-in cancelled")
        if time.monotonic() > deadline:
            raise GitHubError("The device code expired; try again")
        try:
            return poll_device_token(client_id, code.device_code)
        except Pending as p:
            if p.slow_down:
                interval += 5
        sleep(interval)


def _oauth_error(d: dict) -> str:
    err = d.get("error", "error")
    desc = d.get("error_description") or ""
    hints = {
        "expired_token": "the device code expired; try again",
        "access_denied": "you cancelled the sign-in on GitHub",
        "unsupported_grant_type": "device flow is not enabled on this OAuth App",
        "incorrect_client_credentials": "that client ID is not an OAuth App GitHub recognises",
        "device_flow_disabled": "enable Device Flow in the OAuth App settings",
    }
    return f"{err}: {hints.get(err, desc) or desc}".rstrip(": ")


# -- other token sources ---------------------------------------------------------

def gh_cli_token() -> str | None:
    """Reuse the GitHub CLI's login if it is installed and signed in."""
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        cp = subprocess.run([gh, "auth", "token", "-h", "github.com"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    tok = cp.stdout.strip()
    return tok if cp.returncode == 0 and tok else None


# -- REST ------------------------------------------------------------------------

def whoami(token: str) -> str:
    d = _request(f"{API}/user", token=token)
    login = d.get("login")
    if not login:
        raise GitHubError("Token was accepted but no user came back")
    return login


def create_repo(token: str, name: str, private: bool, description: str = "") -> dict:
    """Create a repository under the signed-in user. Returns the API object (clone_url, html_url, ...)."""
    d = _request(f"{API}/user/repos", {"name": name, "private": private, "description": description,
                                        "auto_init": False}, token=token)
    if "clone_url" not in d:
        raise GitHubError(d.get("message", "Repository creation failed"))
    return d


# -- git credential plumbing -------------------------------------------------------

def helper_config_args() -> list[str]:
    """`git -c` arguments that make cursegit.credential answer for github.com over HTTPS."""
    exe = sys.executable.replace("\\", "/")
    helper = f'!"{exe}" -m cursegit.credential'
    return ["-c", "credential.https://github.com.helper=", "-c", f"credential.https://github.com.helper={helper}"]


def store_git_credential(run_full, login: str, token: str) -> None:
    """Hand the token to the user's own credential helper (GCM, osxkeychain, ...) via `git credential approve`."""
    desc = f"protocol=https\nhost=github.com\nusername={login}\npassword={token}\n\n"
    run_full("credential", "approve", check=False, stdin_text=desc)


def erase_git_credential(run_full, login: str) -> None:
    desc = f"protocol=https\nhost=github.com\nusername={login}\n\n"
    run_full("credential", "reject", check=False, stdin_text=desc)
