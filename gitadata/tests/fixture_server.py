"""A dev server backed by a local fixture repository, for ui tests.

    python3 tests/fixture_server.py 8099

github is stubbed, the visitor is a fixed dev identity, and the repository is
built on disk with two branches, a merge and a tag, so the graph, the branch
switcher and the lane layout all get exercised without touching a real account.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from gitadata import github as G
from gitadata import oauth as O
from gitadata import server
from gitadata import sessions as S

FIXTURE_USER = O.AuthUser(sid="fixture", token="fixture-token", login="fixture", user_id=1, name="fixture", dev=True)


def git(repo, *args, env=None):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, env={**os.environ, **(env or {})}
    ).stdout.strip()


def commit(repo, message, when, author="dev <dev@example.com>", path="app.txt"):
    pathlib.Path(repo, path).open("a").write(message + "\n")
    git(repo, "add", path)
    git(
        repo,
        "commit",
        "-q",
        "--author",
        author,
        "-m",
        message,
        env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when},
    )
    return git(repo, "rev-parse", "HEAD")


def build_fixture(root: str) -> str:
    origin = os.path.join(root, "origin.git")
    git(root, "init", "--bare", "-q", "-b", "main", origin)
    git(origin, "config", "uploadpack.allowFilter", "true")
    work = os.path.join(root, "work")
    git(root, "clone", "-q", origin, work)
    git(work, "config", "user.name", "dev")
    git(work, "config", "user.email", "dev@example.com")

    commit(work, "feat: first page", "2026-10-01T10:00:00+0530")
    base = commit(work, "feat: second page\n\nCo-Authored-By: Claude <noreply@anthropic.com>", "2026-10-02T10:00:00+0530")
    git(work, "tag", "-a", "v0.1.0", "-m", "first release", base)

    git(work, "checkout", "-q", "-b", "feature", base)
    commit(work, "feat: search box", "2026-10-03T10:00:00+0530", "riya <riya@example.com>", "search.txt")
    commit(work, "fix: search focus", "2026-10-04T10:00:00+0530", "riya <riya@example.com>", "search.txt")

    git(work, "checkout", "-q", "main")
    commit(work, "chore: tidy imports", "2026-10-05T10:00:00+0530")
    git(
        work,
        "merge",
        "--no-ff",
        "-m",
        "merge branch feature",
        "feature",
        env={"GIT_AUTHOR_DATE": "2026-10-06T10:00:00+0530", "GIT_COMMITTER_DATE": "2026-10-06T10:00:00+0530"},
    )
    commit(work, "docs: readme", "2026-10-07T10:00:00+0530")
    git(work, "push", "-q", "--all", "origin")
    git(work, "push", "-q", "--tags", "origin")
    return origin


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    root = tempfile.mkdtemp(prefix="gitadata-fixture-")
    origin = build_fixture(root)

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
        {"full_name": "fixture/repo", "private": False, "default_branch": "main", "pushed_at": None, "size_kb": 12, "can_push": True}
    ]

    S.SESSION_ROOT = os.path.join(root, "sessions")
    os.makedirs(S.SESSION_ROOT, exist_ok=True)
    server.app.dependency_overrides[server.current_user] = lambda: FIXTURE_USER

    import uvicorn

    print(f"fixture repo at {origin}")
    print(f"serving on http://localhost:{port}/?repo=fixture/repo")
    uvicorn.run(server.app, host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
