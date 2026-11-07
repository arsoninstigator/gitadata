"""path resolution.

one resolver for every url shape, because the same project is reachable three
ways and each of them has to agree about who owns it:

  /user/project            the plain namespace path
  /org/suborg/project      any depth of nested organisation
  /@project                the promoted bare alias
  /u/user  /o/org/a/b      the preview and organisation pages
"""

from __future__ import annotations

from . import repos
from .repos import NotFound

GIT_ENDPOINTS = ("info/refs", "git-upload-pack", "git-receive-pack")


def _classify_project(project: dict, rest: list[str], *, alias: str | None = None, git: bool = False) -> dict:
    route = {
        "kind": "project",
        "project": project,
        "rest": rest,
        "alias": alias,
        "git_path": git,
    }
    joined = "/".join(rest)
    if git or joined in GIT_ENDPOINTS or (rest and rest[0] in GIT_ENDPOINTS) or (
        rest and rest[0] == "HEAD"
    ):
        route["kind"] = "git"
        if not rest:
            route["service"] = None
        elif joined.startswith("info/refs"):
            route["service"] = "info/refs"
        elif rest[0] == "HEAD":
            route["service"] = "dumb"
        else:
            route["service"] = rest[0]
        return route
    if not rest:
        return route
    head = rest[0]
    if head in ("manifest.json", "deps.json"):
        route["kind"] = "manifest"
        route["format"] = "deps" if head == "deps.json" else "full"
        return route
    if head in ("tree", "blob") and len(rest) >= 2:
        route["kind"] = "files"
        route["mode"] = head
        route["rev"] = rest[1]
        route["file_path"] = "/".join(rest[2:])
        return route
    if head == "branches":
        route["kind"] = "branches"
        route["group"] = rest[1] if len(rest) > 1 else None
        return route
    if head == "repositories.json":
        route["kind"] = "manifest"
        route["format"] = "full"
        return route
    route["kind"] = "notfound"
    return route


def _project_from_path(conn, segments: list[str]) -> dict | None:
    for index in range(len(segments) - 1, 0, -1):
        owner = "/".join(segments[:index])
        segment = segments[index]
        is_git = segment.endswith(".git")
        slug = segment[:-4] if is_git else segment
        try:
            project = repos.find_project(conn, owner, slug)
        except NotFound:
            continue
        return _classify_project(project, segments[index + 1 :], git=is_git)
    return None


def resolve(conn, path: str) -> dict:
    segments = [s for s in (path or "").strip("/").split("/") if s]
    if not segments:
        return {"kind": "home"}
    if segments == ["explore.json"]:
        return {"kind": "explore_json"}
    if segments == ["health"]:
        return {"kind": "health"}
    if segments[0] == "api":
        return {"kind": "api", "rest": segments[1:]}
    if segments[0].startswith("@"):
        alias = segments[0][1:]
        remainder = segments[1:]
        if alias.endswith(".git"):
            alias = alias[:-4]
            remainder = ["git"] + remainder if remainder and remainder[0] not in GIT_ENDPOINTS else remainder
        entry = repos.resolve_alias(conn, alias)
        if entry["kind"] == "project":
            force_git = bool(remainder) and remainder[0] == "git"
            if force_git:
                remainder = remainder[1:]
            return _classify_project(entry["project"], remainder, alias=entry["alias"], git=force_git)
        return {"kind": "org", "org": entry["org"], "rest": remainder, "alias": entry["alias"]}
    if segments[0] == "u" and len(segments) >= 2:
        user = repos.get_user(conn, segments[1])
        return {"kind": "user", "user": user, "rest": segments[2:]}
    if segments[0] == "o" and len(segments) >= 2:
        body = segments[1:]
        for cut in range(len(body), 0, -1):
            try:
                org = repos.resolve_org(conn, "/".join(body[:cut]))
            except NotFound:
                continue
            rest = body[cut:]
            if not rest:
                return {"kind": "org", "org": org, "rest": []}
            is_git = rest[0].endswith(".git")
            slug = rest[0][:-4] if is_git else rest[0]
            try:
                project = repos.find_project(conn, repos.org_path(conn, org), slug)
            except NotFound:
                return {"kind": "notfound"}
            return _classify_project(project, rest[1:], git=is_git)
        return {"kind": "notfound"}
    found = _project_from_path(conn, segments)
    if found is not None:
        return found
    if len(segments) == 1:
        user = conn.execute("select login from users where login = ?", (segments[0],)).fetchone()
        if user is not None:
            return {"kind": "user", "user": repos.get_user(conn, segments[0]), "rest": []}
        try:
            org = repos.resolve_org(conn, segments[0])
            return {"kind": "org", "org": org, "rest": []}
        except NotFound:
            pass
    if len(segments) > 1:
        try:
            org = repos.resolve_org(conn, "/".join(segments))
            return {"kind": "org", "org": org, "rest": []}
        except NotFound:
            pass
    return {"kind": "notfound"}
