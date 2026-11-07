"""branch groups, semantic tags and path rules.

a branch group is a named bucket of branches that carries semantics: `channel:
release`, `stability: stable`, `semver: 1.4.0`. a branch inherits its group's
semantics and may add or override keys. that is what makes "release branches"
queryable instead of a naming convention people have to guess.

path rules are per project and scoped by a branch glob, so `secrets/**` can be
private on every branch while a second rule keeps `drafts/**` private only on
the release channel.
"""

from __future__ import annotations

import json
import re

from . import paths as pathlib_
from .db import utcnow
from .repos import Conflict, ForgeError, NotFound, check_slug

SEMANTIC_KEYS = {
    "channel": {"release", "feature", "experiment", "hotfix", "maintenance", "integration"},
    "stability": {"stable", "beta", "alpha", "nightly"},
}
FREE_KEYS = {"semver", "owner", "lts", "since", "eol", "lang", "target"}
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(-[0-9a-z.]+)?$")
KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")


def validate_semantics(semantics: dict | str | None, where: str = "semantics") -> dict:
    if semantics in (None, ""):
        return {}
    if isinstance(semantics, str):
        try:
            semantics = json.loads(semantics)
        except json.JSONDecodeError as exc:
            raise ForgeError(f"{where} is not valid json: {exc.msg}")
    if not isinstance(semantics, dict):
        raise ForgeError(f"{where} must be a json object")
    cleaned: dict = {}
    for key, value in semantics.items():
        if not KEY_RE.match(str(key)):
            raise ForgeError(f"{where}: key '{key}' must be lowercase [a-z0-9_.-]")
        if key in SEMANTIC_KEYS:
            if value not in SEMANTIC_KEYS[key]:
                allowed = ", ".join(sorted(SEMANTIC_KEYS[key]))
                raise ForgeError(f"{where}: {key} must be one of {allowed}")
        elif key == "semver":
            if not isinstance(value, str) or not SEMVER_RE.match(value):
                raise ForgeError(f"{where}: semver must look like 1.4.0")
        elif key == "lts":
            if not isinstance(value, bool):
                raise ForgeError(f"{where}: lts must be true or false")
        else:
            if isinstance(value, (dict, list)):
                raise ForgeError(f"{where}: {key} must be a scalar")
        cleaned[str(key)] = value
    # a release channel without a stability promise is not a release channel
    if cleaned.get("channel") == "release" and "stability" not in cleaned:
        raise ForgeError(f"{where}: channel=release also needs a stability tag")
    return cleaned


def create_group(
    conn,
    project_id: int,
    slug: str,
    title: str = "",
    semantics: dict | str | None = None,
    visibility: str = "public",
    position: int = 0,
) -> dict:
    slug = check_slug(slug, "group slug")
    if visibility not in ("public", "private"):
        raise ForgeError("group visibility must be public or private")
    clean = validate_semantics(semantics, f"group {slug}")
    try:
        cur = conn.execute(
            "insert into branch_groups (project_id, slug, title, semantics, visibility, position) "
            "values (?,?,?,?,?,?)",
            (project_id, slug, title or slug, json.dumps(clean, sort_keys=True), visibility, position),
        )
    except Exception as exc:  # sqlite3.IntegrityError and friends
        if "unique" in str(exc).lower():
            raise Conflict(f"group '{slug}' already exists in this project")
        raise
    conn.commit()
    return get_group(conn, cur.lastrowid)


def get_group(conn, group_id: int) -> dict:
    row = conn.execute("select * from branch_groups where id = ?", (group_id,)).fetchone()
    if row is None:
        raise NotFound(f"no branch group {group_id}")
    return dict(row)


def find_group(conn, project_id: int, slug: str) -> dict:
    row = conn.execute(
        "select * from branch_groups where project_id = ? and slug = ?", (project_id, slug)
    ).fetchone()
    if row is None:
        raise NotFound(f"no group '{slug}'")
    return dict(row)


def list_groups(conn, project_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "select * from branch_groups where project_id = ? order by position, slug",
            (project_id,),
        )
    ]


def set_group_semantics(conn, group_id: int, semantics) -> dict:
    clean = validate_semantics(semantics, "group semantics")
    conn.execute(
        "update branch_groups set semantics = ? where id = ?",
        (json.dumps(clean, sort_keys=True), group_id),
    )
    conn.commit()
    return get_group(conn, group_id)


def ensure_branch(
    conn,
    project_id: int,
    name: str,
    group_id: int | None = None,
    visibility: str = "inherit",
    semantics: dict | str | None = None,
    protected: bool | None = None,
) -> dict:
    if visibility not in ("inherit", "public", "private"):
        raise ForgeError("branch visibility must be inherit, public or private")
    clean = validate_semantics(semantics, f"branch {name}")
    row = conn.execute(
        "select * from branches where project_id = ? and name = ?", (project_id, name)
    ).fetchone()
    if row is None:
        conn.execute(
            "insert into branches (project_id, name, group_id, visibility, semantics, protected, "
            "updated_date) values (?,?,?,?,?,?,?)",
            (
                project_id,
                name,
                group_id,
                visibility,
                json.dumps(clean, sort_keys=True),
                1 if protected else 0,
                utcnow(),
            ),
        )
    else:
        sets, args = [], []
        if group_id is not None:
            sets.append("group_id = ?")
            args.append(group_id)
        if visibility != "inherit" or row["visibility"] not in ("inherit", "public", "private"):
            sets.append("visibility = ?")
            args.append(visibility)
        if semantics is not None:
            sets.append("semantics = ?")
            args.append(json.dumps(clean, sort_keys=True))
        if protected is not None:
            sets.append("protected = ?")
            args.append(1 if protected else 0)
        if sets:
            args.extend([project_id, name])
            conn.execute(
                f"update branches set {', '.join(sets)} where project_id = ? and name = ?", args
            )
    conn.commit()
    return dict(
        conn.execute("select * from branches where project_id = ? and name = ?", (project_id, name)).fetchone()
    )


def assign_group(conn, project_id: int, name: str, group_slug: str | None) -> dict:
    group_id = find_group(conn, project_id, group_slug)["id"] if group_slug else None
    conn.execute(
        "update branches set group_id = ? where project_id = ? and name = ?",
        (group_id, project_id, name),
    )
    conn.commit()
    row = conn.execute(
        "select * from branches where project_id = ? and name = ?", (project_id, name)
    ).fetchone()
    if row is None:
        raise NotFound(f"no branch '{name}'")
    return dict(row)


def sync_branches(conn, project_id: int, ref_names: list[str]) -> None:
    """make sure every existing ref has a row, and drop rows for gone refs."""
    existing = {
        r["name"] for r in conn.execute("select name from branches where project_id = ?", (project_id,))
    }
    for name in ref_names:
        if name not in existing:
            conn.execute(
                "insert into branches (project_id, name, updated_date) values (?,?,?)",
                (project_id, name, utcnow()),
            )
    for name in existing - set(ref_names):
        conn.execute("delete from branches where project_id = ? and name = ?", (project_id, name))
    conn.commit()


def branch_groups_view(conn, project_id: int, visible_names: set[str] | None = None) -> list[dict]:
    """groups plus the branches inside them, for a listing page.

    branches the viewer may not see are simply absent, so the page cannot leak
    a branch name through an empty group title.
    """
    groups = list_groups(conn, project_id)
    rows = [
        dict(r)
        for r in conn.execute(
            "select * from branches where project_id = ? order by name", (project_id,)
        )
    ]
    seen_ids = set()
    out: list[dict] = []
    for group in groups:
        members = [
            b
            for b in rows
            if b["group_id"] == group["id"] and (visible_names is None or b["name"] in visible_names)
        ]
        out.append(
            {
                "slug": group["slug"],
                "title": group["title"],
                "description": group["description"],
                "visibility": group["visibility"],
                "semantics": json.loads(group["semantics"] or "{}"),
                "branches": members,
                "is_default": False,
            }
        )
        seen_ids.add(group["id"])
    loose = [
        b
        for b in rows
        if (b["group_id"] is None or b["group_id"] not in seen_ids)
        and (visible_names is None or b["name"] in visible_names)
    ]
    if loose:
        out.append(
            {
                "slug": "unfiled",
                "title": "unfiled",
                "description": "branches without a group",
                "visibility": "public",
                "semantics": {},
                "branches": loose,
                "is_default": True,
            }
        )
    return out


# ---------------------------------------------------------------- path rules


def add_path_rule(conn, project_id: int, pattern: str, scope_glob: str = "**", note: str = "") -> dict:
    pattern = (pattern or "").strip().strip("/")
    if not pattern:
        raise ForgeError("a path rule needs a pattern")
    if pattern.startswith("..") or "/../" in pattern:
        raise ForgeError("a path rule must stay inside the repository")
    scope_glob = (scope_glob or "**").strip() or "**"
    try:
        conn.execute(
            "insert into path_rules (project_id, pattern, scope_glob, note) values (?,?,?,?)",
            (project_id, pattern, scope_glob, note),
        )
    except Exception as exc:
        if "unique" in str(exc).lower():
            raise Conflict(f"rule '{pattern}' already exists for this project")
        raise
    conn.commit()
    return {
        "project_id": project_id,
        "pattern": pattern,
        "scope_glob": scope_glob,
        "note": note,
    }


def list_path_rules(conn, project_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "select id, pattern, scope_glob, note from path_rules where project_id = ? "
            "order by pattern",
            (project_id,),
        )
    ]


def remove_path_rule(conn, project_id: int, pattern: str) -> None:
    conn.execute(
        "delete from path_rules where project_id = ? and pattern = ?", (project_id, pattern)
    )
    conn.commit()


def rule_matches(pattern: str, scope_glob: str, branch: str) -> bool:
    return pathlib_.matches(branch, scope_glob)
