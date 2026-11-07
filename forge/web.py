"""the http surface: html pages, json, git smart http.

everything goes through one wsgi callable so the forge stays a single process
with no framework. git traffic is proxied straight to `git upload-pack` or
`git receive-pack`, pointed at either the real bare repo or a filtered view.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import urllib.parse
from http import HTTPStatus
from pathlib import Path

from . import access, branches, catalog, db, gitview, metatags, repos, urls
from . import paths as pathlib_
from .access import Viewer
from .repos import Conflict, Forbidden, ForgeError, NotFound

ACCEPT = "text/html,application/json;q=0.9"

CSS = """
:root {
  --bg: #121212;
  --panel: #181818;
  --line: #262626;
  --ink: #e8e8e8;
  --muted: #8b8b8b;
  --accent: #a8c8f0;
  --accent-wash: rgba(168, 200, 240, 0.14);
  --warn: #f0c8a8;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font-family: Poppins, ui-sans-serif, system-ui, -apple-system, sans-serif;
  font-size: 14px; line-height: 1.55;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
header.top {
  border-bottom: 1px solid var(--line); padding: 14px 24px;
  display: flex; gap: 18px; align-items: baseline; flex-wrap: wrap;
}
header.top strong { font-size: 15px; letter-spacing: 0.02em; }
header.top .who { margin-left: auto; color: var(--muted); }
main { padding: 24px; max-width: 1080px; }
h1 { font-size: 20px; font-weight: 600; margin: 0 0 6px; }
h2 { font-size: 14px; font-weight: 600; margin: 26px 0 10px; text-transform: lowercase; }
h3 { font-size: 13px; font-weight: 600; margin: 18px 0 8px; }
p.sub { color: var(--muted); margin: 0 0 18px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; }
.card + .card { margin-top: 8px; }
.row { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
.dim { color: var(--muted); }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.chip {
  display: inline-block; border: 1px solid var(--line); border-radius: 999px;
  padding: 1px 9px; font-size: 11.5px; color: var(--ink); background: transparent;
}
.chip.accent { border-color: var(--accent); color: var(--accent); background: var(--accent-wash); }
.chip.private { border-color: var(--warn); color: var(--warn); }
.chip.dep { border-style: dashed; }
ul.flat { list-style: none; padding: 0; margin: 0; }
ul.flat li { padding: 6px 0; border-bottom: 1px solid var(--line); }
ul.flat li:last-child { border-bottom: none; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 8px; }
.kv { display: grid; grid-template-columns: 160px 1fr; gap: 4px 14px; font-size: 12.5px; }
.kv dt { color: var(--muted); }
pre.box { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px; overflow-x: auto; }
table.files { width: 100%; border-collapse: collapse; font-size: 12.5px; }
table.files td { padding: 5px 8px; border-bottom: 1px solid var(--line); }
table.files td.size { text-align: right; color: var(--muted); width: 90px; }
footer { color: var(--muted); padding: 32px 24px; font-size: 12px; border-top: 1px solid var(--line); }
form.inline { display: inline; }
"""


def esc(value) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def chip(text: str, kind: str = "") -> str:
    klass = "chip" + (f" {kind}" if kind else "")
    return f'<span class="{klass}">{esc(text)}</span>'


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} b"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} kb"
    return f"{size / (1024 * 1024):.1f} mb"


class ForgeApp:
    def __init__(self, data_dir: str | Path, base_url: str = ""):
        self.data = Path(data_dir)
        self.data.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data / "forge.db"
        self.store = gitview.GitStore(self.data / "git")
        self.base_url = (base_url or "").rstrip("/")

    # ------------------------------------------------------------ wsgi

    def __call__(self, environ, start_response):
        conn = db.init(self.db_path)
        try:
            status, headers, body = self.handle(conn, environ)
        except NotFound as exc:
            status, headers, body = self.error_page(404, str(exc))
        except Forbidden as exc:
            status, headers, body = self.error_page(403, str(exc))
        except Conflict as exc:
            status, headers, body = self.error_page(409, str(exc))
        except ForgeError as exc:
            status, headers, body = self.error_page(exc.status, str(exc))
        except gitview.GitError as exc:
            status, headers, body = self.error_page(500, str(exc))
        finally:
            conn.close()
        start_response(status, headers)
        return [body] if isinstance(body, bytes) else body

    def handle(self, conn, environ):
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/") or "/"
        query = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
        viewer = self.viewer(conn, environ, query)
        route = urls.resolve(conn, path)
        kind = route["kind"]
        if kind == "health":
            return self.json_response({"ok": True})
        if kind == "api":
            return self.handle_api(conn, environ, viewer, route["rest"], method, query)
        if kind == "git":
            return self.handle_git(conn, environ, viewer, route, query)
        if kind == "explore_json":
            items = catalog.entries(conn, self.store, viewer, tag=self.param(query, "tag"))
            return self.json_response(
                {
                    "schema": "forge.catalog/v1",
                    "stats": catalog.stats(conn, self.store, viewer),
                    "projects": items,
                }
            )
        return self.handle_page(conn, environ, viewer, route, query)

    # ------------------------------------------------------------ helpers

    def param(self, query, key, default=None):
        values = query.get(key)
        return values[0] if values else default

    def viewer(self, conn, environ, query) -> Viewer:
        token = self.param(query, "access_token")
        if not token:
            header = environ.get("HTTP_AUTHORIZATION", "")
            if header.lower().startswith("bearer "):
                token = header[7:].strip()
        if not token:
            for part in environ.get("HTTP_COOKIE", "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "forge_token":
                    token = value
        return access.viewer_from_token(conn, token)

    def origin(self, environ) -> str:
        if self.base_url:
            return self.base_url
        scheme = environ.get("HTTP_X_FORWARDED_PROTO") or environ.get("wsgi.url_scheme", "http")
        host = environ.get("HTTP_HOST") or environ.get("SERVER_NAME", "localhost")
        return f"{scheme}://{host}"

    def json_response(self, payload, status=200, headers=None):
        body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
        head = [
            ("content-type", "application/json; charset=utf-8"),
            ("content-length", str(len(body))),
            ("cache-control", "no-store"),
        ]
        if headers:
            head.extend(headers)
        return f"{status} {HTTPStatus(status).phrase}", head, body

    def html_response(self, body: str, status=200, headers=None):
        raw = body.encode()
        head = [
            ("content-type", "text/html; charset=utf-8"),
            ("content-length", str(len(raw))),
        ]
        if headers:
            head.extend(headers)
        return f"{status} {HTTPStatus(status).phrase}", head, raw

    def text_response(self, body: bytes, content_type="text/plain; charset=utf-8", status=200):
        head = [("content-type", content_type), ("content-length", str(len(body)))]
        return f"{status} {HTTPStatus(status).phrase}", head, body

    def error_page(self, status: int, message: str):
        body = f"""<!doctype html><html><head><meta charset="utf-8"><title>{status}</title>
<style>{CSS}</style></head><body><header class="top"><strong><a href="/">gitadata forge</a></strong></header>
<main><h1>{status}</h1><p class="sub">{esc(message)}</p><p><a href="/">back to the repository list</a></p></main></body></html>"""
        return self.html_response(body, status=status)

    # ------------------------------------------------------------ pages

    def page_shell(self, viewer: Viewer, title: str, body: str) -> str:
        who = (
            f'<span class="who">{esc(viewer.login)}</span>'
            if viewer.authed
            else '<span class="who">anonymous reader</span>'
        )
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title><style>{CSS}</style></head>
<body>
<header class="top">
  <strong><a href="/">gitadata</a></strong>
  <a href="/">repositories</a>
  <a href="/explore.json">json</a>
  <a href="/api/catalog">api</a>
  {who}
</header>
<main>{body}</main>
<footer>a lightweight forge. private branches, private paths, promoted urls.</footer>
</body></html>"""

    def handle_page(self, conn, environ, viewer: Viewer, route, query):
        kind = route["kind"]
        if kind == "notfound":
            return self.error_page(404, "nothing lives at this path")
        if kind == "home":
            return self.html_response(self.page_shell(viewer, "repositories", self.render_home(conn, viewer, query)))
        if kind == "user":
            return self.html_response(
                self.page_shell(viewer, route["user"]["login"], self.render_user(conn, viewer, route["user"]))
            )
        if kind == "org":
            return self.html_response(
                self.page_shell(viewer, "org", self.render_org(conn, viewer, route["org"], route.get("alias")))
            )
        if kind == "manifest":
            project = route["project"]
            if not access.can_read(conn, viewer, project):
                return self.error_page(404, "nothing lives at this path")
            payload = metatags.manifest(
                conn, project, "", self.origin(environ), repos.project_path(conn, project)
            )
            return self.json_response(payload)
        if kind == "files":
            project = route["project"]
            if not access.can_read(conn, viewer, project):
                return self.error_page(404, "nothing lives at this path")
            return self.html_response(
                self.page_shell(viewer, route["rev"], self.render_files(conn, viewer, route))
            )
        if kind == "branches":
            project = route["project"]
            if not access.can_read(conn, viewer, project):
                return self.error_page(404, "nothing lives at this path")
            return self.html_response(
                self.page_shell(
                    viewer,
                    repos.project_path(conn, project),
                    self.render_project(conn, viewer, project, "", route.get("alias"), focus_group=route.get("group")),
                )
            )
        if kind == "project":
            project = route["project"]
            if not access.can_read(conn, viewer, project):
                return self.error_page(404, "nothing lives at this path")
            return self.html_response(
                self.page_shell(
                    viewer,
                    repos.project_path(conn, project),
                    self.render_project(conn, viewer, project, self.origin(environ), route.get("alias")),
                )
            )
        return self.error_page(404, "nothing lives at this path")

    def repo_card(self, conn, item: dict, show_owner: bool = False) -> str:
        path = item["path"]
        tags = " ".join(chip(t) for t in item["tags"])
        marks = []
        if item["visibility"] == "private":
            marks.append(chip("private repo", "private"))
        if item["private_branches"]:
            marks.append(chip(f"{len(item['private_branches'])} private branch", "private"))
        if item["private_path_count"]:
            marks.append(chip(f"{item['private_path_count']} private path", "private"))
        if item["alias"]:
            marks.append(chip("/@" + item["alias"], "accent"))
        return f"""<div class="card">
<div class="row"><a href="/{esc(path)}"><strong>{esc(path if show_owner else item['slug'])}</strong></a>
{' '.join(marks)}</div>
<div class="dim">{esc(item['description'] or 'no description')}</div>
<div class="row" style="margin-top:6px">{tags or '<span class="dim">no tags</span>'}</div>
</div>"""

    def render_home(self, conn, viewer: Viewer, query) -> str:
        tag = self.param(query, "tag")
        items = catalog.entries(conn, self.store, viewer, tag=tag)
        stats = catalog.stats(conn, self.store, viewer)
        owners = catalog.by_owner(items)
        promotions = repos.list_promotions(conn)
        dep_index = catalog.dependency_index(conn, viewer)
        rows = "".join(
            f'<li><a href="/{esc(item["path"])}">{esc(item["path"])}</a> '
            f'<span class="dim">{" ".join(chip(t) for t in item["tags"])}</span></li>'
            for item in items
        )
        tag_links = " ".join(
            f'<a class="chip" href="/?tag={esc(t)}">{esc(t)} ({n})</a>'
            for t, n in sorted(stats["tags"].items())
        )
        promoted = "".join(
            f'<li><a href="/@{esc(p["alias"])}">/@{esc(p["alias"])}</a> '
            f'<span class="dim">→ {esc(p["path"])}</span></li>'
            for p in promotions
        )
        deps = "".join(
            f'<li><a href="/{esc(d["path"])}">{esc(d["path"])}</a> <span class="dim">'
            + ", ".join(f"{esc(x['name'])} {esc(x['version'])}" for x in d["dependencies"])
            + "</span></li>"
            for d in dep_index
        )
        body = f"""
<h1>{stats['projects']} repositories across {stats['owners']} owners</h1>
<p class="sub">one list, every user and organisation. {'' if stats['multi_user'] else 'only one owner so far.'}</p>
<div class="row" style="margin-bottom:18px">{tag_links or '<span class="dim">no tags yet</span>'}</div>
<h2>all repositories</h2>
<ul class="flat">{rows or '<li class="dim">nothing published yet</li>'}</ul>
<h2>owners</h2>
<div class="grid">
{''.join(f'<div class="card"><a href="/{esc(o["owner_path"]) if "/" not in o["owner_path"] else "/o/" + esc(o["owner_path"])}">{esc(o["owner_path"])}</a> <span class="dim">{len(o["projects"])}</span></div>' for o in owners)}
</div>
<h2>promoted urls</h2>
<ul class="flat">{promoted or '<li class="dim">nothing promoted yet</li>'}</ul>
<h2>self hostable, and what they need</h2>
<ul class="flat">{deps or '<li class="dim">no project is tagged self-hostable yet</li>'}</ul>
"""
        return body

    def render_user(self, conn, viewer: Viewer, user: dict) -> str:
        login = user["login"]
        items = catalog.entries(conn, self.store, viewer, owner_path=login)
        groups = repos.grouped_projects(conn, login, items)
        folders_html = ""
        for group in groups:
            cards = "".join(self.repo_card(conn, item) for item in group["projects"])
            folders_html += f'<h2>{esc(group["title"])} <span class="dim">{len(group["projects"])}</span></h2><div class="grid">{cards}</div>'
        body = f"""
<h1>{esc(user['display_name'])}</h1>
<p class="sub mono">{esc(login)} · {len(items)} repositories · <a href="/explore.json">json</a></p>
<div class="row"><a class="chip accent" href="/u/{esc(login)}?view=folders">folder view</a>
<span class="dim">repositories are grouped into folders, and ungrouped ones fall into unsorted</span></div>
{folders_html or '<p class="dim">this user has no public repositories</p>'}
"""
        return body

    def render_org(self, conn, viewer: Viewer, org: dict, alias: str | None = None) -> str:
        path = repos.org_path(conn, org)
        children = repos.list_org_children(conn, org["id"])
        items = catalog.entries(conn, self.store, viewer, owner_path=path)
        groups = repos.grouped_projects(conn, path, items)
        suborgs = "".join(
            f'<li><a href="/o/{esc(path)}/{esc(child["slug"])}">{esc(child["display_name"])}</a> '
            f'<span class="dim">/o/{esc(path)}/{esc(child["slug"])}</span></li>'
            for child in children
        )
        folders_html = "".join(
            f'<h2>{esc(g["title"])}</h2><div class="grid">'
            + "".join(self.repo_card(conn, item) for item in g["projects"])
            + "</div>"
            for g in groups
        )
        alias_line = f'<p class="sub">promoted to <a href="/@{esc(alias)}">/@{esc(alias)}</a></p>' if alias else ""
        body = f"""
<h1>{esc(org['display_name'])}</h1>
<p class="sub mono">{esc(path)} · {len(children)} suborgs · {len(items)} repositories</p>
{alias_line}
<p>{esc(org['description'] or 'no description')}</p>
<h2>suborganisations</h2>
<ul class="flat">{suborgs or '<li class="dim">none, this org is a leaf</li>'}</ul>
{folders_html or '<p class="dim">no repositories here yet</p>'}
"""
        return body

    def render_project(
        self,
        conn,
        viewer: Viewer,
        project: dict,
        origin: str,
        alias: str | None = None,
        focus_group: str | None = None,
    ) -> str:
        path = repos.project_path(conn, project)
        role = access.project_role(conn, viewer, project)
        sees_paths = access.sees_private_paths(conn, viewer, project)
        real_refs = self.store.refs(project["id"])["branches"]
        branches.sync_branches(conn, project["id"], list(real_refs))
        visible = access.visible_branch_names(conn, viewer, project, real_refs)
        groups = branches.branch_groups_view(conn, project["id"], visible)
        rules = branches.list_path_rules(conn, project["id"])
        tags = repos.project_tags(conn, project["id"])
        deps = metatags.get_dependencies(conn, project["id"])
        clone = f"{origin}/{path}.git" if origin else f"/{path}.git"
        clone_alias = f"{origin}/@{alias}" if (origin and alias) else (f"/@{alias}" if alias else None)

        branch_html = ""
        for group in groups:
            chips = " ".join(chip(f"{k}={v}") for k, v in group["semantics"].items())
            if group["visibility"] == "private":
                chips = (chips + " " + chip("private group", "private")).strip()
            if focus_group and group["slug"] != focus_group:
                continue
            rows = []
            for branch in group["branches"]:
                marks = []
                if access.branch_is_private(conn, branch):
                    marks.append(chip("private", "private"))
                if branch["protected"]:
                    marks.append(chip("protected"))
                own = json.loads(branch["semantics"] or "{}")
                marks.extend(chip(f"{k}={v}") for k, v in own.items())
                rows.append(
                    f'<li><a class="mono" href="/{esc(path)}/tree/{esc(branch["name"])}">{esc(branch["name"])}</a> '
                    f'{" ".join(marks)}</li>'
                )
            branch_html += (
                f'<h3>{esc(group["title"])} <span class="dim mono">/{esc(group["slug"])}</span> {chips}</h3>'
                f'<ul class="flat">{"".join(rows) or "<li class=dim>no branches</li>"}</ul>'
            )
        rule_rows = "".join(
            f'<li><span class="mono">{esc(r["pattern"])}</span> <span class="dim">scoped to '
            f'<span class="mono">{esc(r["scope_glob"])}</span>{" · " + esc(r["note"]) if r["note"] else ""}</span></li>'
            for r in rules
        )
        privacy_note = (
            rule_rows
            if sees_paths
            else (
                f'<p class="dim">{len(rules)} path rule(s) are stripped from what you can fetch here.</p>'
                if rules
                else ""
            )
        )
        dep_rows = "".join(
            f'<li><span class="mono">{esc(d["name"])}</span> {chip(d["kind"], "dep")} '
            f'<span class="dim">{esc(d["version"])} · <a href="{esc(d["url"])}">{esc(d["url"])}</a></span></li>'
            for d in deps
        )
        tree = self.store.tree_at(project["id"], project["default_branch"])
        if tree:
            patterns = access.private_patterns(conn, project["id"], project["default_branch"])
            if not sees_paths and patterns:
                from . import paths as pathlib_

                tree = [e for e in tree if not pathlib_.matches_any(e["path"], patterns)]
            files = "".join(
                f'<tr><td><a class="mono" href="/{esc(path)}/blob/{esc(project["default_branch"])}/{urllib.parse.quote(e["path"])}">'
                f'{esc(e["path"])}</a></td><td class="size">{human_size(e["size"])}</td></tr>'
                for e in tree[:200]
            )
        else:
            files = ""
        body = f"""
<h1>{esc(path)}</h1>
<p class="sub">{esc(project['description'] or 'no description')}
· {chip(project['visibility'] + ' repo')} {chip('default ' + project['default_branch'])}
{' '.join(chip(t, 'accent') for t in tags)}</p>
<div class="grid">
  <div class="card">
    <h3>clone</h3>
    <pre class="box">git clone {esc(clone)}</pre>
    {f'<pre class="box">git clone {esc(clone_alias)}</pre>' if clone_alias else ''}
    <div class="dim">pushes need the maintainer role. filtered clones are read only.</div>
  </div>
  <div class="card">
    <h3>you</h3>
    <div class="kv">
      <dt>role</dt><dd>{esc(role or 'anonymous')}</dd>
      <dt>sees private branches</dt><dd>{'yes' if access.sees_private_branches(conn, viewer, project) else 'no'}</dd>
      <dt>sees private paths</dt><dd>{'yes' if sees_paths else 'no'}</dd>
      <dt>manifest</dt><dd><a href="/{esc(path)}/manifest.json">manifest.json</a></dd>
    </div>
  </div>
</div>
<h2>branch groups</h2>
{branch_html or '<p class="dim">no branches yet</p>'}
<h2>path privacy</h2>
<ul class="flat">{privacy_note or '<li class="dim">every path is public on every branch</li>'}</ul>
<h2>metadata</h2>
<div class="row">{' '.join(chip(t, 'accent') for t in tags) or '<span class="dim">untagged</span>'}</div>
<h3>dependencies</h3>
<ul class="flat">{dep_rows or '<li class="dim">none declared</li>'}</ul>
<h2>files on {esc(project['default_branch'])}</h2>
<table class="files">{files or '<tr><td class="dim">empty repository</td></tr>'}</table>
"""
        return body

    def render_files(self, conn, viewer: Viewer, route) -> str:
        project = route["project"]
        rev = route["rev"]
        path = repos.project_path(conn, project)
        sees_paths = access.sees_private_paths(conn, viewer, project)
        patterns = access.private_patterns(conn, project["id"], rev)
        if route["mode"] == "tree":
            entries = self.store.tree_at(project["id"], rev)
            if not sees_paths and patterns:
                from . import paths as pathlib_

                entries = [e for e in entries if not pathlib_.matches_any(e["path"], patterns)]
            rows = "".join(
                f'<tr><td><a class="mono" href="/{esc(path)}/blob/{esc(rev)}/{urllib.parse.quote(e["path"])}">'
                f'{esc(e["path"])}</a></td><td class="size">{human_size(e["size"])}</td></tr>'
                for e in entries
            )
            return f"""<h1 class="mono">{esc(path)} @ {esc(rev)}</h1>
<p class="sub"><a href="/{esc(path)}">back to the project</a></p>
<table class="files">{rows or '<tr><td class="dim">empty tree</td></tr>'}</table>"""
        file_path = route["file_path"]
        if not sees_paths and pathlib_.matches_any(file_path, patterns):
            raise NotFound("this path is private")
        try:
            blob = gitview.git(
                ["-C", str(self.store.repo_dir(project["id"])), "show", f"{rev}:{file_path}"]
            )
        except gitview.GitError:
            raise NotFound("no such file")
        if len(blob) > 200_000 or b"\x00" in blob[:4096]:
            return f'<h1 class="mono">{esc(file_path)}</h1><p class="dim">binary or large file, {human_size(len(blob))}</p>'
        return (
            f'<h1 class="mono">{esc(file_path)}</h1><p class="sub"><a href="/{esc(path)}">back</a></p>'
            f'<pre class="box">{esc(blob.decode("utf-8", "replace"))}</pre>'
        )

    # ------------------------------------------------------------ git

    def repo_for(self, conn, viewer: Viewer, project: dict, service: str) -> Path:
        """the real repo when nothing has to be hidden, a filtered view otherwise."""
        if service == "git-receive-pack":
            if not access.can_push(conn, viewer, project):
                raise Forbidden("pushes need the maintainer role")
            return self.store.ensure_repo(project["id"])
        real = self.store.refs(project["id"])
        visible = access.visible_branch_names(conn, viewer, project, real["branches"])
        kept = {name: real["branches"][name] for name in visible}
        strips = not access.sees_private_paths(conn, viewer, project)
        patterns = {
            name: (access.private_patterns(conn, project["id"], name) if strips else [])
            for name in kept
        }
        filtered = any(patterns.values()) or set(kept) != set(real["branches"])
        default = project["default_branch"]
        if default not in visible:
            default = sorted(visible)[0] if visible else None
        if not filtered:
            repo = self.store.ensure_repo(project["id"])
            self.store.set_head(repo, default)
            return repo
        tags: dict[str, str] = {}
        if not any(patterns.values()):
            heads = set(kept.values())
            tags = {name: sha for name, sha in real["tags"].items() if sha in heads}
        view = self.store.view_for(
            project["id"], {name: (sha, patterns[name]) for name, sha in kept.items()}, tags
        )
        self.store.set_head(view, default)
        return view

    def handle_git(self, conn, environ, viewer: Viewer, route, query):
        project = route["project"]
        if not access.can_read(conn, viewer, project):
            return self.error_page(404, "nothing lives at this path")
        service = route.get("service")
        protocol = environ.get("HTTP_GIT_PROTOCOL")
        if service == "info/refs":
            wanted = self.param(query, "service", "")
            if wanted not in ("git-upload-pack", "git-receive-pack"):
                return self.error_page(404, "only smart http is served here")
            if wanted == "git-receive-pack" and not access.can_push(conn, viewer, project):
                return self.error_page(403, "pushes need the maintainer role")
            repo = self.repo_for(conn, viewer, project, wanted)
            payload = gitview.advertise_refs(repo, wanted, protocol)
            return self.text_response(
                payload, gitview.service_content_type(wanted, "advertise")
            )
        if service in ("git-upload-pack", "git-receive-pack"):
            length = int(environ.get("CONTENT_LENGTH") or 0)
            body = environ["wsgi.input"].read(length) if length else b""
            repo = self.repo_for(conn, viewer, project, service)
            payload = gitview.rpc(repo, service, body, protocol)
            if service == "git-receive-pack":
                branches.sync_branches(conn, project["id"], self.store.branch_names(project["id"]))
            return self.text_response(payload, gitview.service_content_type(service, "result"))
        return self.error_page(404, "nothing lives at this path")

    # ------------------------------------------------------------ api

    def require_authed(self, viewer: Viewer):
        if not viewer.authed:
            raise Forbidden("this endpoint needs a token")

    def require_maintainer(self, conn, viewer: Viewer, project: dict):
        self.require_authed(viewer)
        role = access.project_role(conn, viewer, project)
        if role not in ("maintainer", "owner"):
            raise Forbidden(f"maintainer role needed, you are {role or 'not a member'}")

    def read_body(self, environ) -> dict:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        raw = environ["wsgi.input"].read(length) if length else b""
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ForgeError(f"body is not json: {exc.msg}")
        if not isinstance(payload, dict):
            raise ForgeError("body must be a json object")
        return payload

    def handle_api(self, conn, environ, viewer: Viewer, rest: list[str], method: str, query):
        payload = self.read_body(environ) if method in ("POST", "PATCH", "PUT", "DELETE") else {}
        if rest == ["catalog"]:
            return self.json_response(
                {
                    "stats": catalog.stats(conn, self.store, viewer),
                    "projects": catalog.entries(conn, self.store, viewer),
                }
            )
        if rest == ["users"] and method == "POST":
            if repos.list_users(conn) and not (viewer.is_admin):
                self.require_authed(viewer)
                raise Forbidden("only an admin can add users once the forge has any")
            user = repos.create_user(
                conn,
                payload.get("login", ""),
                payload.get("display_name", ""),
                payload.get("email", ""),
                bool(payload.get("is_admin")),
            )
            token = secrets.token_urlsafe(24)
            repos.set_token(conn, user["id"], access.hash_token(token))
            return self.json_response({"user": {"login": user["login"], "id": user["id"]}, "token": token}, 201)
        if rest == ["orgs"] and method == "POST":
            self.require_authed(viewer)
            org = repos.create_org(
                conn,
                payload.get("slug", ""),
                payload.get("parent", ""),
                payload.get("display_name", ""),
                payload.get("description", ""),
            )
            conn.execute(
                "insert or replace into org_members (org_id, user_id, role) values (?,?,'owner')",
                (org["id"], viewer.user_id),
            )
            conn.commit()
            return self.json_response({"org": org, "path": repos.org_path(conn, org)}, 201)
        if rest == ["projects"] and method == "POST":
            self.require_authed(viewer)
            owner = payload.get("owner", "")
            if owner != viewer.login and not viewer.is_admin:
                try:
                    org = repos.resolve_org(conn, owner)
                except NotFound:
                    raise Forbidden("you can create projects under your own login or an org you own")
                role = conn.execute(
                    "select role from org_members where org_id = ? and user_id = ?",
                    (org["id"], viewer.user_id),
                ).fetchone()
                if role is None:
                    raise Forbidden(f"you are not a member of '{owner}'")
            project = repos.create_project(
                conn,
                owner,
                payload.get("slug", ""),
                payload.get("description", ""),
                payload.get("visibility", "public"),
                payload.get("default_branch", "main"),
            )
            self.store.ensure_repo(project["id"])
            return self.json_response(
                {"project": project, "path": repos.project_path(conn, project)}, 201
            )
        if rest == ["promote"] and method == "POST":
            self.require_authed(viewer)
            alias = payload.get("alias", "")
            if payload.get("org"):
                org = repos.resolve_org(conn, payload["org"])
                entry = repos.promote(conn, org_id=org["id"], alias=alias)
            else:
                project = repos.find_project(
                    conn, payload.get("owner", ""), payload.get("project", "")
                )
                self.require_maintainer(conn, viewer, project)
                entry = repos.promote(conn, project_id=project["id"], alias=alias)
            return self.json_response({"promotion": entry, "url": f"/@{alias}"}, 201)
        if rest == ["dependencies"]:
            items = catalog.dependency_index(conn, viewer)
            return self.json_response({"schema": "forge.dependencies/v1", "projects": items})
        if len(rest) >= 2 and rest[0] == "projects":
            project, tail = self.project_and_tail(conn, rest[1:])
            if tail == ["manifest"]:
                payload_out = metatags.manifest(
                    conn,
                    project,
                    "",
                    self.origin(environ),
                    repos.project_path(conn, project),
                )
                return self.json_response(payload_out)
            self.require_maintainer(conn, viewer, project)
            if tail == ["tags"] and method in ("POST", "PUT"):
                if isinstance(payload.get("tags"), list):
                    tags = metatags.set_tags(conn, project["id"], payload["tags"])
                elif method == "POST" and payload.get("tag"):
                    tags = metatags.add_tag(conn, project["id"], payload["tag"])
                else:
                    tags = metatags.remove_tag(conn, project["id"], payload.get("tag", ""))
                return self.json_response({"tags": tags})
            if tail == ["dependencies"] and method == "POST":
                deps = metatags.add_dependency(
                    conn,
                    project["id"],
                    payload.get("name", ""),
                    payload.get("kind", "runtime"),
                    payload.get("version", "any"),
                    payload.get("url", ""),
                    bool(payload.get("required", True)),
                    payload.get("note", ""),
                )
                return self.json_response({"dependencies": deps}, 201)
            if tail == ["branch-groups"] and method == "POST":
                group = branches.create_group(
                    conn,
                    project["id"],
                    payload.get("slug", ""),
                    payload.get("title", ""),
                    payload.get("semantics"),
                    payload.get("visibility", "public"),
                    int(payload.get("position", 0)),
                )
                return self.json_response({"group": group}, 201)
            if tail == ["branches"] and method == "POST":
                branch = branches.ensure_branch(
                    conn,
                    project["id"],
                    payload.get("name", ""),
                    branches.find_group(conn, project["id"], payload["group"])["id"]
                    if payload.get("group")
                    else None,
                    payload.get("visibility", "inherit"),
                    payload.get("semantics"),
                    payload.get("protected"),
                )
                return self.json_response({"branch": branch}, 201)
            if tail == ["path-rules"] and method == "POST":
                rule = branches.add_path_rule(
                    conn,
                    project["id"],
                    payload.get("pattern", ""),
                    payload.get("scope", "**"),
                    payload.get("note", ""),
                )
                return self.json_response({"rule": rule}, 201)
            if tail == ["path-rules"] and method == "DELETE":
                branches.remove_path_rule(conn, project["id"], payload.get("pattern", ""))
                return self.json_response({"rules": branches.list_path_rules(conn, project["id"])})
            if tail == ["folders"] and method == "POST":
                folder = repos.create_folder(
                    conn,
                    repos.project_path(conn, project).rsplit("/", 1)[0],
                    payload.get("slug", ""),
                    payload.get("title", ""),
                    int(payload.get("position", 0)),
                )
                repos.add_to_folder(conn, folder["id"], project["id"])
                return self.json_response({"folder": folder}, 201)
            raise NotFound(f"no api route /api/{'/'.join(rest)}")
        raise NotFound(f"no api route /api/{'/'.join(rest)}")

    def project_and_tail(self, conn, segments: list[str]) -> tuple[dict, list[str]]:
        """split ['anne','notes','path-rules'] into (project, ['path-rules'])."""
        for cut in range(len(segments), 1, -1):
            try:
                project = self.project_from_api(conn, "/".join(segments[:cut]))
            except NotFound:
                continue
            return project, segments[cut:]
        raise NotFound(f"no project at /api/projects/{'/'.join(segments)}")

    def project_from_api(self, conn, path: str) -> dict:
        segments = [s for s in path.strip("/").split("/") if s]
        if len(segments) < 2:
            raise NotFound(f"'{path}' is not an owner/project path")
        return repos.find_project(conn, "/".join(segments[:-1]), segments[-1])


def make_app(data_dir: str | Path, base_url: str = "") -> ForgeApp:
    return ForgeApp(data_dir, base_url)
