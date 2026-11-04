# gitadata

deterministic commit-metadata rewriting for already-pushed repositories.
right-click a commit, fix its title, author or dates, and the rewrite is carried
through every descendant so the history stays internally consistent.

status: engine, api, github auth and a working web ui. no deploy yet.

```
gitadata/
  engine.py              rewrite core: read, plan, apply, push, undo
  sessions.py            one bare clone per editing session, torn down after
  github.py              github client, stdlib only
  server.py              fastapi app, serves the ui
  cli.py                 graph / plan / apply / backups / undo
  web/                   commit graph, inspector, plan preview
  tests/                 38 engine, 23 session, 16 layout, 19 ui checks
docs/plan.md             product and architecture notes
```

## run it

```bash
pip install fastapi uvicorn
export GITHUB_TOKEN=<fine grained token with contents write>
python3 -m uvicorn gitadata.server:app --port 8000
```

open http://localhost:8000 and paste a repository name. `?repo=owner/name`
opens one directly.

## how it works

1. a session clones the repository bare and blob-less into a temp dir.
2. the ui reads the commit dag from `/graph`.
3. an edit becomes a plan: old sha to new sha per ref, nothing moved yet.
4. apply snapshots the refs under `refs/gitadata/backup/<ts>/`, rebuilds the
   commits, and optionally force-pushes with a lease on what we cloned.
5. a rejected push rolls the local rewrite back. every apply is undoable.

## api

```
get    /api/health
get    /api/github/me
get    /api/github/repos?limit=50
post   /api/sessions                      {"repo": "owner/name"}
get    /api/sessions/{sid}/graph
post   /api/sessions/{sid}/plan           {"edits": {...}, "refs": ["main"]}
post   /api/sessions/{sid}/apply          {"edits": {...}, "refs": ["main"], "push": true}
get    /api/sessions/{sid}/backups
post   /api/sessions/{sid}/undo           {"ts": "20261104T064432Z"}
delete /api/sessions/{sid}
```

an edit is keyed by full sha or unique prefix:

```json
{
  "edits": {
    "a1b2c3d": {"message": "feat: real title\n"},
    "9f8e7d6": {"author_name": "riya sharma", "author_email": "riya@example.com"},
    "1122334": {"strip_trailers_matching": "^co-authored-by:.*claude"}
  },
  "refs": ["main"]
}
```

## tests

```bash
python3 gitadata/tests/run_tests.py          # rewrite mechanics on scratch repos
python3 gitadata/tests/test_sessions.py      # full session against a local remote
node gitadata/tests/test_layout.mjs          # lane layout, pure functions
node gitadata/tests/test_ui.mjs <url>        # needs chrome and a running server
```

## how the token is handled

- read from the server environment per request, never stored on a session, never
  logged, never placed in a url.
- git authenticates over https through an extraheader built at call time and
  passed in the environment, so it never appears in argv or `ps` output.
- git wants basic auth with the token as the username for smart http, not a
  bearer header.

single tenant for now: one token, acting only on repos it can push to. the
multi-tenant shape is a github app installation token per request, which changes
`_token()` in `server.py` and nothing else.

## guarantees the engine holds

- fields you did not edit are preserved byte for byte, including author and
  committer timestamps with their original utc offsets.
- planning moves no refs. only apply does, and it snapshots first.
- rewrites are scoped to the refs you name; commits shared with other branches
  are left alone there.
- annotated tags are rebuilt as tag objects, not flattened to light tags.
- pushes use `--force-with-lease` against expectations captured at clone time.
  a ref with no known expectation is refused rather than pushed blind.
- protected branches and open pull requests are checked before a push.
- warnings for dropped gpg signatures, non-utf8 encodings, and identities git
  silently sanitises.

## known limits

- merge commit structure (parent order, dropping merges) is out of scope for v1.
- signed commits lose their signature on rewrite, by definition.
- planning writes loose commit objects; they are garbage collected if never
  applied.
- the session registry lives in memory, so a restart drops sessions and leaves
  clone directories behind; they are swept on startup.
