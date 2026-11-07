"""the cross-user repository list.

the default listing is not scoped to one namespace: every project a viewer may
read shows up, whichever user or org owns it, with the owner path in front. an
org page or a user page is that same list filtered down.
"""

from __future__ import annotations

from . import access, branches, metatags, repos


def entry(conn, store, viewer: access.Viewer, project: dict) -> dict:
    owner = access.project_owner(conn, project)
    owner_path = repos.project_path(conn, project).rsplit("/", 1)[0]
    promoted = conn.execute(
        "select alias from promotions where kind = 'project' and project_id = ?",
        (project["id"],),
    ).fetchone()
    rules = branches.list_path_rules(conn, project["id"])
    private_branches = [
        b["name"] for b in access.list_branches(conn, project["id"]) if access.branch_is_private(conn, b)
    ]
    return {
        "id": project["id"],
        "path": repos.project_path(conn, project),
        "owner_path": owner_path,
        "owner": owner,
        "slug": project["slug"],
        "description": project["description"],
        "visibility": project["visibility"],
        "default_branch": project["default_branch"],
        "tags": repos.project_tags(conn, project["id"]),
        "folder": repos.folder_of(conn, project["id"]),
        "alias": promoted["alias"] if promoted else None,
        "private_branches": private_branches,
        "private_path_count": len(rules),
        "last_activity": store.last_activity(project["id"]) if store else None,
        "branch_count": len(store.refs(project["id"])["branches"]) if store else 0,
        "role": access.project_role(conn, viewer, project),
    }


def entries(
    conn,
    store,
    viewer: access.Viewer,
    tag: str | None = None,
    owner_path: str | None = None,
) -> list[dict]:
    out = []
    for project in repos.list_all_projects(conn):
        if not access.can_read(conn, viewer, project):
            continue
        if owner_path:
            parent = repos.project_path(conn, project).rsplit("/", 1)[0]
            if parent != owner_path and not parent.startswith(owner_path + "/"):
                continue
        item = entry(conn, store, viewer, project)
        if tag and tag not in item["tags"]:
            continue
        out.append(item)
    out.sort(key=lambda e: (e["last_activity"] or "", e["path"]), reverse=True)
    return out


def stats(conn, store, viewer: access.Viewer) -> dict:
    items = entries(conn, store, viewer)
    owners = {e["owner_path"] for e in items}
    tag_counts: dict[str, int] = {}
    for item in items:
        for tag in item["tags"]:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    return {
        "projects": len(items),
        "owners": len(owners),
        "orgs": len({o for o in owners if "/" in o or True}),
        "tags": tag_counts,
        "multi_user": len(owners) > 1,
    }


def by_owner(items: list[dict]) -> list[dict]:
    buckets: dict[str, list[dict]] = {}
    for item in items:
        buckets.setdefault(item["owner_path"], []).append(item)
    return [
        {"owner_path": owner, "projects": sorted(projects, key=lambda p: p["slug"])}
        for owner, projects in sorted(buckets.items())
    ]


def dependency_index(conn, viewer: access.Viewer) -> list[dict]:
    """which projects need what, for the 'self-hostable' tag to be useful."""
    out = []
    for project in repos.list_all_projects(conn):
        if not access.can_read(conn, viewer, project):
            continue
        tags = repos.project_tags(conn, project["id"])
        if "self-hostable" not in tags:
            continue
        out.append(
            {
                "path": repos.project_path(conn, project),
                "dependencies": metatags.get_dependencies(conn, project["id"]),
            }
        )
    return out
