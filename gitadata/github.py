"""Minimal GitHub client, stdlib only.

The token is never written to disk, never put in an argv, never logged, and
never placed in a URL. Clone/push auth is injected through git's
GIT_CONFIG_COUNT environment mechanism so it stays out of `ps` output.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

API = "https://api.github.com"
UA = "gitadata"


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"github {status}: {message}")
        self.status = status


def token_from_env() -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise GitHubError(0, "no GITHUB_TOKEN in the environment")
    return token


def _request(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict | list]:
    url = path if path.startswith("http") else API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", UA)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw)
            message = payload.get("message", str(exc))
        except Exception:
            message = raw.decode("utf-8", "replace")[:300] or str(exc)
        return exc.code, {"message": message}
    except urllib.error.URLError as exc:
        return 0, {"message": f"network error: {exc.reason}"}


def _ok(method: str, path: str, token: str, body: dict | None = None):
    status, payload = _request(method, path, token, body)
    if status >= 400:
        raise GitHubError(status, payload.get("message", "request failed"))
    return payload


def whoami(token: str) -> dict:
    return _ok("GET", "/user", token)


def get_repo(token: str, owner: str, repo: str) -> dict:
    return _ok("GET", f"/repos/{owner}/{repo}", token)


def list_repos(token: str, limit: int = 200) -> list[dict]:
    repos: list[dict] = []
    page = 1
    while len(repos) < limit:
        batch = _ok(
            "GET",
            f"/user/repos?per_page=100&page={page}&sort=updated&affiliation=owner,collaborator",
            token,
        )
        if not batch:
            break
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return [
        {
            "full_name": r["full_name"],
            "private": r["private"],
            "default_branch": r.get("default_branch"),
            "pushed_at": r.get("pushed_at"),
            "size_kb": r.get("size"),
            "can_push": (r.get("permissions") or {}).get("push", False),
        }
        for r in repos[:limit]
    ]


def has_commits(token: str, owner: str, repo: str, default_branch: str) -> bool:
    status, payload = _request("GET", f"/repos/{owner}/{repo}/commits?per_page=1", token)
    if status == 409:  # empty repository
        return False
    if status >= 400:
        raise GitHubError(status, payload.get("message", "cannot list commits"))
    return bool(payload)


def branch_protection(token: str, owner: str, repo: str, branch: str) -> dict | None:
    """Returns the protection settings, or None when the branch is unprotected."""
    status, payload = _request("GET", f"/repos/{owner}/{repo}/branches/{branch}/protection", token)
    if status == 404:
        return None
    if status >= 400:
        raise GitHubError(status, payload.get("message", "cannot read branch protection"))
    return payload


def open_pulls(token: str, owner: str, repo: str) -> list[dict]:
    try:
        pulls = _ok("GET", f"/repos/{owner}/{repo}/pulls?state=open&per_page=100", token)
    except GitHubError:
        return []
    return [
        {
            "number": p["number"],
            "title": p["title"],
            "head": f"{p['head']['ref']}@{p['head']['sha'][:10]}",
            "base": p["base"]["ref"],
        }
        for p in pulls
    ]


def git_auth_env(token: str) -> dict[str, str]:
    """Environment for git that authenticates over HTTPS without argv leakage.

    Git's smart HTTP transport wants basic auth with the token as the username,
    not a Bearer header (that form is for the REST API). The value is built from
    the token at call time and never written to disk or put in an argv.
    """
    import base64

    credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {credentials}",
        "GIT_TERMINAL_PROMPT": "0",
    }


def https_url(owner: str, repo: str) -> str:
    return f"https://github.com/{owner}/{repo}.git"
