# gitadata forge

this branch is not the metadata editor on `main`. it is a different product that
lives in the same repository: a lightweight git forge, the kind of thing you
would run instead of gitlab or gitea when you want three things they make hard.

1. branches and branch groups that can be private inside a public repository
2. paths that can be private inside a public branch
3. nested organisations and projects whose urls can be promoted to `/@project`

everything else is ordinary forge furniture: a cross user repository list, a
clickable file browser, json for every page, and git smart http so `git clone`
works against it with no plugins.

```
python3 -m forge.cli --data ./data demo
python3 -m forge.cli --data ./data serve --port 8080
```

that seeds two users, one nested org, one promoted project and one private
branch, then serves it. no dependencies outside the standard library and git.

## what it does differently

### private branches inside a public repo

a repository is public, so anonymous readers can clone it. one branch in it is
not: `notes` is a scratch branch with half finished work. it is absent from the
ref advertisement for anyone who is not a member, so `git clone` never sees it
and `git ls-remote` does not list it. no separate repository, no second copy to
keep in sync.

### private paths inside a public branch

git has no concept of a path you may not fetch, so the server serves a different
history. for a viewer below maintainer the trees are pruned against glob rules
and the commits that contained those paths are rewritten for that viewer. the
result is published as a throwaway bare repository whose object store points
back at the real one with `objects/info/alternates`, so only rewritten objects
cost disk.

rules are scoped by branch glob, so `secrets/**` can be private everywhere while
`drafts/**` is private only on `release-*`.

a branch with nothing pruned keeps its original commit ids. that is checked in
the test suite, because a forge that rewrites public history for no reason would
be unusable.

### branch groups with semantic tags

branches are grouped (`release`, `feature`, `experiment`) and each group carries
semantics: `channel=release`, `stability=stable`, `semver=1.4.0`. a branch
inherits its group's semantics and may override keys. a group tagged
`channel=release` must also declare `stability`, so "release branch" is a
queryable fact instead of a naming convention.

### metadata tags that cannot lie

tags are a closed vocabulary. `self-hostable` is the strict one: a project
cannot carry it without at least one declared dependency, and every dependency
needs a kind (`runtime`, `service`, `build`, `data`, `package`), a version and a
url. the same list is published as json at `/<owner>/<project>/manifest.json`
and at `/api/dependencies`, so "what do i need to run this" is answered by a
document rather than a human.

### nested orgs, and promoted urls

```
/anne/notes                 a project owned by a user
/stack/labs/tinygit         a project two orgs deep
/o/stack/labs               that suborg, on its own page
/@tinygit                   the same project, promoted, no owner in the url
```

promotion is a real alias, not a redirect: git accepts it as a clone url, and an
alias may not shadow an existing user or top level org.

### one repository list for everybody

`/` lists every project the viewer may read, across every user and org, with the
owner path in front. `/explore.json` is the same list as json. user pages fold
their projects into folders, and a project without a folder falls into
`unsorted` rather than disappearing.

## model

```
users ──┬── projects ──┬── branch_groups ── branches
        │              ├── path_rules
        │              ├── project_tags ── dependencies
        │              └── promotions (the /@alias)
        └── folders ── folder_items

orgs (parent_id, any depth) ── projects, folders, org_members
```

roles are `reader < writer < maintainer < owner`, inherited down the org chain,
so a suborg team does not need a row per project. two privileges are separate on
purpose: seeing private branches needs any membership, seeing private paths
needs maintainer. pushes land in the real repository, so they need maintainer
too.

## urls

| url | what |
| --- | --- |
| `/` | every repository the viewer may read |
| `/explore.json` | the same, as json |
| `/u/<login>` | a user's preview, folded into folders |
| `/o/<org>/<sub>` | an org or suborg page |
| `/<owner path>/<project>` | project page: groups, tags, files |
| `/<owner path>/<project>.git` | git smart http |
| `/<owner path>/<project>/manifest.json` | machine readable metadata |
| `/@<alias>` | a promoted project or suborg |

## api

json in, json out, bearer token. `POST /api/users` bootstraps the first user and
returns its token; after that only an admin can add people.

```
POST /api/projects                       {owner, slug, description, visibility}
POST /api/projects/<owner>/<project>/tags             {tag} or {tags: [...]}
POST /api/projects/<owner>/<project>/dependencies     {name, kind, version, url}
POST /api/projects/<owner>/<project>/branch-groups    {slug, semantics}
POST /api/projects/<owner>/<project>/branches         {name, group, visibility}
POST /api/projects/<owner>/<project>/path-rules       {pattern, scope}
POST /api/projects/<owner>/<project>/folders          {slug, title}
POST /api/promote                        {owner, project, alias}
GET  /api/projects/<owner>/<project>/manifest
GET  /api/catalog
```

the same operations exist on the command line, which is how you would seed a
real instance.

## limits, stated plainly

- a clone that had paths stripped is read only. its history is not the real
  history, so pushing from it would need a force push that the server has every
  reason to refuse. clone with a maintainer token to write.
- tags in a filtered view are dropped when they point into a hidden history.
- the rewrite is memoised per viewer policy and cached on disk; the first fetch
  of a long history with rules costs a tree walk. that is the price of path
  privacy in git.
- no web hooks, no issues, no merge requests. this is the forge core: who may
  see what, and where it lives.

## layout

```
forge/
  db.py          sqlite schema
  paths.py       glob rules and subtree skipping
  access.py      roles, visibility, what a viewer may fetch
  repos.py       users, orgs, projects, folders, promotions
  branches.py    branch groups, semantic tags, path rules
  metatags.py    tag vocabulary, dependencies, manifest
  catalog.py     the cross user repository list
  gitview.py     bare repos, filtered views, smart http
  urls.py        path resolution
  web.py         wsgi app: html, json, git
  cli.py         seed, serve, inspect
  tests/         67 tests, no network beyond loopback
docs/spec.md     requirement by requirement
```

## tests

```
python3 -m forge.tests.run_tests
```

the git tests start a real wsgi server on loopback and clone from it, because
the claim being tested is that an ordinary `git clone` cannot see a private path
or a private branch.
