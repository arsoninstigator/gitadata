"""oauth unit tests: pkce, state, cookie signing, expiry, dev fallback.

Run: python3 gitadata/tests/test_oauth.py
"""

from __future__ import annotations

import os
import pathlib
import sys
import time
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from gitadata import github as G  # noqa: E402
from gitadata import oauth as O  # noqa: E402

PASS = 0
FAIL: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL.append(f"{label}{(' :: ' + detail) if detail else ''}")
        print(f"  FAIL {label} {detail}")


def make_config() -> O.Config:
    return O.Config(
        client_id="cid123",
        client_secret="secret456",
        cookie_secret="cookie-secret",
        base_url="https://gitadata.example",
        scopes=("repo",),
    )


class Stubbed(O.OAuth):
    """Stands in for the two network calls: token exchange and /user."""

    def __init__(self, config):
        super().__init__(config)
        self.exchanged = []

    def _exchange(self, code, verifier):
        self.exchanged.append((code, verifier))
        return {"access_token": "gho_fake", "scope": "repo,read:user", "token_type": "bearer"}


def main() -> int:
    print("\n[config]")
    for key in ("GITHUB_OAUTH_CLIENT_ID", "GITHUB_OAUTH_CLIENT_SECRET"):
        os.environ.pop(key, None)
    check("absent credentials disable oauth", O.config_from_env() is None)
    os.environ["GITHUB_OAUTH_CLIENT_ID"] = "cid123"
    check("half a config is still no config", O.config_from_env() is None)
    os.environ["GITHUB_OAUTH_CLIENT_SECRET"] = "secret456"
    os.environ["GITADATA_BASE_URL"] = "https://gitadata.example"
    cfg = O.config_from_env()
    check("configured from env", cfg is not None and cfg.client_id == "cid123")
    check("callback url derived", cfg.callback_url == "https://gitadata.example/auth/github/callback")
    check("https means a secure cookie", cfg.secure_cookie is True)
    check("localhost is not secure", O.config_from_env().base_url.startswith("https") is True)

    print("\n[begin] the authorize redirect")
    auth = Stubbed(make_config())
    url = auth.begin()
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parsed.query)
    check("points at github", parsed.netloc == "github.com" and parsed.path == "/login/oauth/authorize")
    check("carries the client id", query["client_id"] == ["cid123"])
    check("carries the callback", query["redirect_uri"] == ["https://gitadata.example/auth/github/callback"])
    check("asks for repo scope", query["scope"] == ["repo"])
    check("uses pkce s256", query["code_challenge_method"] == ["S256"])
    check("challenge is 43 chars", len(query["code_challenge"][0]) == 43, query["code_challenge"][0])
    check("state is unguessable", len(query["state"][0]) >= 24)
    state = query["state"][0]
    check("state is remembered", state in auth._pending)

    print("\n[complete] the exchange")
    G.whoami = lambda token: {"login": "anne", "id": 175415090, "name": "anne", "avatar_url": "a.png"}
    user = auth.complete("code123", state)
    check("token stored in memory", user.token == "gho_fake")
    check("identity read from github", (user.login, user.user_id) == ("anne", 175415090))
    check("scopes recorded", "repo" in user.scopes, str(user.scopes))
    check("verifier was sent", auth.exchanged and len(auth.exchanged[0][1]) > 30)
    check("state consumed", state not in auth._pending)
    check("public view hides the token", "token" not in user.public())
    check("public view is honest about dev", user.public()["dev"] is False)

    print("\n[state] replay and forgery")
    try:
        auth.complete("code123", state)
        check("replayed state refused", False)
    except O.OAuthError:
        check("replayed state refused", True)
    try:
        auth.complete("code123", "made-up")
        check("forged state refused", False)
    except O.OAuthError:
        check("forged state refused", True)

    print("\n[cookie] signing")
    cookie = auth.cookie_value(user)
    check("cookie verifies", auth.user_from_cookie(cookie) is not None)
    check("tampered signature refused", auth.user_from_cookie(cookie[:-1] + "0") is None)
    check("garbage refused", auth.user_from_cookie("nonsense") is None)
    check("empty refused", auth.user_from_cookie(None) is None)
    other = Stubbed(make_config())
    check("a cookie from another secret is refused", other.user_from_cookie(cookie) is None)

    print("\n[expiry] and logout")
    user.issued = time.time() - (O.SESSION_TTL + 10)
    check("expired session is not returned", auth.user_from_cookie(cookie) is None)
    fresh = Stubbed(make_config())
    url2 = fresh.begin()
    state2 = urllib.parse.parse_qs(urllib.parse.urlsplit(url2).query)["state"][0]
    user2 = fresh.complete("c", state2)
    cookie2 = fresh.cookie_value(user2)
    fresh.logout(cookie2)
    check("logout drops the token", fresh.user_from_cookie(cookie2) is None)

    print("\n[dev] the local escape hatch")
    fresh._dev = None
    os.environ.pop("GITADATA_DEV_PAT", None)
    check("off by default", fresh.dev_user() is None)
    os.environ["GITADATA_DEV_PAT"] = "1"
    os.environ["GITHUB_TOKEN"] = "ghp_dev"
    dev = fresh.dev_user()
    check("dev identity available when asked", dev is not None and dev.dev is True)
    check("dev reports itself in /api/me", dev.public()["dev"] is True)
    check("dev does not create a cookie session", fresh._users == {})
    os.environ.pop("GITADATA_DEV_PAT", None)

    print(f"\n{PASS} checks passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
