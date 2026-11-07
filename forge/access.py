"""who can see what.

three independent axes, deliberately not collapsed into one flag:

  repo visibility   public / private
  branch visibility public branch inside a public repo, or a private one
  path visibility   a glob list that is stripped for viewers below maintainer

a viewer's role decides all three. roles are global to a project and inherited
from the owning organisation chain, so a suborg team gets access without a
per-project row.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from . import paths as pathlib_

ROLE_RANK = {"reader": 1, "writer": 2, "maintainer": 3, "owner": 4}
PUSH_ROLES = ("maintainer", "owner")  # pushes land in the real repo, unconditionally
PRIVATE_PATH_ROLES = ("maintainer", "owner")


@dataclass(frozen=True)
class Viewer:
    user_id: int | None = None
    login: str = ""
    is_admin: bool = False

    @property
    def authed(self) -> bool:
        return self.user_id is not None

    @classmethod
    def anonymous(cls) -> "Viewer":
        return cls()

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return self.login or "anonymous"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def viewer_from_token(conn, token: str | None) -> Viewer:
    if not token:
        return Viewer.anonymous()
    row = conn.execute(
        "select id, login, is_admin from users where token_hash = ?", (hash_token(token),)
    ).fetchone()
    if row is None:
        return Viewer.anonymous()
    return Viewer(user_id=row["id"], login=row["login"], is_admin=bool(row["is_admin"]))


def _rank(role: str | None) -> int:
    return ROLE_RANK.get(role or "", 0)


def org_ancestors(conn, org_id: int) -> list[int]:
    """the org and every parent above it, nearest first."""
    chain: list[int] = []
    seen: set[int] = set()
    current = org_id
    while current is not None and current not in seen:
        seen.add(current)
        chain.append(current)
        row = conn.execute("select parent_id from orgs where id = ?", (current,)).fetchone()
        current = row["parent_id"] if row else None
    return chain


def project_owner(conn, project) -> dict:
    if project["owner_user_id"] is not None:
        row = conn.execute(
            "select id, login, display_name from users where id = ?", (project["owner_user_id"],)
        ).fetchone()
        return {"kind": "user", "id": row["id"], "slug": row["login"], "name": row["display_name"]}
    row = conn.execute(
        "select id, slug, display_name from orgs where id = ?", (project["owner_org_id"],)
    ).fetchone()
    return {"kind": "org", "id": row["id"], "slug": row["slug"], "name": row["display_name"]}


def project_role(conn, viewer: Viewer, project) -> str | None:
    if not viewer.authed or project is None:
        return None
    if viewer.is_admin:
        return "owner"
    if project["owner_user_id"] == viewer.user_id:
        return "owner"
    row = conn.execute(
        "select role from project_members where project_id = ? and user_id = ?",
        (project["id"], viewer.user_id),
    ).fetchone()
    direct = row["role"] if row else None
    inherited = None
    if project["owner_org_id"] is not None:
        for org_id in reversed(org_ancestors(conn, project["owner_org_id"])):
            found = conn.execute(
                "select role from org_members where org_id = ? and user_id = ?",
                (org_id, viewer.user_id),
            ).fetchone()
            if found and _rank(found["role"]) > _rank(inherited):
                inherited = found["role"]
    best = direct if _rank(direct) >= _rank(inherited) else inherited
    return best


def can_read(conn, viewer: Viewer, project) -> bool:
    if project["visibility"] == "public":
        return True
    return _rank(project_role(conn, viewer, project)) >= ROLE_RANK["reader"]


def can_push(conn, viewer: Viewer, project) -> bool:
    return project_role(conn, viewer, project) in PUSH_ROLES


def sees_private_branches(conn, viewer: Viewer, project) -> bool:
    return _rank(project_role(conn, viewer, project)) >= ROLE_RANK["reader"]


def sees_private_paths(conn, viewer: Viewer, project) -> bool:
    role = project_role(conn, viewer, project)
    return role in PRIVATE_PATH_ROLES


def branch_group(conn, branch) -> dict | None:
    if branch["group_id"] is None:
        return None
    row = conn.execute(
        "select * from branch_groups where id = ?", (branch["group_id"],)
    ).fetchone()
    return dict(row) if row else None


def branch_is_private(conn, branch) -> bool:
    if branch["visibility"] == "private":
        return True
    if branch["visibility"] == "public":
        return False
    group = branch_group(conn, branch)
    return bool(group and group["visibility"] == "private")


def branch_semantics(conn, branch) -> dict:
    own = json.loads(branch["semantics"] or "{}")
    group = branch_group(conn, branch)
    inherited = json.loads(group["semantics"]) if group else {}
    merged = dict(inherited)
    merged.update(own)
    return merged


def list_branches(conn, project_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "select * from branches where project_id = ? order by name", (project_id,)
        )
    ]


def branch_row(conn, project_id: int, name: str) -> dict | None:
    row = conn.execute(
        "select * from branches where project_id = ? and name = ?", (project_id, name)
    ).fetchone()
    return dict(row) if row else None


def visible_branch_names(conn, viewer: Viewer, project, refs: dict | None = None) -> set[str]:
    """branch names the viewer is allowed to know exist."""
    known = set(refs) if refs is not None else set()
    show_private = sees_private_branches(conn, viewer, project)
    rows = {b["name"]: b for b in list_branches(conn, project["id"])}
    names: set[str] = set()
    for name in known:
        meta = rows.get(name)
        if meta is None:
            names.add(name)  # a ref pushed straight to disk, treat as public
            continue
        if show_private or not branch_is_private(conn, meta):
            names.add(name)
    return names


def private_patterns(conn, project_id: int, branch_name: str) -> list[str]:
    """glob patterns to strip when serving `branch_name`."""
    found = []
    for row in conn.execute(
        "select pattern, scope_glob from path_rules where project_id = ? order by pattern",
        (project_id,),
    ):
        if pathlib_.matches(branch_name, row["scope_glob"]):
            found.append(row["pattern"])
    return sorted(set(found))
