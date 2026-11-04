"""api auth tests: the gate, the oauth handshake, and per-user isolation.

github itself is stubbed at the module boundary; the oauth state machine, cookie
handling, dependency gate and session ownership all run for real.

Run: python3 gitadata/tests/test_api_auth.py
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

os.environ["GITHUB_OAUTH_CLIENT_ID"] = "cid-test"
os.environ["GITHUB_OAUTH_CLIENT_SECRET"] = "secret-test"
os.environ["GITADATA_BASE_URL"] = "http://testserver"
os.environ.pop("GITADATA_DEV_PAT", None)

from fastapi.testclient import TestClient  # noqa: E402

from gitadata import github as G  # noqa: E402
from gitadata import server as S  # noqa: E402

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


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def build_remote(tmp: str) -> str:
    origin = os.path.join(tmp, "origin.git")
    git(tmp, "init", "--bare", "-q", "-b", "main", origin)
    git(origin, "config", "uploadpack.allowFilter", "true")
    work = os.path.join(tmp, "work")
    git(tmp, "clone", "-q", origin, work)
    git(work, "config", "user.name", "dev")
    git(work, "config", "user.email", "dev@example.com")
    for i in range(2):
        pathlib.Path(work, "f.txt").open("a").write(f"{i}\n")
        git(work, "add", "f.txt")
        git(work, "commit", "-qm", f"wip {i}")
    git(work, "push", "-q", "-u", "origin", "main")
    return origin


def stub_github(origin: str) -> None:
    G.get_repo = lambda token, owner, repo: {
        "default_branch": "main",
        "size": 0,
        "permissions": {"push": True},
        "full_name": f"{owner}/{repo}",
    }
    G.has_commits = lambda token, owner, repo, branch: True
    G.https_url = lambda owner, repo: origin
    G.git_auth_env = lambda token: {}
    G.branch_protection = lambda token, owner, repo, branch: None
    G.open_pulls = lambda token, owner, repo: []
    G.list_repos = lambda token, limit=200: [
        {"full_name": "arson/gitadata", "private": False, "default_branch": "main", "pushed_at": None, "size_kb": 1, "can_push": True}
    ]


USERS = {"gho_anne": ("anne", 175415090), "gho_bob": ("bob", 222999)}


def install_stubs() -> None:
    def exchange(self, code, verifier):
        return {"access_token": f"gho_{code}", "scope": "repo"}

    def whoami(token):
        login, user_id = USERS[token]
        return {"login": login, "id": user_id, "name": login, "avatar_url": f"{login}.png"}

    S.AUTH._exchange = exchange.__get__(S.AUTH)
    G.whoami = whoami


def sign_in(client: TestClient, code: str) -> str:
    start = client.get("/auth/github/start", follow_redirects=False)
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(start.headers["location"]).query)["state"][0]
    done = client.get(f"/auth/github/callback?code={code}&state={state}", follow_redirects=False)
    return done.status_code


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        origin = build_remote(tmp)
        stub_github(origin)
        install_stubs()
        S.S.SESSION_ROOT = os.path.join(tmp, "sessions")

        client = TestClient(S.app)

        print("\n[gate] nothing works before sign in")
        check("/api/me needs a session", client.get("/api/me").status_code == 401)
        check("/api/sessions needs a session", client.get("/api/sessions").status_code == 401)
        check("opening a repo needs a session", client.post("/api/sessions", json={"repo": "a/b"}).status_code == 401)
        check("health stays open", client.get("/api/health").status_code == 200)
        check("health reports oauth configured", client.get("/api/health").json()["oauth"] is True)

        print("\n[handshake] start -> github -> callback -> cookie")
        start = client.get("/auth/github/start", follow_redirects=False)
        check("start redirects", start.status_code == 302)
        location = start.headers["location"]
        check("to github's authorize page", location.startswith("https://github.com/login/oauth/authorize?"))
        check("callback is this server", "redirect_uri=http%3A%2F%2Ftestserver%2Fauth%2Fgithub%2Fcallback" in location)
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)["state"][0]
        bad = client.get("/auth/github/callback?code=anne&state=not-the-state", follow_redirects=False)
        check("wrong state is refused", bad.status_code == 401, str(bad.status_code))
        done = client.get(f"/auth/github/callback?code=anne&state={state}", follow_redirects=False)
        check("callback redirects home", done.status_code == 302 and done.headers["location"] == "/")
        cookie = done.headers.get("set-cookie", "")
        check("cookie is http only", "httponly" in cookie.lower(), cookie[:80])
        check("cookie is same site lax", "samesite=lax" in cookie.lower())
        me = client.get("/api/me")
        check("identity is available", me.status_code == 200 and me.json()["login"] == "anne", me.text[:120])
        check("token is not exposed", "token" not in me.json())
        check("repos come from this user's token", client.get("/api/github/repos").json()["repos"][0]["full_name"] == "arson/gitadata")

        print("\n[isolation] one user's session is invisible to another")
        opened = client.post("/api/sessions", json={"repo": "arson/gitadata"})
        check("session opened", opened.status_code == 200, opened.text[:120])
        sid = opened.json()["session"]
        check("graph readable by owner", client.get(f"/api/sessions/{sid}/graph").status_code == 200)

        other = TestClient(S.app)
        code_owner = USERS
        check("second user signs in", sign_in(other, "bob") == 302)
        check("second user is someone else", other.get("/api/me").json()["login"] == "bob")
        check("stranger cannot read the graph", other.get(f"/api/sessions/{sid}/graph").status_code == 404)
        check("stranger sees no sessions", other.get("/api/sessions").json()["sessions"] == [])
        check("stranger cannot plan", other.post(f"/api/sessions/{sid}/plan", json={"edits": {}}).status_code == 404)
        check("stranger cannot delete", other.delete(f"/api/sessions/{sid}").status_code == 404)
        check("stranger cannot undo", other.post(f"/api/sessions/{sid}/undo", json={"ts": "x"}).status_code == 404)

        print("\n[cookie] forgery")
        forged = TestClient(S.app)
        forged.cookies.set("gitadata_session", "abc.deadbeef")
        check("forged cookie refused", forged.get("/api/me").status_code == 401)
        forged_hijack = TestClient(S.app)
        real = client.cookies.get("gitadata_session")
        forged_hijack.cookies.set("gitadata_session", real[:-1] + ("0" if real[-1] != "0" else "1"))
        check("edited signature refused", forged_hijack.get("/api/me").status_code == 401)

        print("\n[logout]")
        check("logout accepted", client.post("/auth/logout").status_code == 200)
        check("session gone afterwards", client.get("/api/me").status_code == 401)
        check("sessions unreachable", client.get("/api/sessions").status_code == 401)

        print("\n[dev] local pat mode is opt in and labelled")
        os.environ["GITADATA_DEV_PAT"] = "1"
        os.environ["GITHUB_TOKEN"] = "gho_anne"
        S.AUTH._dev = None
        dev = TestClient(S.app)
        me_dev = dev.get("/api/me")
        check("dev mode reaches the api", me_dev.status_code == 200, me_dev.text[:100])
        check("dev mode says so", me_dev.json()["dev"] is True)
        os.environ.pop("GITADATA_DEV_PAT", None)
        S.AUTH._dev = None
        check("and is off again", TestClient(S.app).get("/api/me").status_code == 401)

        S.S.sweep_orphans(grace_s=0)

    print(f"\n{PASS} checks passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
