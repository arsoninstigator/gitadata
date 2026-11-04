"""Server-side sessions: one bare clone per editing session, torn down after.

Everything that touches a remote repository happens here. The rules:

- The GitHub token is never stored on the session; each operation that needs it
  takes it as an argument and hands it to git through the environment.
- The lease expectations are captured at clone time (`ls-remote`), so a push is
  refused if anyone pushed between our clone and our push.
- A session directory is a temp dir per repo, deleted on destroy and swept by age.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import threading
import time
import uuid

from . import engine as E
from . import github as G

SESSION_ROOT = os.environ.get("GITADATA_SESSION_ROOT", "/tmp/gitadata-sessions")
SESSION_TTL = int(os.environ.get("GITADATA_SESSION_TTL", 3600))
MAX_REPO_KB = int(os.environ.get("GITADATA_MAX_REPO_KB", 1_500_000))  # ~1.5 GB
CLONE_TIMEOUT_S = int(os.environ.get("GITADATA_CLONE_TIMEOUT", 600))


class SessionError(RuntimeError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclasses.dataclass
class Session:
    id: str
    owner: str
    repo: str
    path: str
    user_id: int                   # the github user who opened this session
    default_branch: str | None
    base_refs: dict[str, str]      # remote refs at clone time: refname -> sha
    empty: bool
    created: float
    last_used: float
    lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


_sessions: dict[str, Session] = {}
_registry_lock = threading.Lock()


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------

def create(owner: str, repo: str, token: str, user_id: int) -> Session:
    info = G.get_repo(token, owner, repo)  # also validates the name
    if not (info.get("permissions") or {}).get("push"):
        raise SessionError(f"no write access to {owner}/{repo}", 403)
    size_kb = int(info.get("size") or 0)
    if size_kb > MAX_REPO_KB:
        raise SessionError(
            f"repository is {size_kb // 1024} MB, over the {MAX_REPO_KB // 1024} MB limit", 413
        )

    os.makedirs(SESSION_ROOT, exist_ok=True)
    sid = uuid.uuid4().hex[:16]
    path = os.path.join(SESSION_ROOT, sid)
    env = G.git_auth_env(token)

    has_commits = G.has_commits(token, owner, repo, info.get("default_branch") or "main")
    try:
        if has_commits:
            E._text(
                SESSION_ROOT,
                "clone",
                "--bare",
                "--filter=blob:none",
                G.https_url(owner, repo),
                path,
                env=env,
            )
        else:
            # an empty repository has nothing to clone; make a bare skeleton
            os.makedirs(path)
            E._text(path, "init", "--bare", "-q")
            E._text(path, "remote", "add", "origin", G.https_url(owner, repo))
    except E.GitError as exc:
        shutil.rmtree(path, ignore_errors=True)
        raise SessionError(f"clone failed: {exc}", 502) from exc

    base = E.ls_remote(path, "origin", env=env)
    now = time.time()
    session = Session(
        id=sid,
        user_id=user_id,
        owner=owner,
        repo=repo,
        path=path,
        default_branch=info.get("default_branch"),
        base_refs=base,
        empty=not has_commits,
        created=now,
        last_used=now,
    )
    with _registry_lock:
        _sessions[sid] = session
    return session


def get(sid: str, user_id: int | None = None) -> Session:
    """Look up a session, and treat another user's session as nonexistent."""
    with _registry_lock:
        session = _sessions.get(sid)
    if session is None:
        raise SessionError("unknown session", 404)
    if user_id is not None and session.user_id != user_id:
        raise SessionError("unknown session", 404)
    session.last_used = time.time()
    return session


def destroy(sid: str) -> bool:
    with _registry_lock:
        session = _sessions.pop(sid, None)
    if session is None:
        return False
    shutil.rmtree(session.path, ignore_errors=True)
    return True


def sweep_orphans(grace_s: int = 30) -> list[str]:
    """Delete session directories no live session owns.

    The registry lives in memory, so a restart (or a crash) leaves clones on
    disk with no owner. Those are pure liability: they hold repository content
    and, in a multi-user deployment, someone else's private code.
    """
    if not os.path.isdir(SESSION_ROOT):
        return []
    with _registry_lock:
        live = {s.id for s in _sessions.values()}
    removed: list[str] = []
    cutoff = time.time() - grace_s
    for name in os.listdir(SESSION_ROOT):
        path = os.path.join(SESSION_ROOT, name)
        if name in live or not os.path.isdir(path):
            continue
        try:
            if os.stat(path).st_mtime > cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed.append(name)
    return removed


def sweep(ttl: int = SESSION_TTL) -> list[str]:
    cutoff = time.time() - ttl
    with _registry_lock:
        stale = [s.id for s in _sessions.values() if s.last_used < cutoff]
    for sid in stale:
        destroy(sid)
    return stale


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def graph(sid: str, user_id: int | None = None, ref: str | None = None) -> dict:
    """The commit graph, optionally narrowed to one branch's history."""
    session = get(sid, user_id)
    with session.lock:
        refs = E.read_refs(session.path, ("refs/heads", "refs/tags"))
        view: str | None = None
        if ref:
            view = normalize_refs(session, [ref])[0]
        if not refs:
            return {
                "session": session.id,
                "repo": session.full_name,
                "empty": True,
                "view": view,
                "default_branch": session.default_branch,
                "branches": [],
                "commits": [],
                "refs": [],
                "warnings": ["repository has no commits yet"],
            }
        commits = E.read_commits(session.path, [view] if view else None)
    by_commit: dict[str, list[str]] = {}
    for ref in refs:
        by_commit.setdefault(ref.commit, []).append(ref.short)
    return {
        "session": session.id,
        "repo": session.full_name,
        "empty": False,
        "view": view,
        "default_branch": session.default_branch,
        "branches": sorted(r.short for r in refs if r.name.startswith("refs/heads/")),
        "commits": [
            {
                "sha": c.sha,
                "parents": list(c.parents),
                "subject": c.subject,
                "body": c.body,
                "author": {"name": c.author.name, "email": c.author.email, "date": c.author.iso},
                "committer": {
                    "name": c.committer.name,
                    "email": c.committer.email,
                    "date": c.committer.iso,
                },
                "signed": c.signed,
                "refs": by_commit.get(c.sha, []),
            }
            for c in commits
        ],
        "refs": [
            {
                "name": ref.name,
                "short": ref.short,
                "target": ref.target,
                "commit": ref.commit,
                "kind": ref.kind,
            }
            for ref in refs
        ],
        "warnings": [f"{c.sha[:10]} is signed" for c in commits if c.signed],
    }


def normalize_refs(session: Session, refs: list[str] | None) -> list[str] | None:
    if not refs:
        return None
    available = {r.name for r in E.read_refs(session.path, ("refs/heads", "refs/tags"))}
    out: list[str] = []
    for value in refs:
        if value.startswith("refs/"):
            candidate = value
        elif value in {"heads", "tags"}:
            continue
        else:
            candidate = f"refs/heads/{value}"
        if candidate not in available:
            matches = [a for a in available if a.endswith("/" + value.lstrip("/"))]
            if len(matches) == 1:
                candidate = matches[0]
            else:
                raise SessionError(f"unknown ref: {value}")
        if candidate not in out:
            out.append(candidate)
    return out


# --------------------------------------------------------------------------
# planning / applying
# --------------------------------------------------------------------------

EDIT_FIELDS = {f.name for f in dataclasses.fields(E.CommitEdit)}


def build_edits(session: Session, raw: dict) -> dict[str, E.CommitEdit]:
    if not isinstance(raw, dict) or not raw:
        raise SessionError("edits must be a non-empty object keyed by commit sha")
    known = {c.sha for c in E.read_commits(session.path)}
    edits: dict[str, E.CommitEdit] = {}
    for key, fields in raw.items():
        if not isinstance(fields, dict):
            raise SessionError(f"{key}: edit must be an object")
        matches = [sha for sha in known if sha.startswith(key)]
        if not matches:
            raise SessionError(f"{key}: no such commit")
        if len(matches) > 1:
            raise SessionError(f"{key}: ambiguous commit prefix")
        unknown = set(fields) - EDIT_FIELDS
        if unknown:
            raise SessionError(f"{key}: unknown field(s) {sorted(unknown)}")
        edit = E.CommitEdit(**fields)
        if edit.is_empty():
            raise SessionError(f"{key}: edit changes nothing")
        edits[matches[0]] = edit
    return edits


def _serialize_plan(session: Session, plan: E.Plan) -> dict:
    return {
        "session": session.id,
        "repo": session.full_name,
        "summary": plan.summary(),
        "noop": plan.is_noop,
        "rewritten_count": len(plan.rewritten),
        "rewritten": [
            {"old": old, "new": new, "subject": _subject(session, old)}
            for old, new in plan.rewritten.items()
        ],
        "refs": [
            {
                "ref": d.refname,
                "kind": d.kind,
                "old": d.old_target,
                "new": d.new_target,
                "changed": d.changed,
            }
            for d in plan.ref_deltas
        ],
        "warnings": plan.warnings,
    }


def _subject(session: Session, sha: str) -> str:
    raw = E._cat_file_batch(session.path, [sha])
    return E._parse_commit(sha, raw[sha][1]).subject


def prechecks(session: Session, token: str, plan: E.Plan) -> dict:
    branches = [d.refname for d in plan.changed_refs if d.kind == "branch"]
    protected: list[str] = []
    for ref in branches:
        branch = ref[len("refs/heads/"):]
        try:
            if G.branch_protection(token, session.owner, session.repo, branch) is not None:
                protected.append(branch)
        except G.GitHubError:
            continue
    touched_branches = {b for b in branches}
    pulls = [
        p
        for p in G.open_pulls(token, session.owner, session.repo)
        if p["base"] in {b[len("refs/heads/"):] for b in touched_branches}
    ]
    return {
        "protected_branches": protected,
        "open_pulls": pulls,
        "blocked": bool(protected),
        "notes": _precheck_notes(protected, pulls),
    }


def _precheck_notes(protected: list[str], pulls: list[dict]) -> list[str]:
    notes: list[str] = []
    for branch in protected:
        notes.append(
            f"{branch} is protected: github will reject the force-push unless you lift the rule"
        )
    for pull in pulls:
        notes.append(
            f"open pr #{pull['number']} targets {pull['base']}: rewriting that branch rewrites "
            f"its base, so the pr will show a rewritten history"
        )
    return notes


def plan_edits(sid: str, raw_edits: dict, refs: list[str] | None = None, user_id: int | None = None) -> dict:
    session = get(sid, user_id)
    with session.lock:
        edits = build_edits(session, raw_edits)
        resolved = normalize_refs(session, refs)
        try:
            plan = E.build_plan(session.path, edits, resolved)
        except E.GitError as exc:
            raise SessionError(str(exc)) from exc
        return _serialize_plan(session, plan)


def apply_edits(
    sid: str,
    raw_edits: dict,
    refs: list[str] | None,
    token: str,
    push: bool = False,
    remote: str = "origin",
    include_tags: bool = True,
    user_id: int | None = None,
) -> dict:
    session = get(sid, user_id)
    env = G.git_auth_env(token)
    with session.lock:
        edits = build_edits(session, raw_edits)
        resolved = normalize_refs(session, refs)
        try:
            plan = E.build_plan(session.path, edits, resolved)
        except E.GitError as exc:
            raise SessionError(str(exc)) from exc

        result: dict = {"plan": _serialize_plan(session, plan), "pushed": False}
        if plan.is_noop:
            return result

        checks = prechecks(session, token, plan)
        result["prechecks"] = checks
        if push and checks["blocked"]:
            raise SessionError(
                "refusing to push: " + "; ".join(checks["notes"]), 409
            )

        snapshot = E.apply_plan(plan)
        result["snapshot"] = snapshot

        if push:
            try:
                result["push_output"] = E.push_plan(
                    plan,
                    remote=remote,
                    include_tags=include_tags,
                    expected=session.base_refs,
                    env=env,
                )
                result["pushed"] = True
                session.base_refs = {
                    **session.base_refs,
                    **{d.refname: d.new_target for d in plan.changed_refs},
                }
            except E.GitError as exc:
                E.undo(session.path, snapshot.split("/")[-1])
                result["error"] = f"push failed, rewrite rolled back: {exc}"
                raise SessionError(result["error"], 502) from exc
        return result


def undo(sid: str, ts: str, user_id: int | None = None) -> dict:
    session = get(sid, user_id)
    with session.lock:
        try:
            restored = E.undo(session.path, ts)
        except E.GitError as exc:
            raise SessionError(str(exc), 404) from exc
    return {"restored": restored, "snapshot": ts}


def backups(sid: str, user_id: int | None = None) -> dict:
    session = get(sid, user_id)
    return {"snapshots": E.list_backups(session.path)}


def sessions_snapshot(user_id: int | None = None) -> list[dict]:
    with _registry_lock:
        return [
            {
                "id": s.id,
                "repo": s.full_name,
                "created": s.created,
                "last_used": s.last_used,
                "empty": s.empty,
            }
            for s in _sessions.values()
            if user_id is None or s.user_id == user_id
        ]
