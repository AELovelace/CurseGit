"""GitHub sign-in plumbing with the network mocked out, plus the credential helper end to end."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

from cursegit import credential, github


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen_with(responses):
    """Return a fake urlopen that pops (body_dict | HTTPError) responses in order and records requests."""
    calls = []

    def fake(req, timeout=0):
        calls.append(req)
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(json.dumps(item).encode())
    fake.calls = calls
    return fake


def _http_error(code, body):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(json.dumps(body).encode()))


class DeviceFlow(unittest.TestCase):
    def test_request_device_code(self):
        fake = _urlopen_with([{"device_code": "dc", "user_code": "ABCD-1234", "verification_uri": "https://github.com/login/device",
                               "expires_in": 899, "interval": 5}])
        with mock.patch("urllib.request.urlopen", fake):
            code = github.request_device_code("cid")
        self.assertEqual(code.user_code, "ABCD-1234")
        body = fake.calls[0].data.decode()
        self.assertIn("client_id=cid", body)
        self.assertIn("scope=repo+read%3Auser", body)

    def test_poll_states(self):
        fake = _urlopen_with([
            {"error": "authorization_pending"},
            _http_error(400, {"error": "slow_down"}),
            {"access_token": "tok", "token_type": "bearer"},
        ])
        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(github.Pending) as p1:
                github.poll_device_token("cid", "dc")
            self.assertFalse(p1.exception.slow_down)
            with self.assertRaises(github.Pending) as p2:
                github.poll_device_token("cid", "dc")
            self.assertTrue(p2.exception.slow_down)
            self.assertEqual(github.poll_device_token("cid", "dc"), "tok")

    def test_poll_terminal_errors(self):
        for err in ("expired_token", "access_denied", "unsupported_grant_type"):
            with mock.patch("urllib.request.urlopen", _urlopen_with([{"error": err}])):
                with self.assertRaises(github.GitHubError) as ctx:
                    github.poll_device_token("cid", "dc")
                self.assertIn(err, str(ctx.exception))

    def test_wait_for_token_backs_off_and_cancels(self):
        fake = _urlopen_with([{"error": "authorization_pending"}, {"error": "slow_down"}, {"access_token": "tok"}])
        sleeps = []
        code = github.DeviceCode("dc", "UC", "https://github.com/login/device", 900, 5)
        with mock.patch("urllib.request.urlopen", fake):
            tok = github.wait_for_device_token("cid", code, sleep=sleeps.append)
        self.assertEqual(tok, "tok")
        self.assertEqual(sleeps, [5, 10])

        with self.assertRaises(github.GitHubError) as ctx:
            github.wait_for_device_token("cid", code, cancelled=lambda: True, sleep=lambda s: None)
        self.assertIn("cancelled", str(ctx.exception))

    def test_missing_client_id(self):
        with self.assertRaises(github.GitHubError):
            github.request_device_code("  ")


class Rest(unittest.TestCase):
    def test_whoami_and_create_repo(self):
        fake = _urlopen_with([{"login": "edith"}, {"clone_url": "https://github.com/edith/x.git", "html_url": "https://github.com/edith/x"}])
        with mock.patch("urllib.request.urlopen", fake):
            self.assertEqual(github.whoami("tok"), "edith")
            repo = github.create_repo("tok", "x", private=True)
        self.assertEqual(repo["clone_url"], "https://github.com/edith/x.git")
        self.assertEqual(fake.calls[0].get_header("Authorization"), "Bearer tok")
        self.assertEqual(json.loads(fake.calls[1].data)["private"], True)

    def test_api_error_message(self):
        with mock.patch("urllib.request.urlopen", _urlopen_with([_http_error(422, {"message": "name already exists"})])):
            with self.assertRaises(github.GitHubError) as ctx:
                github.create_repo("tok", "x", private=False)
        self.assertIn("name already exists", str(ctx.exception))


class Storage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cursegit-auth-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = mock.patch.dict(os.environ, {"CURSEGIT_CONFIG_DIR": self.tmp})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_roundtrip_and_signout_keeps_client_id(self):
        self.assertFalse(github.load_auth().signed_in)
        github.save_auth(github.Auth(token="t", login="edith", client_id="cid", source="oauth"))
        a = github.load_auth()
        self.assertTrue(a.signed_in)
        self.assertEqual((a.login, a.client_id, a.source), ("edith", "cid", "oauth"))
        a = github.clear_token(a)
        self.assertFalse(a.signed_in)
        self.assertEqual(github.load_auth().client_id, "cid")

    def test_credential_helper_answers_for_github_only(self):
        github.save_auth(github.Auth(token="t0k", login="edith", source="pat"))

        def run(op, stdin):
            out = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(stdin)), mock.patch("sys.stdout", out):
                rc = credential.main([op])
            return rc, out.getvalue()

        rc, out = run("get", "protocol=https\nhost=github.com\n\n")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "username=edith\npassword=t0k\n")
        self.assertEqual(run("get", "protocol=https\nhost=gitlab.com\n\n")[1], "")
        self.assertEqual(run("get", "protocol=https\nhost=github.com\nusername=someone-else\n\n")[1], "")
        self.assertEqual(run("store", "protocol=https\nhost=github.com\n\n")[1], "")

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_git_uses_helper_via_config_args(self):
        """Run real git credential fill with our helper config and see the token come back."""
        github.save_auth(github.Auth(token="t0k", login="edith", source="pat"))
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.path.join(self.tmp, "gc"), GIT_CONFIG_NOSYSTEM="1",
                   GIT_TERMINAL_PROMPT="0", PYTHONPATH=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cp = subprocess.run(["git", *github.helper_config_args(), "credential", "fill"],
                            input="protocol=https\nhost=github.com\n\n", capture_output=True, text=True,
                            env=env, cwd=self.tmp, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("password=t0k", cp.stdout)
        self.assertIn("username=edith", cp.stdout)


if __name__ == "__main__":
    unittest.main()
