"""command line for the forge: build a server, seed it, publish it.

    python3 -m forge.cli init --data ./data
    python3 -m forge.cli user add anne --admin
    python3 -m forge.cli project add anne/notes --description "scratch"
    python3 -m forge.cli dep add anne/notes git \
        --kind runtime --version ">=2.30" --url https://git-scm.com
    python3 -m forge.cli tag add anne/notes self-hostable
    python3 -m forge.cli serve --port 8080
"""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
from pathlib import Path

from . import access, branches, catalog, db, gitview, metatags, repos
from .access import Viewer
from .repos import ForgeError, NotFound


def connect(args):
    conn = db.init(Path(args.data) / "forge.db")
    return conn


def project_of(conn, path: str) -> dict:
    segments = [s for s in path.strip("/").split("/") if s]
    if len(segments) < 2:
        raise ForgeError("give a path like owner/project")
    return repos.find_project(conn, "/".join(segments[:-1]), segments[-1])


def out(payload) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


# ---------------------------------------------------------------- commands


def cmd_init(args):
    data = Path(args.data)
    data.mkdir(parents=True, exist_ok=True)
    conn = db.init(data / "forge.db")
    gitview.GitStore(data / "git")
    out({"data": str(data.resolve()), "db": str((data / "forge.db").resolve())})
    conn.close()


def cmd_user_add(args):
    conn = connect(args)
    user = repos.create_user(conn, args.login, args.name or args.login, args.email, args.admin)
    token = secrets.token_urlsafe(24)
    repos.set_token(conn, user["id"], access.hash_token(token))
    out({"login": user["login"], "token": token})
    conn.close()


def cmd_org_add(args):
    conn = connect(args)
    org = repos.create_org(conn, args.slug, args.parent or "", args.name or args.slug, args.description or "")
    out({"org": org, "path": repos.org_path(conn, org)})
    conn.close()


def cmd_project_add(args):
    conn = connect(args)
    project = repos.create_project(
        conn, args.owner, args.slug, args.description or "", args.visibility, args.default_branch
    )
    gitview.GitStore(Path(args.data) / "git").ensure_repo(project["id"])
    out({"project": project, "path": repos.project_path(conn, project)})
    conn.close()


def cmd_group_add(args):
    conn = connect(args)
    project = project_of(conn, args.project)
    group = branches.create_group(
        conn, project["id"], args.slug, args.title or args.slug, args.semantics, args.visibility
    )
    out({"group": group})
    conn.close()


def cmd_branch_set(args):
    conn = connect(args)
    project = project_of(conn, args.project)
    branch = branches.ensure_branch(
        conn,
        project["id"],
        args.branch,
        branches.find_group(conn, project["id"], args.group)["id"] if args.group else None,
        args.visibility or "inherit",
        args.semantics,
        args.protected if args.protected else None,
    )
    out({"branch": branch})
    conn.close()


def cmd_paths_add(args):
    conn = connect(args)
    project = project_of(conn, args.project)
    out({"rule": branches.add_path_rule(conn, project["id"], args.pattern, args.scope, args.note or "")})
    conn.close()


def cmd_tag_add(args):
    conn = connect(args)
    project = project_of(conn, args.project)
    out({"tags": metatags.add_tag(conn, project["id"], args.tag)})
    conn.close()


def cmd_dep_add(args):
    conn = connect(args)
    project = project_of(conn, args.project)
    out(
        {
            "dependencies": metatags.add_dependency(
                conn, project["id"], args.name, args.kind, args.version, args.url, not args.optional, ""
            )
        }
    )
    conn.close()


def cmd_promote(args):
    conn = connect(args)
    project = project_of(conn, args.project)
    entry = repos.promote(conn, project_id=project["id"], alias=args.alias)
    out({"promotion": entry, "url": f"/@{args.alias}"})
    conn.close()


def cmd_manifest(args):
    conn = connect(args)
    project = project_of(conn, args.project)
    print(metatags.manifest_json(conn, project, args.base_url or ""), end="")
    conn.close()


def cmd_catalog(args):
    conn = connect(args)
    store = gitview.GitStore(Path(args.data) / "git")
    viewer = Viewer.anonymous()
    if args.as_user:
        row = repos.get_user(conn, args.as_user)
        viewer = Viewer(user_id=row["id"], login=row["login"], is_admin=bool(row["is_admin"]))
    out({"stats": catalog.stats(conn, store, viewer), "projects": catalog.entries(conn, store, viewer)})
    conn.close()


def cmd_serve(args):
    from .web import ForgeApp

    from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server
    from socketserver import ThreadingMixIn

    class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    app = ForgeApp(Path(args.data), args.base_url or "")
    class Quiet(WSGIRequestHandler):
        def log_message(self, fmt, *a):  # keep one line per request
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

    httpd = make_server(
        args.host, args.port, app, server_class=ThreadingWSGIServer, handler_class=Quiet
    )
    print(f"forge listening on http://{args.host}:{httpd.server_port} (data {args.data})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("stopped")


def cmd_demo(args):
    """a small two user forge with a nested org, a promoted project and a
    private branch, so the shape is visible without reading the tests."""
    data = Path(args.data)
    conn = db.init(data / "forge.db")
    store = gitview.GitStore(data / "git")
    try:
        repos.get_user(conn, "anne")
    except NotFound:
        repos.create_user(conn, "anne", "anne", "anne@example.test", True)
        repos.create_user(conn, "bo", "bo", "bo@example.test")
        stack = repos.create_org(conn, "stack", "", "stack", "shared infrastructure")
        labs = repos.create_org(conn, "labs", "stack", "stack labs", "experiments")
        repos.create_folder(conn, "anne", "tools", "tools", 0)
        repos.create_folder(conn, "anne", "archive", "archive", 1)
        for owner, slug, desc in (
            ("anne", "notes", "scratch notes, published because why not"),
            ("bo", "untitled", "bo's experiments"),
            ("stack/labs", "tinygit", "a git client that hides what you cannot see"),
        ):
            project = repos.create_project(conn, owner, slug, desc)
            store.ensure_repo(project["id"])
        notes = project_of(conn, "anne/notes")
        tools = repos.list_folders(conn, "anne")[0]
        repos.add_to_folder(conn, tools["id"], notes["id"])
        metatags.add_dependency(conn, notes["id"], "git", "runtime", ">=2.30", "https://git-scm.com", True, "")
        metatags.add_tag(conn, notes["id"], "self-hostable")
        tinygit = project_of(conn, "stack/labs/tinygit")
        branches.add_path_rule(conn, tinygit["id"], "secrets/**", "**", "keys stay local")
        group = branches.create_group(
            conn, tinygit["id"], "release", "release", {"channel": "release", "stability": "stable"}
        )
        branches.ensure_branch(conn, tinygit["id"], "main", group["id"], "public")
        branches.ensure_branch(conn, tinygit["id"], "notes", None, "private")
        repos.promote(conn, project_id=tinygit["id"], alias="tinygit")
        out({"ok": True, "data": str(data.resolve()), "projects": len(repos.list_all_projects(conn))})
    finally:
        conn.close()


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forge", description="a lightweight forge")
    parser.add_argument("--data", default="./data", help="data directory (db + bare repos)")
    subs = parser.add_subparsers(dest="command", required=True)

    init = subs.add_parser("init", help="create the data directory and schema")
    init.set_defaults(func=cmd_init)

    user = subs.add_parser("user").add_subparsers(dest="user_command", required=True)
    add = user.add_parser("add")
    add.add_argument("login")
    add.add_argument("--name", default="")
    add.add_argument("--email", default="")
    add.add_argument("--admin", action="store_true")
    add.set_defaults(func=cmd_user_add)

    org = subs.add_parser("org").add_subparsers(dest="org_command", required=True)
    oadd = org.add_parser("add")
    oadd.add_argument("slug")
    oadd.add_argument("--parent", default="")
    oadd.add_argument("--name", default="")
    oadd.add_argument("--description", default="")
    oadd.set_defaults(func=cmd_org_add)

    project = subs.add_parser("project").add_subparsers(dest="project_command", required=True)
    padd = project.add_parser("add")
    padd.add_argument("owner")
    padd.add_argument("slug")
    padd.add_argument("--description", default="")
    padd.add_argument("--visibility", default="public", choices=["public", "private"])
    padd.add_argument("--default-branch", default="main")
    padd.set_defaults(func=cmd_project_add)

    group = subs.add_parser("group").add_subparsers(dest="group_command", required=True)
    gadd = group.add_parser("add")
    gadd.add_argument("project")
    gadd.add_argument("slug")
    gadd.add_argument("--title", default="")
    gadd.add_argument("--semantics", default="{}")
    gadd.add_argument("--visibility", default="public", choices=["public", "private"])
    gadd.set_defaults(func=cmd_group_add)

    branch = subs.add_parser("branch").add_subparsers(dest="branch_command", required=True)
    bset = branch.add_parser("set")
    bset.add_argument("project")
    bset.add_argument("branch")
    bset.add_argument("--group", default="")
    bset.add_argument("--visibility", default="inherit", choices=["inherit", "public", "private"])
    bset.add_argument("--semantics", default=None)
    bset.add_argument("--protected", action="store_true")
    bset.set_defaults(func=cmd_branch_set)

    paths = subs.add_parser("paths").add_subparsers(dest="paths_command", required=True)
    padd2 = paths.add_parser("add")
    padd2.add_argument("project")
    padd2.add_argument("pattern")
    padd2.add_argument("--scope", default="**")
    padd2.add_argument("--note", default="")
    padd2.set_defaults(func=cmd_paths_add)

    tag = subs.add_parser("tag").add_subparsers(dest="tag_command", required=True)
    tadd = tag.add_parser("add")
    tadd.add_argument("project")
    tadd.add_argument("tag")
    tadd.set_defaults(func=cmd_tag_add)

    dep = subs.add_parser("dep").add_subparsers(dest="dep_command", required=True)
    dadd = dep.add_parser("add")
    dadd.add_argument("project")
    dadd.add_argument("name")
    dadd.add_argument("--kind", default="runtime")
    dadd.add_argument("--version", default="any")
    dadd.add_argument("--url", default="")
    dadd.add_argument("--optional", action="store_true")
    dadd.set_defaults(func=cmd_dep_add)

    promote = subs.add_parser("promote")
    promote.add_argument("project")
    promote.add_argument("--as", dest="alias", required=True)
    promote.set_defaults(func=cmd_promote)

    manifest = subs.add_parser("manifest")
    manifest.add_argument("project")
    manifest.add_argument("--base-url", default="")
    manifest.set_defaults(func=cmd_manifest)

    cat = subs.add_parser("catalog")
    cat.add_argument("--as-user", default="")
    cat.set_defaults(func=cmd_catalog)

    serve = subs.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--base-url", default="")
    serve.set_defaults(func=cmd_serve)

    demo = subs.add_parser("demo")
    demo.set_defaults(func=cmd_demo)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except ForgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except gitview.GitError as exc:
        print(f"git error: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
