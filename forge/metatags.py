"""metadata tags and machine readable dependencies.

tags are a closed vocabulary on purpose: `self-hostable` means something only
if every project carrying it says what it needs to run. so tagging a project
self-hostable forces a dependency list, every entry needs a kind and a url, and
the same list is published as json at /<owner>/<project>/manifest.json. a
reader (or a bot) can then answer "what do i need to run this" without asking a
human.
"""

from __future__ import annotations

import json

from .db import utcnow
from .repos import ForgeError, TAG_VOCAB

DEP_KINDS = {
    "runtime": "language runtime or interpreter",
    "service": "external service or daemon",
    "build": "tool needed to build",
    "data": "dataset or volume",
    "package": "library or package manager entry",
}

TAG_RULES: dict[str, dict] = {
    "self-hostable": {
        "requires_dependencies": 1,
        "summary": "every dependency must declare a kind and a machine readable url",
    },
    "dependency-light": {"summary": "descriptive only"},
    "readonly-mirror": {"forbids_path_privacy": False},
}


def _dep_error(deps: list[dict]) -> str | None:
    for dep in deps:
        if not (dep.get("name") or "").strip():
            return "every dependency needs a name"
        if dep.get("kind") not in DEP_KINDS:
            return f"dependency '{dep.get('name')}' needs a kind from {sorted(DEP_KINDS)}"
        if not (dep.get("url") or "").strip():
            return f"dependency '{dep.get('name')}' needs a url so the manifest is machine readable"
        if not (dep.get("version") or "").strip():
            return f"dependency '{dep.get('name')}' needs a version (use 'any' if unpinned)"
    return None


def validate(conn, project_id: int, tags: list[str] | None = None) -> list[str]:
    """raise unless the tag set is consistent with the declared dependencies."""
    tags = tags if tags is not None else [
        r["tag"]
        for r in conn.execute(
            "select tag from project_tags where project_id = ? order by tag", (project_id,)
        )
    ]
    unknown = [t for t in tags if t not in TAG_VOCAB]
    if unknown:
        raise ForgeError(
            f"unknown tag(s): {', '.join(unknown)}. vocabulary: {', '.join(sorted(TAG_VOCAB))}"
        )
    deps = [
        dict(r)
        for r in conn.execute(
            "select name, kind, version, url, required, note from dependencies where project_id = ?",
            (project_id,),
        )
    ]
    if "self-hostable" in tags:
        rule = TAG_RULES["self-hostable"]
        if len(deps) < rule["requires_dependencies"]:
            raise ForgeError(
                "self-hostable needs at least one declared dependency "
                "(git, python, postgres ...). declare them, then tag it."
            )
        problem = _dep_error(deps)
        if problem:
            raise ForgeError(f"self-hostable: {problem}")
    return tags


def set_tags(conn, project_id: int, tags: list[str]) -> list[str]:
    tags = sorted({t.strip().lower() for t in tags if t and t.strip()})
    validate(conn, project_id, tags)
    conn.execute("delete from project_tags where project_id = ? and source = 'manual'", (project_id,))
    for tag in tags:
        conn.execute(
            "insert or ignore into project_tags (project_id, tag, source) values (?,?,'manual')",
            (project_id, tag),
        )
    conn.commit()
    return get_tags(conn, project_id)


def add_tag(conn, project_id: int, tag: str) -> list[str]:
    tag = (tag or "").strip().lower()
    if tag not in TAG_VOCAB:
        raise ForgeError(f"unknown tag '{tag}'. vocabulary: {', '.join(sorted(TAG_VOCAB))}")
    existing = get_tags(conn, project_id)
    if tag in existing:
        return existing
    validate(conn, project_id, sorted(existing + [tag]))
    conn.execute(
        "insert or ignore into project_tags (project_id, tag, source) values (?,?,'manual')",
        (project_id, tag),
    )
    conn.commit()
    return get_tags(conn, project_id)


def remove_tag(conn, project_id: int, tag: str) -> list[str]:
    conn.execute(
        "delete from project_tags where project_id = ? and tag = ? and source = 'manual'",
        (project_id, tag),
    )
    conn.commit()
    return get_tags(conn, project_id)


def get_tags(conn, project_id: int) -> list[str]:
    return [
        r["tag"]
        for r in conn.execute(
            "select tag from project_tags where project_id = ? order by tag", (project_id,)
        )
    ]


def add_dependency(
    conn,
    project_id: int,
    name: str,
    kind: str = "runtime",
    version: str = "any",
    url: str = "",
    required: bool = True,
    note: str = "",
) -> list[dict]:
    if kind not in DEP_KINDS:
        raise ForgeError(f"dependency kind must be one of {sorted(DEP_KINDS)}")
    conn.execute(
        "insert into dependencies (project_id, name, kind, version, url, required, note) "
        "values (?,?,?,?,?,?,?)",
        (project_id, name, kind, version, url, 1 if required else 0, note),
    )
    conn.commit()
    return get_dependencies(conn, project_id)


def remove_dependency(conn, project_id: int, name: str) -> list[dict]:
    conn.execute("delete from dependencies where project_id = ? and name = ?", (project_id, name))
    conn.commit()
    return get_dependencies(conn, project_id)


def get_dependencies(conn, project_id: int) -> list[dict]:
    return [
        {
            **dict(r),
            "required": bool(r["required"]),
            "kind_description": DEP_KINDS.get(r["kind"], ""),
        }
        for r in conn.execute(
            "select name, kind, version, url, required, note from dependencies "
            "where project_id = ? order by kind, name",
            (project_id,),
        )
    ]


def manifest(conn, project: dict, path: str, base_url: str = "", project_path: str = "") -> dict:
    """the machine readable view of one project.

    deliberately flat and stable: this is what other tools read, so keys do not
    move once published.
    """
    from . import access, branches, repos

    base = base_url.rstrip("/")
    project_path = project_path or repos.project_path(conn, project)
    tags = get_tags(conn, project["id"])
    deps = get_dependencies(conn, project["id"])
    groups = branches.branch_groups_view(conn, project["id"])
    rules = branches.list_path_rules(conn, project["id"])
    promoted = conn.execute(
        "select alias from promotions where kind = 'project' and project_id = ?",
        (project["id"],),
    ).fetchone()
    owner = access.project_owner(conn, project)
    return {
        "schema": "forge.manifest/v1",
        "generated": utcnow(),
        "project": {
            "path": project_path,
            "slug": project["slug"],
            "description": project["description"],
            "visibility": project["visibility"],
            "default_branch": project["default_branch"],
            "owner": {"kind": owner["kind"], "slug": owner["slug"], "name": owner["name"]},
        },
        "urls": {
            "web": f"{base}/{project_path}" if base else f"/{project_path}",
            "clone": f"{base}/{project_path}.git" if base else f"/{project_path}.git",
            "manifest": f"{base}/{project_path}/manifest.json"
            if base
            else f"/{project_path}/manifest.json",
            "promoted": f"@/{promoted['alias']}" if promoted else None,
        },
        "tags": [
            {"tag": tag, "description": TAG_VOCAB.get(tag, "")} for tag in tags
        ],
        "self_hostable": "self-hostable" in tags,
        "dependencies": [
            {
                "name": d["name"],
                "kind": d["kind"],
                "version": d["version"],
                "url": d["url"],
                "required": d["required"],
            }
            for d in deps
        ],
        "branches": {
            "groups": [
                {
                    "slug": g["slug"],
                    "title": g["title"],
                    "visibility": g["visibility"],
                    "semantics": g["semantics"],
                    "branches": [
                        {
                            "name": b["name"],
                            "visibility": b["visibility"],
                            "semantics": b["semantics"] and json.loads(b["semantics"] or "{}"),
                        }
                        for b in g["branches"]
                    ],
                }
                for g in groups
            ],
            "private_branches": [
                b["name"]
                for b in access.list_branches(conn, project["id"])
                if access.branch_is_private(conn, b)
            ],
        },
        "private_paths": [
            {"pattern": r["pattern"], "scope": r["scope_glob"], "note": r["note"]} for r in rules
        ],
    }


def manifest_json(conn, project: dict, base_url: str = "") -> str:
    return json.dumps(
        manifest(conn, project, "", base_url), indent=2, sort_keys=True
    ) + "\n"
