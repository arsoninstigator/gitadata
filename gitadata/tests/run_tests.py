"""End-to-end tests for the gitadata engine. Run: python3 tests/run_tests.py"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from gitadata import engine as E  # noqa: E402

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


def git(repo, *args, env=None, check=True):
    full = {**os.environ, **(env or {})}
    p = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, env=full)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr}")
    return p.stdout.strip()


def make_repo(tmp: str) -> dict[str, str]:
    """Build:

        main:    A -- B -- C -- E
        feature:      \\-- D          (D branches from B)
        tag v1 (annotated) -> B ; tag light-c -> C
    """
    r = os.path.join(tmp, "repo")
    os.makedirs(r)
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.name", "Test Bot")
    git(r, "config", "user.email", "bot@example.com")

    def commit(msg: str, when: str, author: str = "Alice <alice@example.com>", extra=()):
        pathlib.Path(r, "f.txt").open("a").write(msg + "\n")
        git(r, "add", "f.txt")
        env = {
            "GIT_AUTHOR_DATE": when,
            "GIT_COMMITTER_DATE": when,
        }
        git(r, "commit", "-q", "--author", author, "-m", msg, *extra, env=env)
        return git(r, "rev-parse", "HEAD")

    a = commit("A: init", "2024-01-01T10:00:00+0530")
    b = commit("B: second", "2024-01-02T10:00:00+0530")
    git(r, "tag", "-a", "v1", "-m", "release one", b)
    c = commit(
        "C: third\n\nBody line.\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n",
        "2024-01-03T10:00:00+0530",
    )
    git(r, "tag", "light-c", c)
    git(r, "checkout", "-q", "-b", "feature", b)
    d = commit("D: feature work", "2024-01-04T10:00:00-0800", author="Bob <bob@example.com>")
    git(r, "checkout", "-q", "main")
    e = commit("E: fifth", "2024-01-05T10:00:00+0000")
    return {"repo": r, "A": a, "B": b, "C": c, "D": d, "E": e}


def test_scope() -> None:
    print("\n[scope] editing B on main only leaves feature alone")
    with tempfile.TemporaryDirectory() as tmp:
        s = make_repo(tmp)
        r = s["repo"]
        plan = E.build_plan(r, {s["B"]: E.CommitEdit(message="B: rewritten\n")}, ["refs/heads/main"])
        check("rewrites B", s["B"] in plan.rewritten)
        check("rewrites descendants C and E", {s["C"], s["E"]} <= set(plan.rewritten))
        check("does not touch A", s["A"] not in plan.rewritten)
        check("does not touch feature commit D", s["D"] not in plan.rewritten)
        check("only main moves", [d.refname for d in plan.changed_refs] == ["refs/heads/main"])
        check("tags untouched", not any("tags" in d.refname for d in plan.changed_refs))

        E.apply_plan(plan)
        check("main moved", git(r, "rev-parse", "main") == plan.new_shas[s["E"]])
        check("feature still on old D", git(r, "rev-parse", "feature") == s["D"])
        check("old B still reachable from feature", git(r, "merge-base", "--is-ancestor", s["B"], "feature", check=False) is not None)


def test_all_refs() -> None:
    print("\n[all refs] editing B across every ref also rebuilds tags and feature")
    with tempfile.TemporaryDirectory() as tmp:
        s = make_repo(tmp)
        r = s["repo"]
        plan = E.build_plan(r, {s["B"]: E.CommitEdit(author_name="Alice Adams")})
        check("feature rewritten too", s["D"] in plan.rewritten)
        names = {d.refname for d in plan.changed_refs}
        check("branches moved", {"refs/heads/main", "refs/heads/feature"} <= names, str(names))
        check("annotated tag moved", "refs/tags/v1" in names, str(names))
        check("light tag moved", "refs/tags/light-c" in names, str(names))

        E.apply_plan(plan)
        check("v1 is still an annotated tag", git(r, "cat-file", "-t", "v1") == "tag")
        check("v1 message kept", git(r, "tag", "-l", "-n1", "v1").endswith("release one"))
        check("v1 points at new B", git(r, "rev-parse", "v1^{commit}") == plan.new_shas[s["B"]])
        check("new author applied", git(r, "show", "-s", "--format=%an", plan.new_shas[s["B"]]) == "Alice Adams")
        check("author email kept", git(r, "show", "-s", "--format=%ae", plan.new_shas[s["B"]]) == "alice@example.com")
        check("fsck clean", git(r, "fsck", "--no-progress", check=False) == "")


def test_dates_preserved() -> None:
    print("\n[dates] untouched timestamps survive a message-only rewrite")
    with tempfile.TemporaryDirectory() as tmp:
        s = make_repo(tmp)
        r = s["repo"]
        plan = E.build_plan(r, {s["C"]: E.CommitEdit(message="C: retitled\n")})
        new_c = plan.new_shas[s["C"]]
        for fmt, field in (("%aI", "author"), ("%cI", "committer")):
            check(
                f"{field} date preserved ({fmt})",
                git(r, "show", "-s", f"--format={fmt}", new_c) == git(r, "show", "-s", f"--format={fmt}", s["C"]),
                f"{git(r, 'show', '-s', f'--format={fmt}', new_c)} vs {git(r, 'show', '-s', f'--format={fmt}', s['C'])}",
            )
        check("offset preserved (-0800 on D)", git(r, "show", "-s", "--format=%aI", s["D"]).endswith("-08:00"), git(r, "show", "-s", "--format=%aI", s["D"]))
        check("content identical", git(r, "diff", "--stat", s["C"], new_c) == "")
        check("E rewritten only for parent link", git(r, "show", "-s", "--format=%s", plan.new_shas[s["E"]]) == "E: fifth")


def test_edit_date_and_trailer() -> None:
    print("\n[date + trailer] retime a root commit, strip only the Claude trailer")
    with tempfile.TemporaryDirectory() as tmp:
        s = make_repo(tmp)
        r = s["repo"]
        plan = E.build_plan(
            r,
            {
                s["A"]: E.CommitEdit(author_date="2023-06-01T09:00:00+0530"),
                s["C"]: E.CommitEdit(strip_trailers_matching=r"^co-authored-by:.*claude"),
            },
            ["refs/heads/main"],
        )
        E.apply_plan(plan)
        new_a = plan.new_shas[s["A"]]
        check("root date changed", git(r, "show", "-s", "--format=%aI", new_a) == "2023-06-01T09:00:00+05:30", git(r, "show", "-s", "--format=%aI", new_a))
        check("root committer date untouched", git(r, "show", "-s", "--format=%cI", new_a) == "2024-01-01T10:00:00+05:30")
        new_c = plan.new_shas[s["C"]]
        body = git(r, "show", "-s", "--format=%B", new_c)
        check("Claude trailer gone", "Claude" not in body, body)
        check("body line kept", "Body line." in body)


def keep_human_coauthor(repo, sha):
    msg = "X: pair\n\nCo-Authored-By: Claude <noreply@anthropic.com>\nCo-Authored-By: Dana <dana@example.com>\n"
    pathlib.Path(repo, "g.txt").write_text("x")
    git(repo, "add", "g.txt")
    env = {"GIT_AUTHOR_DATE": "2024-02-01T00:00:00+0000", "GIT_COMMITTER_DATE": "2024-02-01T00:00:00+0000"}
    git(repo, "commit", "-q", "-m", msg, env=env)
    return git(repo, "rev-parse", "HEAD")


def test_coauthor_filter() -> None:
    print("\n[trailers] excluding one co-author keeps the other")
    with tempfile.TemporaryDirectory() as tmp:
        s = make_repo(tmp)
        r = s["repo"]
        x = keep_human_coauthor(r, s["E"])
        plan = E.build_plan(r, {x: E.CommitEdit(strip_trailers_matching=r"^co-authored-by:.*claude")}, ["refs/heads/main"])
        E.apply_plan(plan)
        body = git(r, "show", "-s", "--format=%B", plan.new_shas[x])
        check("Claude gone", "Claude" not in body, body)
        check("Dana kept", "Dana <dana@example.com>" in body, body)
        check("subject intact", body.startswith("X: pair"))


def test_undo_and_noop() -> None:
    print("\n[undo] snapshot restores refs exactly; empty plan is a no-op")
    with tempfile.TemporaryDirectory() as tmp:
        s = make_repo(tmp)
        r = s["repo"]
        before = {name: git(r, "rev-parse", name) for name in ("main", "feature", "v1", "light-c")}

        plan = E.build_plan(r, {s["B"]: E.CommitEdit(author_name="Chief")})
        E.apply_plan(plan)
        check("refs did move", git(r, "rev-parse", "main") != before["main"])

        stamps = E.list_backups(r)
        check("one snapshot recorded", len(stamps) == 1, str(stamps))
        E.undo(r, stamps[0])
        after = {name: git(r, "rev-parse", name) for name in ("main", "feature", "v1", "light-c")}
        check("all refs restored", before == after, f"{before} != {after}")
        check("snapshot refs cleared", E.list_backups(r) == [])

        noop = E.build_plan(r, {s["B"]: E.CommitEdit(author_name="Alice")})
        check("setting an identical value is a no-op", noop.is_noop, noop.summary())


def test_plan_does_not_move_refs() -> None:
    print("\n[safety] build_plan is read-only with respect to refs")
    with tempfile.TemporaryDirectory() as tmp:
        s = make_repo(tmp)
        r = s["repo"]
        tip = git(r, "rev-parse", "main")
        reflog_before = git(r, "reflog", "--all", "--format=%H %gs")
        E.build_plan(r, {s["B"]: E.CommitEdit(message="danger\n")})
        check("main unchanged after planning", git(r, "rev-parse", "main") == tip)
        check("no reflog movement after planning", git(r, "reflog", "--all", "--format=%H %gs") == reflog_before)


if __name__ == "__main__":
    test_scope()
    test_all_refs()
    test_dates_preserved()
    test_edit_date_and_trailer()
    test_coauthor_filter()
    test_undo_and_noop()
    test_plan_does_not_move_refs()
    print(f"\n{PASS} checks passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  -", f)
    sys.exit(1 if FAIL else 0)
