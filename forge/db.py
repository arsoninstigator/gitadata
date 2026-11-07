"""sqlite schema for the forge.

one file, no migrations framework: the schema is created on demand and the
shape is small enough to read in one go. every write goes through the helper
functions in the other modules, never through raw sql in the http layer.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
pragma journal_mode = wal;
pragma foreign_keys = on;

create table if not exists users (
    id integer primary key,
    login text not null unique,
    display_name text not null default '',
    email text,
    token_hash text,
    is_admin integer not null default 0,
    created_date text not null
);

create table if not exists orgs (
    id integer primary key,
    slug text not null,
    parent_id integer references orgs(id) on delete cascade,
    display_name text not null default '',
    description text not null default '',
    created_date text not null,
    unique (parent_id, slug)
);
create unique index if not exists orgs_root_slug on orgs(slug) where parent_id is null;

create table if not exists projects (
    id integer primary key,
    slug text not null,
    owner_user_id integer references users(id) on delete cascade,
    owner_org_id integer references orgs(id) on delete cascade,
    description text not null default '',
    visibility text not null default 'public',
    default_branch text not null default 'main',
    created_date text not null
);
create unique index if not exists projects_by_user
    on projects(owner_user_id, slug) where owner_user_id is not null;
create unique index if not exists projects_by_org
    on projects(owner_org_id, slug) where owner_org_id is not null;

create table if not exists project_members (
    project_id integer not null references projects(id) on delete cascade,
    user_id integer not null references users(id) on delete cascade,
    role text not null,
    primary key (project_id, user_id)
);

create table if not exists org_members (
    org_id integer not null references orgs(id) on delete cascade,
    user_id integer not null references users(id) on delete cascade,
    role text not null,
    primary key (org_id, user_id)
);

create table if not exists folders (
    id integer primary key,
    slug text not null,
    title text not null default '',
    owner_user_id integer references users(id) on delete cascade,
    owner_org_id integer references orgs(id) on delete cascade,
    position integer not null default 0
);
create unique index if not exists folders_by_user
    on folders(owner_user_id, slug) where owner_user_id is not null;
create unique index if not exists folders_by_org
    on folders(owner_org_id, slug) where owner_org_id is not null;

create table if not exists folder_items (
    folder_id integer not null references folders(id) on delete cascade,
    project_id integer not null references projects(id) on delete cascade,
    position integer not null default 0,
    primary key (folder_id, project_id)
);

create table if not exists branch_groups (
    id integer primary key,
    project_id integer not null references projects(id) on delete cascade,
    slug text not null,
    title text not null default '',
    description text not null default '',
    semantics text not null default '{}',
    visibility text not null default 'public',
    position integer not null default 0
);
create unique index if not exists branch_groups_slug on branch_groups(project_id, slug);

create table if not exists branches (
    id integer primary key,
    project_id integer not null references projects(id) on delete cascade,
    name text not null,
    group_id integer references branch_groups(id) on delete set null,
    visibility text not null default 'inherit',
    semantics text not null default '{}',
    protected integer not null default 0,
    updated_date text
);
create unique index if not exists branches_name on branches(project_id, name);

create table if not exists path_rules (
    id integer primary key,
    project_id integer not null references projects(id) on delete cascade,
    pattern text not null,
    scope_glob text not null default '**',
    note text not null default ''
);
create unique index if not exists path_rules_pattern on path_rules(project_id, pattern, scope_glob);

create table if not exists project_tags (
    project_id integer not null references projects(id) on delete cascade,
    tag text not null,
    source text not null default 'manual',
    primary key (project_id, tag)
);

create table if not exists dependencies (
    id integer primary key,
    project_id integer not null references projects(id) on delete cascade,
    name text not null,
    kind text not null default 'runtime',
    version text not null default '',
    url text not null default '',
    required integer not null default 1,
    note text not null default ''
);

create table if not exists promotions (
    id integer primary key,
    alias text not null unique,
    kind text not null default 'project',
    project_id integer references projects(id) on delete cascade,
    org_id integer references orgs(id) on delete cascade,
    created_date text not null
);
"""


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    return conn


def init(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def row_to_dict(row) -> dict | None:
    return dict(row) if row is not None else None


def rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]
