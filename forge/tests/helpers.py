"""test fixture: a forge in a temp dir, a scratch git repo, and a wsgi client."""

from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from .. import access, branches, db, gitview, repos
from ..web import ForgeApp

SAFE = ["-c", "safe.directory=*"]


def run(args, cwd=None, env=None, check=True):
    proc = subprocess.run(
        ["git", *SAFE, *args],
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {proc.stderr.decode('utf-8', 'replace')}"
        )
    if not check:
        return (
            proc.stdout.decode("utf-8", "replace")
            + proc.stderr.decode("utf-8", "replace")
            + f"\n[exit {proc.returncode}]"
        )
    return proc.stdout.decode("utf-8", "replace")


class Forge:
    def __init__(self, tmpdir: str):
        self.dir = Path(tmpdir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.conn = db.init(self.dir / "forge.db")
        self.app = ForgeApp(self.dir)
        self.store = self.app.store
        self._work = self.dir / "work"
        self._counter = 0

    # ------------------------------------------------------------ seeding

    def user(self, login: str, admin: bool = False) -> dict:
        user = repos.create_user(self.conn, login, login, f"{login}@example.test", admin)
        token = secrets.token_urlsafe(18)
        repos.set_token(self.conn, user["id"], access.hash_token(token))
        user["token"] = token
        return user

    def org(self, slug: str, parent: str = "") -> dict:
        return repos.create_org(self.conn, slug, parent, slug, "test org")

    def project(self, owner: str, slug: str, **kwargs) -> dict:
        project = repos.create_project(self.conn, owner, slug, kwargs.get("description", ""), **{k: v for k, v in kwargs.items() if k != "description"})
        self.store.ensure_repo(project["id"])
        return project

    def push(self, project: dict, files: dict[str, str], branch: str = "main", message: str = "commit") -> str:
        """commit `files` in a scratch repo and push them to the forge."""
        self._counter += 1
        work = self._work / f"w{self._counter}"
        work.mkdir(parents=True, exist_ok=True)
        run(["init", "-q", "-b", "main", "."], cwd=work)
        run(["config", "user.name", "anne"], cwd=work)
        run(["config", "user.email", "anne@example.test"], cwd=work)
        for name, content in files.items():
            target = work / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        run(["add", "-A"], cwd=work)
        env = dict(os.environ)
        env.update(
            {
                "GIT_AUTHOR_DATE": "2026-01-02T03:04:05+0000",
                "GIT_COMMITTER_DATE": "2026-01-02T03:04:05+0000",
            }
        )
        run(["commit", "-q", "-m", message], cwd=work, env=env)
        bare = str(self.store.repo_dir(project["id"]))
        run(["remote", "add", "forge", bare], cwd=work)
        run(["push", "-q", "forge", f"HEAD:refs/heads/{branch}"], cwd=work)
        sha = run(["rev-parse", "HEAD"], cwd=work).strip()
        branches.sync_branches(self.conn, project["id"], self.store.branch_names(project["id"]))
        return sha

    # ------------------------------------------------------------ http

    def call(self, path: str, method: str = "GET", body=None, headers=None, query: str = ""):
        raw = b""
        head = dict(headers or {})
        if body is not None:
            raw = json.dumps(body).encode() if not isinstance(body, bytes) else body
            head.setdefault("CONTENT_TYPE", "application/json")
            head["CONTENT_LENGTH"] = str(len(raw))
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "SERVER_NAME": "testserver",
            "SERVER_PORT": "80",
            "HTTP_HOST": "testserver",
            "wsgi.url_scheme": "http",
            "wsgi.input": io.BytesIO(raw),
            "wsgi.errors": io.StringIO(),
            "wsgi.version": (1, 0),
            "wsgi.multithread": False,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
        }
        for key, value in head.items():
            normalized = key.upper().replace("-", "_")
            if normalized in ("CONTENT_LENGTH", "CONTENT_TYPE"):
                environ[normalized] = value
            else:
                environ["HTTP_" + normalized] = value
        captured = {}

        def start_response(status, response_headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = dict(response_headers)

        chunks = self.app(environ, start_response)
        payload = b"".join(chunks)
        return int(captured["status"].split()[0]), captured["headers"], payload

    def json(self, path: str, method: str = "GET", body=None, token: str | None = None, query: str = ""):
        headers = {"authorization": f"Bearer {token}"} if token else {}
        status, head, payload = self.call(path, method, body, headers, query)
        try:
            return status, json.loads(payload)
        except json.JSONDecodeError:
            return status, payload.decode("utf-8", "replace")

    def token_headers(self, token: str) -> dict:
        return {"authorization": f"Bearer {token}"}


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class _Quiet(WSGIRequestHandler):
    def log_message(self, fmt, *args):
        pass


@contextmanager
def live_server(forge: Forge):
    """a real http server, so clones exercise the actual smart http protocol."""
    httpd = make_server("127.0.0.1", 0, forge.app, server_class=_ThreadingWSGIServer, handler_class=_Quiet)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def clone(url: str, dest: Path, *options: str) -> str:
    """options go before the subcommand, so -c http.extraHeader works."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    return run([*options, "clone", "-q", url, str(dest)])


def pkt_lines(payload: bytes) -> list[str]:
    """parse a pkt-line stream, dropping flush markers."""
    out, index = [], 0
    while index + 4 <= len(payload):
        try:
            length = int(payload[index : index + 4], 16)
        except ValueError:
            break
        if length == 0:
            index += 4
            continue
        chunk = payload[index + 4 : index + length]
        out.append(chunk.decode("utf-8", "replace").rstrip("\n"))
        index += length
    return out


def tempdir() -> str:
    return tempfile.mkdtemp(prefix="forge-test-")


def cleanup(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
