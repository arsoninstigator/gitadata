# gitadata plan

a visual gui over a commit graph. right-click a commit, edit its metadata, and
the whole branch behind it is rewritten deterministically. then push.

metadata means: subject and description, author name and email, author date,
committer date, and co-author trailers (the stray `co-authored-by: claude`
line, usually).

## the constraint everything follows from

a commit hash covers its metadata and its parents. editing any pushed commit
therefore rewrites it and every commit above it. there is no edit in place, and
no amount of ui makes that untrue. so the product is mostly a trust product:

- a dry run that lists every old sha and its replacement before anything moves.
- a snapshot of every affected ref under `refs/gitadata/backup/<ts>/`, so undo
  is a ref transaction.
- a lease on the push, so a push that would clobber someone else's work is
  refused instead of silently winning.
- a precheck for protected branches and open pull requests, because github will
  reject or mangle those and the user should know before clicking.
- honest warnings for signed commits (signature lost), non-utf8 messages, and
  identities git quietly trims.

## shape

```
browser (graph, inspector, plan preview)
   |
   |  rest: session -> graph -> plan -> apply -> undo
   v
fastapi service (python)
   - one bare, blob-less clone per session in a temp dir, deleted after
   - the rewrite engine, which knows nothing about http
   - github app installation token per request (today: one pat from env)
```

python for the engine, because that is where the logic is and it is testable
without a browser. a build-free vanilla frontend for the graph, because the
screen is one canvas and one panel; no bundler to maintain yet.

## why not streamlit

it would get the editor on screen in an hour, but the thing being right-clicked
is a dag: lanes, curves, hover, long scroll. streamlit reruns the whole script on
every interaction and has no canvas or context menu, so the graph ends up as an
html component inside an iframe talking over postMessage. at that point the
frontend is already javascript, and the rerun model is pure cost.

## scope of a rewrite

a commit can be shared by several branches. two honest options:

1. scope the rewrite to one branch: other branches keep the old history, and the
   ui says how many refs still carry the old metadata.
2. rewrite every ref that contains it.

v1 defaults to option 1 with the count shown, because surprising a second branch
is worse than leaving it alone.

## milestones

1. engine, tested. done.
2. api, sessions, github auth. done.
3. commit graph and inspector. done.
4. push with lease, undo, protection prechecks. done.
5. github app auth, background jobs for large repos, deploy.
6. merge commit structure (parent edits, dropping merges) on the same engine.

## open questions

- background jobs: a 20k commit rewrite is seconds of cpu but a long pack push.
  it should not ride on a single http request.
- big repositories: blob-less partial clone covers most of it, but a full pack
  push from a blob-less clone needs a fallback that fetches blobs on demand.
- guest mode: a read-only graph of any public repo is a good demo funnel before
  anyone authorizes anything.
