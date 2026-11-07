"""entities: users, organisations, projects, folders, promotions.

orgs nest arbitrarily deep. a project belongs to exactly one of a user or an
org, and the pair (owner, slug) is unique, so a checklist of projects can be
grouped per user or per org without collisions.
"""

from __future__ import annotations

import re
import sqlite3

from .db import utcnow

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
RESERVED_SLUGS = {
    "u",
    "o",
    "api",
    "explore.json",
    "static",
    "health",
    "manifest",
    "favicon.ico",
}

TAG_VOCAB = {
    "self-hostable": "runs from a checkout with no hosted control plane",
    "dependency-light": "few or no runtime dependencies",
    "library": "imported, not run",
    "service": "long running process",
    "cli": "command line tool",
    "spec": "documentation or protocol",
    "readonly-mirror": "published for reading, writes happen upstream",
}


class ForgeError(Exception):
    """a user facing refusal, 4xx."""

    status = 400


class NotFound(ForgeError):
    status = 404


class Forbidden(ForgeError):
    status = 403


class Conflict(ForgeError):
    status = 409


def check_slug(value: str, what: str = "slug") -> str:
    value = (value or "").strip()
    if not SLUG_RE.match(value):
        raise ForgeError(
            f"{what} must match {SLUG_RE.pattern} (lowercase letters, digits, . _ -)"
        )
    if value in RESERVED_SLUGS:
        raise ForgeError(f"{what} '{value}' is reserved")
    if ".." in value:
        raise ForgeError(f"{what} must not contain '..'")
    return value


# ---------------------------------------------------------------- users


def create_user(conn, login: str, display_name: str = "", email: str = "", is_admin=False) -> dict:
    login = check_slug(login, "login")
    try:
        cur = conn.execute(
            "insert into users (login, display_name, email, is_admin, created_date) "
            "values (?,?,?,?,?)",
            (login, display_name or login, email, 1 if is_admin else 0, utcnow()),
        )
    except sqlite3.IntegrityError:
        raise Conflict(f"user '{login}' already exists")
    conn.commit()
    return get_user(conn, login)


def get_user(conn, login: str) -> dict:
    row = conn.execute("select * from users where login = ?", (login,)).fetchone()
    if row is None:
        raise NotFound(f"no user '{login}'")
    return dict(row)


def list_users(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("select * from users order by login")]


def set_token(conn, user_id: int, token_hash: str) -> None:
    conn.execute("update users set token_hash = ? where id = ?", (token_hash, user_id))
    conn.commit()


# ---------------------------------------------------------------- orgs


def create_org(conn, slug: str, parent_path: str = "", display_name: str = "", description="") -> dict:
    slug = check_slug(slug, "org slug")
    parent_id = None
    if parent_path:
        parent_id = resolve_org(conn, parent_path)["id"]
    try:
        cur = conn.execute(
            "insert into orgs (slug, parent_id, display_name, description, created_date) "
            "values (?,?,?,?,?)",
            (slug, parent_id, display_name or slug, description, utcnow()),
        )
    except sqlite3.IntegrityError:
        raise Conflict(f"org '{slug}' already exists under '{parent_path or 'root'}'")
    conn.commit()
    return get_org(conn, cur.lastrowid)


def get_org(conn, org_id: int) -> dict:
    row = conn.execute("select * from orgs where id = ?", (org_id,)).fetchone()
    if row is None:
        raise NotFound(f"no org {org_id}")
    return dict(row)


def org_path(conn, org) -> str:
    parts, seen = [], set()
    current = org
    while current is not None and current["id"] not in seen:
        seen.add(current["id"])
        parts.append(current["slug"])
        parent = current["parent_id"]
        current = get_org(conn, parent) if parent is not None else None
    return "/".join(reversed(parts))


def resolve_org(conn, path: str) -> dict:
    segments = [s for s in (path or "").strip("/").split("/") if s]
    if not segments:
        raise NotFound("no org path given")
    parent_id = None
    org = None
    for segment in segments:
        row = conn.execute(
            "select * from orgs where slug = ? and parent_id is ?", (segment, parent_id)
        ).fetchone()
        if row is None:
            raise NotFound(f"no org '{path}'")
        org = dict(row)
        parent_id = org["id"]
    return org


def list_org_children(conn, org_id: int | None) -> list[dict]:
    if org_id is None:
        rows = conn.execute(
            "select * from orgs where parent_id is null order by slug"
        ).fetchall()
    else:
        rows = conn.execute(
            "select * from orgs where parent_id = ? order by slug", (org_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- projects


def create_project(
    conn,
    owner: str,
    slug: str,
    description: str = "",
    visibility: str = "public",
    default_branch: str = "main",
) -> dict:
    slug = check_slug(slug, "project slug")
    if not (owner or "").strip():
        raise ForgeError("a project needs an owner")
    user = conn.execute("select id from users where login = ?", (owner,)).fetchone()
    org = None
    if user is None:
        try:
            org = resolve_org(conn, owner)
        except NotFound:
            raise NotFound(f"no user or org '{owner}'")
    if visibility not in ("public", "private"):
        raise ForgeError("visibility must be public or private")
    try:
        cur = conn.execute(
            "insert into projects (slug, owner_user_id, owner_org_id, description, visibility, "
            "default_branch, created_date) values (?,?,?,?,?,?,?)",
            (
                slug,
                user["id"] if user else None,
                org["id"] if org else None,
                description,
                visibility,
                default_branch,
                utcnow(),
            ),
        )
    except sqlite3.IntegrityError:
        raise Conflict(f"'{owner}/{slug}' already exists")
    conn.commit()
    return get_project(conn, cur.lastrowid)


def get_project(conn, project_id: int) -> dict:
    row = conn.execute("select * from projects where id = ?", (project_id,)).fetchone()
    if row is None:
        raise NotFound(f"no project {project_id}")
    return dict(row)


def project_path(conn, project) -> str:
    if project["owner_user_id"] is not None:
        row = conn.execute(
            "select login from users where id = ?", (project["owner_user_id"],)
        ).fetchone()
        return f"{row['login']}/{project['slug']}"
    org = get_org(conn, project["owner_org_id"])
    return f"{org_path(conn, org)}/{project['slug']}"


def find_project(conn, owner_path: str, slug: str) -> dict:
    segments = [s for s in (owner_path or "").strip("/").split("/") if s]
    if len(segments) == 1:
        user = conn.execute("select id from users where login = ?", (segments[0],)).fetchone()
        if user is not None:
            row = conn.execute(
                "select * from projects where owner_user_id = ? and slug = ?",
                (user["id"], slug),
            ).fetchone()
            if row:
                return dict(row)
    org = resolve_org(conn, owner_path)
    row = conn.execute(
        "select * from projects where owner_org_id = ? and slug = ?", (org["id"], slug)
    ).fetchone()
    if row is None:
        raise NotFound(f"no project '{owner_path}/{slug}'")
    return dict(row)


def list_owner_projects(conn, owner_path: str) -> list[dict]:
    segments = [s for s in (owner_path or "").strip("/").split("/") if s]
    if len(segments) == 1:
        user = conn.execute("select id from users where login = ?", (segments[0],)).fetchone()
        if user is not None:
            return [
                dict(r)
                for r in conn.execute(
                    "select * from projects where owner_user_id = ? order by slug", (user["id"],)
                )
            ]
    org = resolve_org(conn, owner_path)
    return [
        dict(r)
        for r in conn.execute(
            "select * from projects where owner_org_id = ? order by slug", (org["id"],)
        )
    ]


def list_all_projects(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("select * from projects order by id")]


def project_tags(conn, project_id: int) -> list[str]:
    return [
        r["tag"]
        for r in conn.execute(
            "select tag from project_tags where project_id = ? order by tag", (project_id,)
        )
    ]


def project_dependencies(conn, project_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "select name, kind, version, url, required, note from dependencies "
            "where project_id = ? order by name",
            (project_id,),
        )
    ]


# ---------------------------------------------------------------- folders


def create_folder(conn, owner_path: str, slug: str, title: str = "", position: int = 0) -> dict:
    slug = check_slug(slug, "folder slug")
    user = conn.execute("select id from users where login = ?", (owner_path,)).fetchone()
    org_id = None
    if user is None:
        org_id = resolve_org(conn, owner_path)["id"]
    try:
        cur = conn.execute(
            "insert into folders (slug, title, owner_user_id, owner_org_id, position) "
            "values (?,?,?,?,?)",
            (
                slug,
                title or slug,
                user["id"] if user else None,
                org_id,
                position,
            ),
        )
    except sqlite3.IntegrityError:
        raise Conflict(f"folder '{slug}' already exists for '{owner_path}'")
    conn.commit()
    return dict(
        conn.execute("select * from folders where id = ?", (cur.lastrowid,)).fetchone()
    )


def list_folders(conn, owner_path: str) -> list[dict]:
    user = conn.execute("select id from users where login = ?", (owner_path,)).fetchone()
    if user is not None:
        rows = conn.execute(
            "select * from folders where owner_user_id = ? order by position, slug", (user["id"],)
        )
    else:
        org = resolve_org(conn, owner_path)
        rows = conn.execute(
            "select * from folders where owner_org_id = ? order by position, slug", (org["id"],)
        )
    return [dict(r) for r in rows]


def add_to_folder(conn, folder_id: int, project_id: int, position: int = 0) -> None:
    conn.execute(
        "insert or replace into folder_items (folder_id, project_id, position) values (?,?,?)",
        (folder_id, project_id, position),
    )
    conn.commit()


def folder_of(conn, project_id: int) -> dict | None:
    row = conn.execute(
        "select f.* from folders f join folder_items i on i.folder_id = f.id "
        "where i.project_id = ? order by f.position, f.slug limit 1",
        (project_id,),
    ).fetchone()
    return dict(row) if row else None


def grouped_projects(conn, owner_path: str, projects: list[dict]) -> list[dict]:
    """projects of one owner, folded into their folders.

    a project without a folder lands in an `unsorted` bucket so a preview page
    never silently drops a repository.
    """
    folders = list_folders(conn, owner_path)
    by_folder: dict[int, list[dict]] = {f["id"]: [] for f in folders}
    loose: list[dict] = []
    for project in projects:
        folder = folder_of(conn, project["id"])
        if folder and folder["id"] in by_folder:
            by_folder[folder["id"]].append(project)
        else:
            loose.append(project)
    groups = [
        {
            "slug": f["slug"],
            "title": f["title"],
            "position": f["position"],
            "projects": by_folder[f["id"]],
            "is_default": False,
        }
        for f in folders
        if by_folder[f["id"]]
    ]
    if loose:
        groups.append(
            {"slug": "unsorted", "title": "unsorted", "position": 10**6, "projects": loose, "is_default": True}
        )
    return groups


# ---------------------------------------------------------------- promotions


def promote(conn, project_id: int | None = None, org_id: int | None = None, alias: str = "") -> dict:
    """give a nested project (or suborg) the bare url /@alias."""
    alias = check_slug(alias, "alias")
    if (project_id is None) == (org_id is None):
        raise ForgeError("promote exactly one project or org")
    taken = conn.execute("select id from promotions where alias = ?", (alias,)).fetchone()
    if taken:
        raise Conflict(f"/@{alias} is already promoted")
    if conn.execute("select id from users where login = ?", (alias,)).fetchone():
        raise Conflict(f"/@{alias} collides with the user '{alias}'")
    if conn.execute(
        "select id from orgs where slug = ? and parent_id is null", (alias,)
    ).fetchone():
        raise Conflict(f"/@{alias} collides with the top level org '{alias}'")
    try:
        conn.execute(
            "insert into promotions (alias, kind, project_id, org_id, created_date) values (?,?,?,?,?)",
            (alias, "org" if org_id is not None else "project", project_id, org_id, utcnow()),
        )
    except sqlite3.IntegrityError:
        raise Conflict(f"/@{alias} is already promoted")
    conn.commit()
    return dict(conn.execute("select * from promotions where alias = ?", (alias,)).fetchone())


def resolve_alias(conn, alias: str) -> dict:
    row = conn.execute("select * from promotions where alias = ?", (alias,)).fetchone()
    if row is None:
        raise NotFound(f"/@{alias} is not promoted")
    entry = dict(row)
    if entry["kind"] == "project":
        entry["project"] = get_project(conn, entry["project_id"])
        entry["path"] = project_path(conn, entry["project"])
    else:
        entry["org"] = get_org(conn, entry["org_id"])
        entry["path"] = org_path(conn, entry["org"])
    return entry


def list_promotions(conn) -> list[dict]:
    out = []
    for row in conn.execute("select * from promotions order by alias"):
        entry = dict(row)
        try:
            entry["path"] = (
                project_path(conn, get_project(conn, entry["project_id"]))
                if entry["kind"] == "project"
                else org_path(conn, get_org(conn, entry["org_id"]))
            )
        except NotFound:
            continue
        out.append(entry)
    return out


# ---------------------------------------------------------------- path parsing

GIT_SERVICE_SUFFIXES = ("info/refs", "git-upload-pack", "git-receive-pack", "HEAD")


def split_git_path(segments: list[str]) -> tuple[list[str], str] | None:
    """separate '<owner...>/<project>.git/<rest>' into (owner+project, rest)."""
    for index, segment in enumerate(segments):
        if segment.endswith(".git"):
            owner_and_project = segments[:index] + [segment[: -len(".git")]]
            rest = "/".join(segments[index + 1 :])
            return owner_and_project, rest
    return None
