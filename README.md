# gitadata

deterministic commit-metadata rewriting for already-pushed repositories.
right-click a commit, fix its title, author or dates, and the rewrite is carried
through every descendant so the history stays internally consistent.

status: engine, api, per-user github auth and a working web ui. not deployed.

```
gitadata/
  engine.py              rewrite core: read, plan, apply, push, undo
  oauth.py               github oauth web flow, pkce, signed cookies
  sessions.py            one bare clone per editing session, torn down after
  github.py              github client, stdlib only
  server.py              fastapi app, serves the ui
  cli.py                 graph / plan / apply / backups / undo
  web/                   commit graph, inspector, plan preview
  tests/                 38 engine, 27 session, 34 oauth, 32 api, 16 layout, 19 ui
docs/plan.md             product and architecture notes
```

## run it

```bash
pip install fastapi uvicorn
export GITHUB_OAUTH_CLIENT_ID=...
export GITHUB_OAUTH_CLIENT_SECRET=...
export GITADATA_BASE_URL=http://localhost:8000
export GITADATA_SECRET=$(python3 -c "import secrets;print(secrets.token_hex(32))")
python3 -m uvicorn gitadata.server:app --port 8000
```

create the oauth app at github.com/settings/developers, callback url
`{GITADATA_BASE_URL}/auth/github/callback`. then open the server and sign in.

## auth

the server holds no github token of its own. each visitor authorizes their own
account:

1. `/auth/github/start` redirects to github with a pkce challenge and a one-time
   state value.
2. the callback checks the state, exchanges the code, reads `/user`, and sets an
   http-only, samesite=lax cookie that points at a token held in this process.
3. every api call resolves that cookie to a user, and sessions belong to them.
   someone else's session id returns 404, not 403, so ids are not enumerable.

what it can do: force-push any repository you can push, during your session,
from this process. what it never does: write a token to disk, send a token to
the browser, or share one between users. tokens die with the process or after 8
hours.

scope is `repo`, which is broad because github has no narrower oauth scope for
force-push. a github app with `contents: write`, installed per repository, is the
narrower replacement and slots into `oauth.py` without touching the rest.

local development without registering an oauth app: run with
`GITADATA_DEV_PAT=1` and `GITHUB_TOKEN` set. `/api/me` reports `dev: true` and the
ui says so. never run that way on a shared host.

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
get    /api/me
get    /api/auth/config
get    /auth/github/start
get    /auth/github/callback
post   /auth/logout
get    /api/github/me
get    /api/github/repos?limit=50
post   /api/sessions                      {"repo": "owner/name"}
get    /api/sessions
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
python3 gitadata/tests/test_oauth.py         # pkce, state, cookies, expiry
python3 gitadata/tests/test_api_auth.py      # auth gate, handshake, isolation
node gitadata/tests/test_layout.mjs          # lane layout, pure functions
node gitadata/tests/test_ui.mjs <url>        # needs chrome and a running server
```

the api tests need `httpx`.

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
- the token store is in memory, so a restart signs everyone out and leaves clone
  directories behind; they are swept on startup.
- a rewrite runs inside the request that asked for it, which is fine for normal
  repositories and wrong for very large ones.
