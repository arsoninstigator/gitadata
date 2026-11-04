"""gitadata rewrite engine.

Deterministic commit-metadata rewriting for already-pushed repositories.

Design rules this file enforces:

1. Nothing is mutated unless you call ``apply_plan`` (planning writes loose
   objects only, which is additive and harmless).
2. Every field you did not ask to edit is preserved byte for byte, including
   author/committer timestamps with their original UTC offsets. Losing dates is
   the classic history-rewrite disaster; the engine carries them forward.
3. Rewriting is scoped: only commits reachable from the refs you target change.
   Other branches keep pointing at the old history.
4. Every applied rewrite first snapshots the affected refs under
   ``refs/gitadata/backup/<ts>/...`` so undo is a ref update, not a guess.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
import subprocess
from typing import Iterable, Mapping, Sequence

BACKUP_NS = "refs/gitadata/backup"
BOT_MARKER = "gitadata"

_SIG_RE = re.compile(r"^(?P<name>.*) <(?P<email>.*)> (?P<when>\d+ [+-]\d{4})$")


class GitError(RuntimeError):
    """A git command failed."""


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------

def _git(
    repo: str,
    *args: str,
    stdin: bytes | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
) -> bytes:
    full_env = None
    if env is not None:
        import os

        full_env = {**os.environ, **env}
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        input=stdin,
        capture_output=True,
        env=full_env,
    )
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout


def _text(repo: str, *args: str, **kw) -> str:
    return _git(repo, *args, **kw).decode("utf-8", "surrogateescape")


def is_repo(path: str) -> bool:
    try:
        return _text(path, "rev-parse", "--git-dir").strip() != ""
    except GitError:
        return False


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Signature:
    name: str
    email: str
    when: str  # "<epoch> <+hhmm>", git's native form: lossless round trip

    @property
    def iso(self) -> str:
        epoch, tz = self.when.split(" ")
        sign = -1 if tz.startswith("-") else 1
        offset = dt.timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5])) * sign
        return dt.datetime.fromtimestamp(int(epoch), dt.timezone(offset)).isoformat()

    def replace(self, **kw) -> "Signature":
        return dataclasses.replace(self, **kw)


@dataclasses.dataclass(frozen=True)
class Commit:
    sha: str
    tree: str
    parents: tuple[str, ...]
    author: Signature
    committer: Signature
    message: str
    encoding: str | None = None
    signed: bool = False

    @property
    def subject(self) -> str:
        return self.message.split("\n", 1)[0]

    @property
    def body(self) -> str:
        parts = self.message.split("\n", 1)
        return parts[1].lstrip("\n") if len(parts) > 1 else ""


@dataclasses.dataclass
class CommitEdit:
    """Which fields of one commit to change. ``None`` means leave alone."""

    message: str | None = None
    author_name: str | None = None
    author_email: str | None = None
    author_date: str | int | None = None      # ISO 8601 or epoch seconds
    committer_name: str | None = None
    committer_email: str | None = None
    committer_date: str | int | None = None
    strip_coauthors: bool = False             # drop "Co-Authored-By:" trailers
    strip_trailers_matching: str | None = None  # regex, case-insensitive

    def is_empty(self) -> bool:
        return (
            self.message is None
            and self.author_name is None
            and self.author_email is None
            and self.author_date is None
            and self.committer_name is None
            and self.committer_email is None
            and self.committer_date is None
            and not self.strip_coauthors
            and self.strip_trailers_matching is None
        )


def to_git_date(value: str | int) -> str:
    """Accept ISO 8601 (with or without offset) or epoch seconds -> 'epoch +hhmm'."""
    if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
        return f"{int(value)} +0000"
    raw = str(value).strip()
    if re.fullmatch(r"\d+ [+-]\d{4}", raw):
        return raw
    parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    offset = parsed.utcoffset() or dt.timedelta(0)
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    tz = f"{sign}{total // 3600:02d}{(total % 3600) // 60:02d}"
    return f"{int(parsed.timestamp())} {tz}"


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def _cat_file_batch(repo: str, shas: Sequence[str]) -> dict[str, tuple[str, bytes]]:
    if not shas:
        return {}
    out = _git(repo, "cat-file", "--batch", stdin=("\n".join(shas) + "\n").encode())
    result: dict[str, tuple[str, bytes]] = {}
    pos = 0
    while pos < len(out):
        nl = out.index(b"\n", pos)
        header = out[pos:nl].decode("utf-8", "surrogateescape")
        if header.endswith(" missing") or header.endswith(" ambiguous"):
            raise GitError(f"object not found: {header}")
        sha, otype, size = header.split(" ")
        start = nl + 1
        end = start + int(size)
        result[sha] = (otype, out[start:end])
        pos = end + 1  # skip trailing newline
    return result


def _parse_signature(raw: str) -> Signature:
    m = _SIG_RE.match(raw)
    if not m:
        raise GitError(f"unparseable signature line: {raw!r}")
    return Signature(m["name"], m["email"], m["when"])


def _parse_commit(sha: str, raw: bytes) -> Commit:
    head, _, body = raw.partition(b"\n\n")
    tree = ""
    parents: list[str] = []
    author = committer = None
    encoding = None
    signed = False
    for line in head.split(b"\n"):
        if line.startswith(b" "):  # continuation of gpgsig / mergetag
            continue
        key, _, value = line.decode("utf-8", "surrogateescape").partition(" ")
        if key == "tree":
            tree = value
        elif key == "parent":
            parents.append(value)
        elif key == "author":
            author = _parse_signature(value)
        elif key == "committer":
            committer = _parse_signature(value)
        elif key == "encoding":
            encoding = value
        elif key in ("gpgsig", "gpgsig-sha256"):
            signed = True
    if not (tree and author and committer):
        raise GitError(f"incomplete commit object {sha}")
    return Commit(
        sha=sha,
        tree=tree,
        parents=tuple(parents),
        author=author,
        committer=committer,
        message=body.decode("utf-8", "surrogateescape"),
        encoding=encoding,
        signed=signed,
    )


def _commit_shas(repo: str, revs: Sequence[str] | None) -> list[str]:
    args = ["rev-list", "--topo-order", "--reverse", *(["--all"] if not revs else list(revs))]
    out = _text(repo, *args)
    return [line for line in out.split("\n") if line.strip()]


def read_commits(repo: str, revs: Sequence[str] | None = None) -> list[Commit]:
    """Commits reachable from ``revs`` (default: all refs), parents before children."""
    shas = _commit_shas(repo, revs)
    blobs = _cat_file_batch(repo, shas)
    return [_parse_commit(sha, blobs[sha][1]) for sha in shas]


@dataclasses.dataclass(frozen=True)
class Ref:
    name: str
    target: str      # object the ref points at (may be an annotated tag object)
    kind: str        # commit | tag
    commit: str      # commit it ultimately refers to

    @property
    def short(self) -> str:
        for prefix in ("refs/heads/", "refs/tags/", "refs/remotes/"):
            if self.name.startswith(prefix):
                return self.name[len(prefix):]
        return self.name


def read_refs(repo: str, scopes: Sequence[str] = ("refs/heads", "refs/tags")) -> list[Ref]:
    fmt = "%(refname)%00%(objectname)%00%(objecttype)%00%(*objectname)"
    out = _text(repo, "for-each-ref", f"--format={fmt}", *scopes)
    refs: list[Ref] = []
    for line in out.split("\n"):
        if not line.strip():
            continue
        name, target, otype, deref = line.split("\x00")
        commit = deref if otype == "tag" else target
        refs.append(Ref(name, target, otype, commit))
    return refs


# --------------------------------------------------------------------------
# editing one message / signature
# --------------------------------------------------------------------------

def _strip_message(message: str, edit: CommitEdit) -> str:
    if not (edit.strip_coauthors or edit.strip_trailers_matching):
        return message
    lines = message.split("\n")
    kept = []
    pattern = None
    if edit.strip_trailers_matching:
        pattern = re.compile(edit.strip_trailers_matching, re.IGNORECASE)
    for line in lines:
        low = line.strip().lower()
        if edit.strip_coauthors and low.startswith("co-authored-by:"):
            continue
        if pattern is not None and pattern.search(line):
            continue
        kept.append(line)
    return re.sub(r"\n{3,}$", "\n\n", "\n".join(kept).rstrip("\n") + "\n")


def apply_edit(commit: Commit, edit: CommitEdit) -> Commit:
    message = commit.message if edit.message is None else edit.message
    message = _strip_message(message, edit)
    if not message.endswith("\n"):
        message += "\n"

    author = commit.author
    if edit.author_name is not None:
        author = author.replace(name=edit.author_name)
    if edit.author_email is not None:
        author = author.replace(email=edit.author_email)
    if edit.author_date is not None:
        author = author.replace(when=to_git_date(edit.author_date))

    committer = commit.committer
    if edit.committer_name is not None:
        committer = committer.replace(name=edit.committer_name)
    if edit.committer_email is not None:
        committer = committer.replace(email=edit.committer_email)
    if edit.committer_date is not None:
        committer = committer.replace(when=to_git_date(edit.committer_date))

    return dataclasses.replace(commit, message=message, author=author, committer=committer)


def _write_commit(repo: str, commit: Commit, parents: Sequence[str]) -> str:
    args = ["commit-tree", commit.tree]
    for parent in parents:
        args += ["-p", parent]
    env = {
        "GIT_AUTHOR_NAME": commit.author.name,
        "GIT_AUTHOR_EMAIL": commit.author.email,
        "GIT_AUTHOR_DATE": commit.author.when,
        "GIT_COMMITTER_NAME": commit.committer.name,
        "GIT_COMMITTER_EMAIL": commit.committer.email,
        "GIT_COMMITTER_DATE": commit.committer.when,
    }
    out = _git(
        repo,
        *args,
        stdin=commit.message.encode("utf-8", "surrogateescape"),
        env=env,
    )
    return out.decode().strip()


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------

@dataclasses.dataclass
class RefDelta:
    refname: str
    kind: str            # branch | tag | tag-annotated
    old_target: str
    new_target: str
    old_commit: str
    new_commit: str

    @property
    def changed(self) -> bool:
        return self.old_target != self.new_target


@dataclasses.dataclass
class Plan:
    repo: str
    new_shas: dict[str, str]                 # every commit in scope -> resulting sha
    rewritten: dict[str, str]                # only the ones whose sha changed
    edits: dict[str, CommitEdit]
    ref_deltas: list[RefDelta]
    warnings: list[str]
    backup_ns: str | None = None

    @property
    def changed_refs(self) -> list[RefDelta]:
        return [d for d in self.ref_deltas if d.changed]

    @property
    def is_noop(self) -> bool:
        return not self.changed_refs

    def summary(self) -> str:
        if self.is_noop:
            return "no changes"
        branches = [d.refname for d in self.changed_refs if d.kind == "branch"]
        tags = [d.refname for d in self.changed_refs if d.kind != "branch"]
        bits = [f"rewrite {len(self.rewritten)} commit(s)"]
        if branches:
            bits.append(f"{len(branches)} branch(es): {', '.join(sorted(branches))}")
        if tags:
            bits.append(f"{len(tags)} tag(s): {', '.join(sorted(tags))}")
        return "; ".join(bits)


def build_plan(
    repo: str,
    edits: Mapping[str, CommitEdit],
    refs: Sequence[str] | None = None,
) -> Plan:
    """Work out the rewrite without touching any ref.

    ``refs`` limits the blast radius (e.g. ``["refs/heads/main"]``). Default is
    every local branch and tag. New commit objects are written to the object
    database as a side effect, but no ref moves until ``apply_plan``.
    """
    if not is_repo(repo):
        raise GitError(f"not a git repository: {repo}")

    available = {ref.name: ref for ref in read_refs(repo)}
    targets: list[Ref]
    if refs is None:
        targets = list(available.values())
    else:
        missing = [r for r in refs if r not in available]
        if missing:
            raise GitError(f"unknown ref(s): {', '.join(missing)}")
        targets = [available[r] for r in refs]

    scope = sorted({ref.commit for ref in targets})
    commits = read_commits(repo, scope)
    known = {c.sha for c in commits}
    for sha in edits:
        if sha not in known:
            raise GitError(f"commit {sha} is not reachable from the targeted refs")

    new_shas: dict[str, str] = {}
    rewritten: dict[str, str] = {}
    intended: dict[str, Commit] = {}
    warnings: list[str] = []

    for commit in commits:
        parents = tuple(new_shas.get(p, p) for p in commit.parents)
        parents_changed = parents != commit.parents
        edit = edits.get(commit.sha)

        if edit is None and not parents_changed:
            new_shas[commit.sha] = commit.sha
            continue

        edited = apply_edit(commit, edit) if edit is not None else commit
        if edited.signed:
            warnings.append(
                f"{commit.sha[:10]}: GPG signature dropped (a rewritten commit cannot keep it)"
            )
        if edited.encoding and edited.encoding.lower() not in ("utf-8", "utf8"):
            warnings.append(
                f"{commit.sha[:10]}: message encoding {edited.encoding} is not preserved "
                f"exactly (stored as UTF-8)"
            )
        new_sha = _write_commit(repo, edited, parents)
        new_shas[commit.sha] = new_sha
        if new_sha != commit.sha:
            rewritten[commit.sha] = new_sha
        if edit is not None:
            intended[new_sha] = edited

    if intended:
        stored = _cat_file_batch(repo, list(intended))
        for new_sha, want in intended.items():
            got = _parse_commit(new_sha, stored[new_sha][1])
            if got.author != want.author or got.committer != want.committer:
                warnings.append(
                    f"{want.sha[:10]}: git rewrote the identity you asked for "
                    f"(it strips leading/trailing punctuation and whitespace, and <> etc.): "
                    f"asked {want.author.name!r} <{want.author.email}>, "
                    f"stored {got.author.name!r} <{got.author.email}>"
                )

    deltas: list[RefDelta] = []
    for ref in targets:
        new_commit = new_shas.get(ref.commit, ref.commit)
        if ref.kind == "tag":
            new_target = (
                _rewrite_tag_object(repo, ref.target, new_commit, warnings)
                if new_commit != ref.commit
                else ref.target
            )
            kind = "tag-annotated"
        else:
            new_target = new_commit
            kind = "branch"
        deltas.append(RefDelta(ref.name, kind, ref.target, new_target, ref.commit, new_commit))

    for sha in edits:
        if sha not in new_shas:
            warnings.append(f"{sha[:10]}: outside the targeted refs, ignored")

    return Plan(repo, new_shas, rewritten, dict(edits), deltas, warnings)


def _rewrite_tag_object(repo: str, tag_sha: str, new_commit: str, warnings: list[str]) -> str:
    raw = _cat_file_batch(repo, [tag_sha])[tag_sha][1].decode("utf-8", "surrogateescape")
    if "-----BEGIN PGP SIGNATURE-----" in raw:
        warnings.append(f"tag {tag_sha[:10]}: signature dropped on rewrite")
        raw = raw.split("\n-----BEGIN PGP SIGNATURE-----")[0]
    lines = raw.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("object "):
            lines[i] = f"object {new_commit}"
            break
    body = "\n".join(lines)
    out = _git(repo, "hash-object", "-t", "tag", "-w", "--stdin", stdin=body.encode("utf-8", "surrogateescape"))
    return out.decode().strip()


# --------------------------------------------------------------------------
# applying / undo
# --------------------------------------------------------------------------

def _backup_ref_name(refname: str, ts: str) -> str:
    tail = refname[len("refs/"):] if refname.startswith("refs/") else refname
    return f"{BACKUP_NS}/{ts}/{tail}"


def _fresh_stamp(repo: str) -> str:
    """A snapshot id no existing backup namespace already uses.

    Two rewrites inside the same second are normal (retry after a rejected
    push), and a second snapshot must never fail to write its backup.
    """
    base = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    taken = set()
    out = _text(repo, "for-each-ref", "--format=%(refname)", f"{BACKUP_NS}/{base}")
    for line in out.split("\n"):
        line = line.strip()
        if line.startswith(f"{BACKUP_NS}/"):
            taken.add(line[len(BACKUP_NS) + 1:].split("/", 1)[0])
    if base not in taken:
        return base
    index = 2
    while f"{base}-{index}" in taken:
        index += 1
    return f"{base}-{index}"


def apply_plan(plan: Plan, dry_run: bool = False) -> str | None:
    """Move the refs. Returns the backup namespace, or None when nothing moved."""
    if plan.is_noop:
        return None
    ts = _fresh_stamp(plan.repo)
    ns = f"{BACKUP_NS}/{ts}"
    plan.backup_ns = ns

    lines: list[str] = []
    for delta in plan.changed_refs:
        lines.append(f"create {_backup_ref_name(delta.refname, ts)} {delta.old_target}")
    for delta in plan.changed_refs:
        lines.append(f"update {delta.refname} {delta.new_target} {delta.old_target}")
    payload = ("\n".join(lines) + "\n").encode()

    if dry_run:
        return ns
    _git(plan.repo, "update-ref", "--stdin", stdin=payload)
    return ns


def list_backups(repo: str) -> list[str]:
    out = _text(repo, "for-each-ref", "--format=%(refname)", BACKUP_NS)
    stamps = set()
    for line in out.split("\n"):
        if not line.strip():
            continue
        rest = line[len(BACKUP_NS) + 1:]
        stamps.add(rest.split("/", 1)[0])
    return sorted(stamps, reverse=True)


def undo(repo: str, ts: str) -> list[str]:
    """Restore every ref snapshotted at ``ts`` and drop the snapshot refs."""
    prefix = f"{BACKUP_NS}/{ts}/"
    out = _text(repo, "for-each-ref", "--format=%(refname)%00%(objectname)", prefix)
    entries = [line.split("\x00") for line in out.split("\n") if line.strip()]
    if not entries:
        raise GitError(f"no backup snapshot {ts}")
    lines = []
    for name, sha in entries:
        refname = "refs/" + name[len(prefix):]
        lines.append(f"update {refname} {sha}")
    _git(repo, "update-ref", "--stdin", stdin=("\n".join(lines) + "\n").encode())
    _git(repo, "update-ref", "--stdin", stdin=("".join(f"delete {n}\n" for n, _ in entries)).encode())
    return ["refs/" + n[len(prefix):] for n, _ in entries]


# --------------------------------------------------------------------------
# pushing
# --------------------------------------------------------------------------

def push_plan(
    plan: Plan,
    remote: str = "origin",
    include_tags: bool = True,
    dry_run: bool = False,
    expected: Mapping[str, str] | None = None,
    env: Mapping[str, str] | None = None,
    allow_unleased: bool = False,
) -> str:
    """Force-push the rewritten refs, leased against expectations captured earlier.

    ``expected`` maps refname -> the sha we believe the remote still has. It is
    deliberately not read from remote-tracking refs: in a bare server-side clone
    those do not exist, and a stale-but-present ref is worse than no ref. Refs
    with no known expectation are refused unless ``allow_unleased`` is set.
    """
    refspecs: list[str] = []
    leases: list[str] = []
    for delta in plan.changed_refs:
        if delta.kind != "branch" and not include_tags:
            continue
        refspecs.append(f"{delta.refname}:{delta.refname}")
        lease = expected.get(delta.refname) if expected else None
        if lease is None:
            lease = _remote_tracking_sha(plan.repo, remote, delta.refname)
        if lease:
            leases.append(f"--force-with-lease={delta.refname}:{lease}")
        elif not allow_unleased:
            raise GitError(
                f"no lease expectation for {delta.refname}: refusing to force-push blind"
            )
    if not refspecs:
        return "nothing to push"
    args = ["push", *leases, "--no-verify"]
    if dry_run:
        args.append("--dry-run")
    args += [remote, *refspecs]
    return _text(plan.repo, *args, env=env)


def ls_remote(repo: str, remote: str = "origin", env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Snapshot the remote's refs: refname -> sha (peeled for annotated tags)."""
    out = _text(repo, "ls-remote", remote, env=env)
    refs: dict[str, str] = {}
    for line in out.split("\n"):
        if not line.strip():
            continue
        sha, name = line.split("\t", 1)
        if name.endswith("^{}"):
            refs[name[:-3]] = sha
        else:
            refs.setdefault(name, sha)
    return refs
