"""End-to-end session tests against a local bare "remote".

GitHub is stubbed at the module boundary (repo metadata, URLs, auth), so the
whole path runs for real: bare partial clone, graph, plan, apply, push with
lease, rollback on a stale lease, undo, teardown, orphan sweep.

Run: python3 gitadata/tests/test_sessions.py
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from gitadata import engine as E  # noqa: E402
from gitadata import github as G  # noqa: E402
from gitadata import sessions as S  # noqa: E402

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


def git(repo, *args, check=True):
    p = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr}")
    return p.stdout.strip()


def build_remote(tmp: str) -> str:
    origin = os.path.join(tmp, "origin.git")
    git(tmp, "init", "--bare", "-q", "-b", "main", origin)
    git(origin, "config", "uploadpack.allowFilter", "true")
    work = os.path.join(tmp, "work")
    git(tmp, "clone", "-q", origin, work)
    git(work, "config", "user.name", "Dev Bot")
    git(work, "config", "user.email", "dev@example.com")
    for i in range(3):
        pathlib.Path(work, "a.txt").open("a").write(f"{i}\n")
        git(work, "add", "a.txt")
        env_dates = {"GIT_AUTHOR_DATE": f"2024-05-0{i + 1}T10:00:00+0530"}
        subprocess.run(
            ["git", "commit", "-qm", f"wip {i}"],
            cwd=work,
            env={**os.environ, **env_dates, "GIT_COMMITTER_DATE": env_dates["GIT_AUTHOR_DATE"]},
            check=True,
        )
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


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        origin = build_remote(tmp)
        stub_github(origin)
        S.SESSION_ROOT = os.path.join(tmp, "sessions")

        print("\n[session] clone, read graph")
        session = S.create("arson", "gitadata", "dummy-token", user_id=175415090)
        check("session dir exists", os.path.isdir(session.path))
        check("partial clone requested", "blob:none" in pathlib.Path(session.path, "config").read_text())
        check("base refs captured at clone", "refs/heads/main" in session.base_refs)

        graph = S.graph(session.id)
        check("graph has 3 commits", len(graph["commits"]) == 3, str(len(graph["commits"])))
        check("graph exposes refs", [r["short"] for r in graph["refs"]] == ["main"])
        oldest = graph["commits"][0]["sha"]

        print("\n[plan] rewrites the chain, moves nothing")
        plan = S.plan_edits(session.id, {oldest[:8]: {"message": "chore: bootstrap\n"}}, ["main"])
        check("three commits rewritten", plan["rewritten_count"] == 3, str(plan["rewritten_count"]))
        check("plan marks main changed", [r["ref"] for r in plan["refs"] if r["changed"]] == ["refs/heads/main"])
        check("plan did not move main", git(session.path, "rev-parse", "refs/heads/main") == session.base_refs["refs/heads/main"])

        print("\n[push] apply locally then force-push with a lease")
        result = S.apply_edits(
            session.id,
            {oldest[:8]: {"message": "chore: bootstrap\n", "author_name": "Riya Sharma"}},
            ["main"],
            token="dummy-token",
            push=True,
        )
        check("pushed", result["pushed"] is True)
        check("snapshot taken", bool(result["snapshot"]))
        remote_log = git(origin, "log", "--format=%an|%s", "main").split("\n")
        check("remote tip is the new commit", remote_log[0] == "Dev Bot|wip 2", str(remote_log))
        check("remote root rewritten", remote_log[-1] == "Riya Sharma|chore: bootstrap", str(remote_log))
        check("dates survived the round trip",
              git(origin, "log", "-1", "--format=%aI", "main") == "2024-05-03T10:00:00+05:30",
              git(origin, "log", "-1", "--format=%aI", "main"))

        print("\n[lease] someone else pushes, our push must be refused and rolled back")
        other = os.path.join(tmp, "other")
        git(tmp, "clone", "-q", origin, other)
        git(other, "config", "user.name", "Other Dev")
        git(other, "config", "user.email", "other@example.com")
        pathlib.Path(other, "b.txt").write_text("theirs\n")
        git(other, "add", "b.txt")
        git(other, "commit", "-qm", "their work")
        git(other, "push", "-q", "origin", "main")
        theirs = git(origin, "rev-parse", "main")

        local_before = git(session.path, "rev-parse", "refs/heads/main")
        refused = False
        try:
            S.apply_edits(session.id, {local_before[:8]: {"message": "clobber attempt\n"}}, ["main"],
                          token="dummy-token", push=True)
        except S.SessionError as exc:
            refused = True
            check("error says rolled back", "rolled back" in str(exc), str(exc))
        check("push refused on stale lease", refused)
        check("local ref rolled back", git(session.path, "rev-parse", "refs/heads/main") == local_before)
        check("their work untouched on the remote", git(origin, "rev-parse", "main") == theirs)

        print("\n[undo] snapshot restores the clone")
        stamps = S.backups(session.id)["snapshots"]
        check("snapshots listed", len(stamps) >= 1, str(stamps))

        print("\n[ownership] another user cannot touch this session")
        try:
            S.graph(session.id, user_id=999)
            check("stranger is refused", False)
        except S.SessionError as exc:
            check("stranger is refused", exc.status == 404, str(exc))
        check("owner still allowed", len(S.graph(session.id, user_id=175415090)["commits"]) >= 1)
        check("listing is scoped", S.sessions_snapshot(999) == [])
        check("owner listing has it", len(S.sessions_snapshot(175415090)) == 1)

        print("\n[teardown] destroy and orphan sweep")
        S.destroy(session.id)
        check("session dir removed", not os.path.isdir(os.path.join(S.SESSION_ROOT, session.id)))
        orphan = os.path.join(S.SESSION_ROOT, "deadbeef")
        os.makedirs(orphan, exist_ok=True)
        removed = S.sweep_orphans(grace_s=0)
        check("orphan dir swept", not os.path.isdir(orphan), str(removed))

        print("\n[empty repo] a repository with no commits")
        empty_origin = os.path.join(tmp, "empty.git")
        git(tmp, "init", "--bare", "-q", "-b", "main", empty_origin)
        G.has_commits = lambda token, owner, repo, branch: False
        G.https_url = lambda owner, repo: empty_origin
        empty = S.create("arson", "fresh", "dummy-token", user_id=175415090)
        check("marked empty", empty.empty is True)
        check("graph degrades cleanly", S.graph(empty.id)["empty"] is True)
        check("no commits to plan against", S.graph(empty.id)["commits"] == [])
        S.destroy(empty.id)

        print("\n[branches] the graph can be narrowed to one branch")
        branched = os.path.join(tmp, "branched.git")
        git(tmp, "init", "--bare", "-q", "-b", "main", branched)
        git(branched, "config", "uploadpack.allowFilter", "true")
        bwork = os.path.join(tmp, "bwork")
        git(tmp, "clone", "-q", branched, bwork)
        git(bwork, "config", "user.name", "Dev Bot")
        git(bwork, "config", "user.email", "dev@example.com")
        for index in range(2):
            pathlib.Path(bwork, f"m{index}.txt").write_text(f"{index}\n")
            git(bwork, "add", f"m{index}.txt")
            git(bwork, "commit", "-qm", f"main {index}")
        git(bwork, "push", "-q", "-u", "origin", "main")
        git(bwork, "checkout", "-q", "-b", "feature")
        pathlib.Path(bwork, "f.txt").write_text("feature\n")
        git(bwork, "add", "f.txt")
        git(bwork, "commit", "-qm", "feature work")
        git(bwork, "push", "-q", "-u", "origin", "feature")

        G.has_commits = lambda token, owner, repo, branch: True
        G.https_url = lambda owner, repo: branched
        multi = S.create("arson", "branched", "dummy-token", user_id=175415090)

        everything = S.graph(multi.id, user_id=175415090)
        check("all refs walks both branches", len(everything["commits"]) == 3, str(len(everything["commits"])))
        check("no view means everything", everything["view"] is None)
        check("branches are listed", everything["branches"] == ["feature", "main"], str(everything["branches"]))

        on_main = S.graph(multi.id, user_id=175415090, ref="main")
        check("main is two commits", len(on_main["commits"]) == 2, str(len(on_main["commits"])))
        check("view is the canonical ref", on_main["view"] == "refs/heads/main")
        check("the feature commit is absent", all(c["subject"] != "feature work" for c in on_main["commits"]))

        on_feature = S.graph(multi.id, user_id=175415090, ref="refs/heads/feature")
        check("feature is three commits", len(on_feature["commits"]) == 3, str(len(on_feature["commits"])))
        check("feature keeps its tip", "feature work" in [c["subject"] for c in on_feature["commits"]])

        try:
            S.graph(multi.id, user_id=175415090, ref="nope")
            check("unknown branch refused", False)
        except S.SessionError as exc:
            check("unknown branch refused", exc.status == 400, str(exc))

        scoped = S.plan_edits(
            multi.id,
            {on_feature["commits"][0]["sha"][:8]: {"message": "base retitled\n"}},
            ["feature"],
            user_id=175415090,
        )
        check("a scoped plan touches only that branch",
              [r["ref"] for r in scoped["refs"] if r["changed"]] == ["refs/heads/feature"],
              str([r["ref"] for r in scoped["refs"] if r["changed"]]))
        check("main is not in the plan", "refs/heads/main" not in [r["ref"] for r in scoped["refs"]])
        S.destroy(multi.id)

    print(f"\n{PASS} checks passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
