"""gitadata CLI: the engine with a steering wheel.

    python3 -m gitadata.cli graph  REPO
    python3 -m gitadata.cli plan   REPO edits.json [--ref refs/heads/main]
    python3 -m gitadata.cli apply  REPO edits.json [--ref ...] [--push --remote origin]
    python3 -m gitadata.cli backups REPO
    python3 -m gitadata.cli undo    REPO --ts 20261104T114600Z

edits.json is a map of commit (full sha or unique prefix) to fields to change:

    {
      "a1b2c3d": {"message": "feat: real title\\n"},
      "9f8e7d6": {"author_name": "Riya Sharma", "author_email": "riya@example.com"},
      "1122334": {"strip_trailers_matching": "^co-authored-by:.*claude"}
    }
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import sys

from . import engine as E

EDIT_FIELDS = {f.name for f in dataclasses.fields(E.CommitEdit)}


def _load_edits(repo: str, path: str) -> dict[str, E.CommitEdit]:
    raw = json.loads(pathlib.Path(path).read_text())
    if not isinstance(raw, dict):
        raise SystemExit("edits.json must be an object keyed by commit sha")
    known = {c.sha for c in E.read_commits(repo)}
    edits: dict[str, E.CommitEdit] = {}
    for key, fields in raw.items():
        matches = [sha for sha in known if sha.startswith(key)]
        if len(matches) != 1:
            raise SystemExit(f"{key}: {'ambiguous' if matches else 'no such commit'}")
        unknown = set(fields) - EDIT_FIELDS
        if unknown:
            raise SystemExit(f"{key}: unknown field(s) {sorted(unknown)}; valid: {sorted(EDIT_FIELDS)}")
        edits[matches[0]] = E.CommitEdit(**fields)
    return edits


def _refs(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    out = []
    for v in values:
        for prefix in ("refs/heads/", "refs/tags/"):
            if v.startswith(prefix):
                out.append(v)
                break
        else:
            out.append(f"refs/heads/{v}")
    return out


def _print_plan(plan: E.Plan, verbose: bool) -> None:
    print(f"summary: {plan.summary()}")
    if verbose and plan.rewritten:
        print("\ncommits")
        for old, new in plan.rewritten.items():
            print(f"  {old[:10]} -> {new[:10]}")
    if plan.changed_refs:
        print("\nrefs")
        for d in plan.changed_refs:
            print(f"  {d.refname:<28} {d.old_target[:10]} -> {d.new_target[:10]}")
    for w in plan.warnings:
        print(f"warning: {w}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gitadata")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("graph", help="print the commit graph")
    g.add_argument("repo")
    g.add_argument("--ref", action="append")

    for name in ("plan", "apply"):
        p = sub.add_parser(name, help=f"{name} a rewrite")
        p.add_argument("repo")
        p.add_argument("edits")
        p.add_argument("--ref", action="append", help="limit the rewrite to these refs")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true", help="machine-readable plan")
        if name == "apply":
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--push", action="store_true")
            p.add_argument("--remote", default="origin")
            p.add_argument("--no-tags", action="store_true")

    b = sub.add_parser("backups", help="list snapshots")
    b.add_argument("repo")

    u = sub.add_parser("undo", help="restore a snapshot")
    u.add_argument("repo")
    u.add_argument("--ts", required=True)

    args = ap.parse_args(argv)

    try:
        if args.cmd == "graph":
            commits = E.read_commits(args.repo, _refs(args.ref))
            refs = E.read_refs(args.repo)
            tips = {r.commit: r.short for r in refs}
            for c in commits:
                label = tips.get(c.sha, "")
                print(
                    f"{c.sha[:10]} {c.author.iso[:16]} {c.author.name:<18} "
                    f"{c.subject[:60]}{'  ' + label if label else ''}"
                )
            return 0

        if args.cmd == "backups":
            for ts in E.list_backups(args.repo):
                print(ts)
            return 0

        if args.cmd == "undo":
            restored = E.undo(args.repo, args.ts)
            print("restored:\n  " + "\n  ".join(restored))
            return 0

        edits = _load_edits(args.repo, args.edits)
        plan = E.build_plan(args.repo, edits, _refs(args.ref))

        if args.json:
            print(json.dumps(
                {
                    "summary": plan.summary(),
                    "rewritten": plan.rewritten,
                    "refs": [
                        {"ref": d.refname, "old": d.old_target, "new": d.new_target}
                        for d in plan.changed_refs
                    ],
                    "warnings": plan.warnings,
                },
                indent=2,
            ))
        elif not args.quiet:
            _print_plan(plan, verbose=args.cmd == "plan")

        if args.cmd == "plan" or plan.is_noop:
            return 0

        ns = E.apply_plan(plan, dry_run=args.dry_run)
        print(f"\n{'would snapshot' if args.dry_run else 'snapshot'}: {ns}")
        if args.push and not args.dry_run:
            from . import engine

            print(engine.push_plan(plan, remote=args.remote, include_tags=not args.no_tags))
        return 0

    except E.GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
