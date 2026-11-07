# spec: what was asked for, and where it lives

written against the original brief, one requirement per section. each section
says where the behaviour is implemented, how it is tested, and what it does not
cover.

## 1. an orphan branch called `gitadata`, holding a lightweight git client

`git checkout --orphan gitadata` and a fresh root: no file from `main` is
carried over. the branch is a forge, not a wrapper around one.

implementation: `forge/` package, standard library only plus the `git` binary.
`forge/web.py` is one wsgi callable, `forge/cli.py` seeds and serves it.

test: the suite runs against a temp data directory, so `git log --oneline main`
shows nothing on this branch.

not covered: hosting, tls termination, backups. those are deployment choices the
server stays out of.

## 2. a clone of gitlab or gitea

- repositories with public and private visibility (`projects.visibility`)
- users and organisations as owners, orgs nestable to any depth (`orgs.parent_id`)
- roles reader, writer, maintainer, owner, inherited down the org chain
  (`access.project_role`)
- git smart http for clone and push (`gitview.advertise_refs`, `gitview.rpc`)
- web pages for the repository list, a user, an org, a project and its files
- json for the list and for each project's metadata

test: `test_git.py` clones through a real server; `test_api.py` covers the pages
and the json; `test_catalog.py` covers visibility.

not covered: issues, merge requests, ci. the brief asked for a client, and the
core of a client is who may see what.

## 3. private branches

`branches.visibility` is `inherit`, `public` or `private`. effective privacy is
the branch, or its group when the branch inherits.

enforcement happens at the ref advertisement, not in the ui: a viewer who may
not see a branch never receives its name or its objects, and `upload-pack`
running against a view cannot serve unadvertised objects because
`uploadpack.allowAnySHA1InWant` is off.

test: `test_private_branch_is_not_advertised` (anonymous `git ls-remote`),
`test_private_branch_is_advertised_to_a_maintainer`, and
`test_anonymous_clone_does_not_get_the_private_branch`.

## 4. branch groupings with semantic tags

`branch_groups` (slug, title, semantics, visibility) and `branches.group_id`.
semantics are a validated json object: `channel` and `stability` are closed
vocabularies, `semver` is checked against a pattern, every other key must be a
lowercase scalar. `channel=release` requires a `stability` tag.

branches inherit their group's semantics and may override keys
(`access.branch_semantics`).

test: `SemanticsTests` for the vocabulary, `test_manifest_lists_branch_semantics`
for inheritance, `test_branch_group_semantics_are_validated_over_http` for the
api path.

## 5. a public branch where some files in a directory are private

`path_rules(project_id, pattern, scope_glob)`. patterns are posix globs with
`**`; the scope is a branch glob, so a rule applies to `**` (every branch),
`release-*`, or any other pattern.

serving: for a viewer below maintainer, `GitStore.view_for` builds a bare repo
where each branch's history is rewritten with that branch's rules
(`_Rehydrator`): private blobs are dropped, emptied directories removed, commits
rebuilt with the same author, committer, dates and message, and the object store
linked to the real one by `objects/info/alternates`. views are cached by content
hash, so a repeat clone costs nothing.

a branch with nothing to prune keeps its original commit ids, which is asserted
in `test_a_clean_clone_keeps_the_original_commit_ids`.

test: `test_anonymous_clone_has_no_private_paths`,
`test_anonymous_clone_rewrites_history_so_the_tree_hash_changes`,
`test_maintainer_clone_keeps_everything`,
`test_path_rule_scope_limits_which_branches_are_filtered`.

not covered, deliberately: writes from a filtered clone. its history is not the
real history, so a push would need a force push the server refuses. a maintainer
clone is the write path, and this is documented in the readme rather than hidden.

## 6. repositories natively listed for more than two users

`catalog.entries(conn, store, viewer)` walks every project in the instance,
keeps the ones the viewer may read, and returns the owner path with each entry.
no namespace parameter is required, so the default listing is cross user.

`catalog.stats` reports the project and owner counts. `/` renders it,
`/explore.json` serialises it, `/api/catalog` is the same behind the api.

test: `test_list_spans_every_owner` (three owners), `test_stats_count_owners`,
`test_explore_json_lists_every_owner`.

## 7. folder separated repository groups in every user's preview

`folders` and `folder_items`, owned by a user or an org.
`repos.grouped_projects` folds a page's projects into those folders and puts the
rest in an `unsorted` bucket, so no repository silently vanishes from a preview.

test: `test_folders_group_a_users_preview`,
`test_ungrouped_projects_land_in_unsorted`, `test_folder_collision_refused`.

## 8. metadata tags, with metatagged dependencies

`project_tags` against a closed vocabulary in `repos.TAG_VOCAB`.
`metatags.validate` enforces the consequences: `self-hostable` needs at least
one dependency, each dependency needs a kind from `DEP_KINDS`, a version and a
url. the check runs on tagging, on `set_tags`, and is available standalone so a
cron job can re-validate an instance after a dependency is deleted.

the machine readable form is pre-made, not generated per request into a file:
`/manifest.json` on every project, `/api/projects/<owner>/<project>/manifest`,
`/api/dependencies` for the whole instance, and `forge.manifest.json` in this
repository as an example a client can read.

test: `MetadataTagTests` (six cases), `test_self_hostable_tag_is_refused_until_dependencies_exist`,
`test_dependency_index_for_self_hostable_projects`.

## 9. nested orgs and projects, promotable to /@projectname

`orgs.parent_id` gives arbitrary nesting; `repos.org_path` and
`repos.resolve_org` walk it. `/o/stack/labs` opens the suborg on its own page,
`/stack/labs/tinygit` is the project's canonical path.

`promotions(alias, kind, project_id, org_id)` promotes either a project or a
suborg to `/@alias`. the alias is a real route: it renders the project page and
it is a clone url (`git clone http://host/@tinygit`). promotion refuses an alias
that would shadow an existing user or top level org, and refuses duplicates.

test: `UrlResolutionTests` (promotion, suborg promotion, shadowing, duplicates,
clone route), `test_promoted_alias_is_clonable`, `test_promotion_via_api`.

## open decisions

- tokens are bearer tokens issued by the api or the cli. wiring an external
  identity provider is a deployment concern and was not in the brief.
- view cache pruning is a manual call (`GitStore.prune_views`) plus mtime based
  cleanup, not a background job.
