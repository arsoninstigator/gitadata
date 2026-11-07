"""git plumbing: bare repositories, filtered views and smart http.

the interesting part is the filtered view. git has no notion of a path you are
not allowed to fetch, so hiding `secrets/**` from an anonymous clone means
serving a different history: the trees are pruned, the commits that contain
them are rewritten, and the result is published as a throwaway bare repository
whose object store points back at the real one through `objects/info/alternates`.

only refs are written per view; objects are copied when a commit had to change.
a public branch with no private paths keeps its original commit ids, so cloning
it gives byte identical shas.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import paths as pathlib_

SAFE = ["-c", "safe.directory=*"]


class GitError(RuntimeError):
    pass


def git(args: list[str], cwd: str | Path | None = None, input: bytes | None = None, env=None):
    proc = subprocess.run(
        ["git", *SAFE, *args],
        cwd=str(cwd) if cwd else None,
        input=input,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    if proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout


def pkt_line(payload: bytes) -> bytes:
    return b"%04x" % (len(payload) + 4) + payload


FLUSH = b"0000"


class GitStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.repos = self.root / "repos"
        self.views = self.root / "views"
        self.repos.mkdir(parents=True, exist_ok=True)
        self.views.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- repos

    def repo_dir(self, project_id: int) -> Path:
        return self.repos / f"p{project_id}.git"

    def ensure_repo(self, project_id: int) -> Path:
        path = self.repo_dir(project_id)
        if not (path / "HEAD").exists():
            path.mkdir(parents=True, exist_ok=True)
            git(["init", "--bare", "-q", str(path)])
            for key, value in (
                ("uploadpack.allowTipSHA1InWant", "false"),
                ("uploadpack.allowReachableSHA1InWant", "false"),
                ("uploadpack.allowAnySHA1InWant", "false"),
                ("receive.denyNonFastForwards", "false"),
                ("gc.auto", "0"),
            ):
                git(["-C", str(path), "config", key, value])
        return path

    def refs(self, project_id: int) -> dict[str, dict[str, str]]:
        """{'branches': {name: sha}, 'tags': {name: sha}}"""
        path = self.repo_dir(project_id)
        if not (path / "HEAD").exists():
            return {"branches": {}, "tags": {}}
        out = git(
            ["-C", str(path), "for-each-ref", "--format=%(refname) %(objectname) %(objecttype)"]
        ).decode()
        branches, tags = {}, {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) != 3:
                continue
            refname, sha, kind = parts
            if refname.startswith("refs/heads/"):
                branches[refname[len("refs/heads/") :]] = sha
            elif refname.startswith("refs/tags/") and kind in ("commit", "tag"):
                tags[refname[len("refs/tags/") :]] = sha
        return {"branches": branches, "tags": tags}

    def set_head(self, repo: Path, branch: str | None) -> None:
        """point HEAD at a branch that exists, so a clone checks something out."""
        if not branch:
            return
        try:
            git(["-C", str(repo), "symbolic-ref", "HEAD", f"refs/heads/{branch}"])
        except GitError:
            return

    def branch_names(self, project_id: int) -> list[str]:
        return sorted(self.refs(project_id)["branches"])

    def last_activity(self, project_id: int) -> str | None:
        refs = self.refs(project_id)["branches"]
        if not refs:
            return None
        stamps = []
        for sha in refs.values():
            try:
                stamps.append(
                    int(git(["-C", str(self.repo_dir(project_id)), "show", "-s", "--format=%ct", sha])
                        .decode()
                        .strip())
                )
            except GitError:
                continue
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(max(stamps))) if stamps else None

    def tree_at(self, project_id: int, rev: str) -> list[dict]:
        """flat listing of one revision, for the file browser."""
        path = self.repo_dir(project_id)
        try:
            out = git(["-C", str(path), "ls-tree", "-r", "-z", "--long", rev])
        except GitError:
            return []
        entries = []
        for chunk in out.split(b"\x00"):
            if not chunk:
                continue
            head, _, name = chunk.partition(b"\t")
            parts = head.split()
            if len(parts) < 4:
                continue
            entries.append(
                {
                    "mode": parts[0].decode(),
                    "type": parts[1].decode(),
                    "sha": parts[2].decode(),
                    "size": int(parts[3]) if parts[3].isdigit() else 0,
                    "path": name.decode("utf-8", "replace"),
                }
            )
        return entries

    # ------------------------------------------------------------- views

    def view_key(self, project_id: int, branches: dict, tags: dict | None = None) -> str:
        payload = {
            "project": project_id,
            "branches": {
                name: {"sha": sha, "patterns": sorted(patterns)}
                for name, (sha, patterns) in sorted(branches.items())
            },
            "tags": tags or {},
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]

    def view_for(
        self,
        project_id: int,
        branches: dict[str, tuple[str, list[str]]],
        tags: dict[str, str] | None = None,
    ) -> Path:
        """a bare repo the caller is allowed to see, cached by content.

        `branches` maps a branch name to (commit sha, path rules for that
        branch). branches whose rules differ get their history rewritten
        independently, so a rule scoped to release branches does not prune the
        same file on main.
        """
        key = self.view_key(project_id, branches, tags)
        dest = self.views / key
        if (dest / "FORGE_VIEW").exists():
            return dest
        real = self.ensure_repo(project_id)
        staging = self.views / f".staging-{key}-{os.getpid()}-{int(time.time()*1000)}"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        git(["init", "--bare", "-q", str(staging)])
        objects = real / "objects"
        (staging / "objects" / "info").mkdir(parents=True, exist_ok=True)
        (staging / "objects" / "info" / "alternates").write_text(str(objects.resolve()) + "\n")
        rehydrators: dict[tuple[str, ...], _Rehydrator] = {}
        try:
            for name, (sha, patterns) in branches.items():
                policy = tuple(sorted(patterns))
                if policy not in rehydrators:
                    rehydrators[policy] = _Rehydrator(staging, list(policy))
                mapped = rehydrators[policy].commit(sha)
                if mapped:
                    git(["-C", str(staging), "update-ref", f"refs/heads/{name}", mapped])
            for name, sha in (tags or {}).items():
                mapped = rehydrators.get((), _Rehydrator(staging, [])).commit(sha)
                if mapped:
                    git(["-C", str(staging), "update-ref", f"refs/tags/{name}", mapped])
            for key_, value in (
                ("uploadpack.allowTipSHA1InWant", "false"),
                ("uploadpack.allowReachableSHA1InWant", "false"),
                ("uploadpack.allowAnySHA1InWant", "false"),
            ):
                git(["-C", str(staging), "config", key_, value])
            (staging / "FORGE_VIEW").write_text(
                json.dumps(
                    {
                        "key": key,
                        "created": time.time(),
                        "policies": {n: sorted(p) for n, (_, p) in branches.items()},
                    }
                )
            )
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        try:
            staging.rename(dest)
        except OSError:
            shutil.rmtree(staging, ignore_errors=True)
        return dest

    def prune_views(self, max_age_seconds: int = 24 * 3600) -> int:
        cutoff = time.time() - max_age_seconds
        removed = 0
        for entry in self.views.iterdir():
            try:
                if entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
            except FileNotFoundError:
                continue
        return removed


class _Rehydrator:
    """rewrites one history so that private paths are absent from every tree."""

    def __init__(self, repo: Path, patterns: list[str]):
        self.repo = repo
        self.patterns = [p for p in patterns if p]
        self.memo: dict[str, str] = {}
        self.rules = len(self.patterns)

    def _git(self, args: list[str], input: bytes | None = None) -> bytes:
        return git(["-C", str(self.repo), *args], input=input)

    def commit(self, sha: str) -> str:
        if sha in self.memo:
            return self.memo[sha]
        raw = self._git(["cat-file", "-p", sha])
        text = raw.decode("utf-8", "surrogateescape")
        head, sep, message = text.partition("\n\n")
        lines = head.split("\n")
        tree_sha = None
        parents: list[str] = []
        kept: list[str] = []
        for line in lines:
            if line.startswith("tree "):
                tree_sha = line[5:].strip()
            elif line.startswith("parent "):
                parents.append(line[7:].strip())
            elif line.startswith(("gpgsig", "mergetag")):
                continue  # signature cannot survive a rewrite
            elif line.startswith(" ") and kept and kept[-1].startswith(("gpgsig", "mergetag")):
                continue
            else:
                kept.append(line)
        if tree_sha is None:
            raise GitError(f"{sha} is not a commit")
        new_tree = self.tree(tree_sha, "")
        new_parents = [self.commit(p) for p in parents]
        if new_tree == tree_sha and new_parents == parents:
            self.memo[sha] = sha
            return sha
        rebuilt = ["tree " + new_tree]
        rebuilt.extend("parent " + p for p in new_parents)
        rebuilt.extend(line for line in kept if not line.startswith("tree "))
        body = "\n".join(rebuilt) + "\n\n" + message
        new_sha = (
            self._git(["hash-object", "-t", "commit", "-w", "--stdin"], body.encode("utf-8", "surrogateescape"))
            .decode()
            .strip()
        )
        self.memo[sha] = new_sha
        return new_sha

    def tree(self, sha: str, prefix: str) -> str:
        relevant = [p for p in self.patterns if pathlib_.could_match_under(p, prefix)]
        if not relevant:
            return sha
        out = self._git(["ls-tree", "-z", sha])
        entries, changed = [], False
        for chunk in out.split(b"\x00"):
            if not chunk:
                continue
            head, _, name = chunk.partition(b"\t")
            mode, kind, obj = head.decode().split()
            path = f"{prefix}/{name.decode('utf-8', 'surrogateescape')}" if prefix else name.decode("utf-8", "surrogateescape")
            if kind == "blob":
                if pathlib_.matches_any(path, relevant):
                    changed = True
                    continue
                entries.append(f"{mode} {kind} {obj}\t{name.decode('utf-8', 'surrogateescape')}")
            elif kind == "tree":
                new_obj = self.tree(obj, path)
                if new_obj != obj:
                    changed = True
                remaining = self._git(["ls-tree", "-z", new_obj])
                if not remaining.strip(b"\x00"):
                    continue  # the whole directory was private
                entries.append(f"{mode} {kind} {new_obj}\t{name.decode('utf-8', 'surrogateescape')}")
            else:  # submodule pointer: keep, the pointer leaks nothing
                entries.append(f"{mode} {kind} {obj}\t{name.decode('utf-8', 'surrogateescape')}")
        if not changed:
            return sha
        payload = ("\n".join(entries) + ("\n" if entries else "")).encode("utf-8", "surrogateescape")
        return self._git(["mktree"], payload).decode().strip()


# ---------------------------------------------------------------- smart http


SERVICE_COMMAND = {
    "git-upload-pack": "upload-pack",
    "git-receive-pack": "receive-pack",
}


def service_content_type(service: str, phase: str) -> str:
    suffix = "advertisement" if phase == "advertise" else "result"
    return f"application/x-{service}-{suffix}"


def advertise_refs(repo: Path, service: str, protocol: str | None = None) -> bytes:
    env = dict(os.environ)
    if protocol:
        env["GIT_PROTOCOL"] = protocol
    out = pkt_line(f"# service={service}\n".encode()) + FLUSH
    command = SERVICE_COMMAND[service]
    out += git([command, "--stateless-rpc", "--advertise-refs", str(repo)], env=env)
    return out


def rpc(repo: Path, service: str, body: bytes, protocol: str | None = None) -> bytes:
    env = dict(os.environ)
    if protocol:
        env["GIT_PROTOCOL"] = protocol
    command = SERVICE_COMMAND[service]
    return git([command, "--stateless-rpc", str(repo)], input=body, env=env)
