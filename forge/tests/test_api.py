"""the json api: bootstrap, authorization, and metadata that refuses to lie."""

from __future__ import annotations

import unittest

from .. import metatags, repos
from .helpers import Forge, cleanup, tempdir


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempdir()
        self.forge = Forge(self.tmp)
        self.conn = self.forge.conn

    def tearDown(self):
        self.conn.close()
        cleanup(self.tmp)

    def bootstrap(self, login="anne", admin=True):
        status, payload = self.forge.json("/api/users", "POST", {"login": login, "is_admin": admin})
        self.assertEqual(status, 201, payload)
        return payload["token"]

    def test_first_user_can_bootstrap_anonymously(self):
        token = self.bootstrap()
        self.assertTrue(token)

    def test_second_anonymous_signup_is_refused(self):
        self.bootstrap("anne")
        status, payload = self.forge.json("/api/users", "POST", {"login": "bo"})
        self.assertEqual(status, 403)

    def test_admin_can_add_a_user(self):
        token = self.bootstrap("anne", admin=True)
        status, payload = self.forge.json(
            "/api/users", "POST", {"login": "bo"}, token=token
        )
        self.assertEqual(status, 201, payload)

    def test_project_creation_under_own_login(self):
        token = self.bootstrap("anne")
        status, payload = self.forge.json(
            "/api/projects", "POST", {"owner": "anne", "slug": "notes"}, token=token
        )
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["path"], "anne/notes")

    def test_project_creation_in_someone_elses_namespace_is_refused(self):
        anne = self.bootstrap("anne", admin=False)
        bo = self.forge.user("bo")["token"]
        status, payload = self.forge.json(
            "/api/projects", "POST", {"owner": "anne", "slug": "steal"}, token=bo
        )
        self.assertEqual(status, 403, payload)

    def test_path_rules_need_a_maintainer(self):
        token = self.bootstrap("anne", admin=False)
        self.forge.project("anne", "notes")
        reader = self.forge.user("reader")
        repos.create_user(self.conn, "mallory")
        project = repos.find_project(self.conn, "anne", "notes")
        self.conn.execute(
            "insert into project_members (project_id, user_id, role) values (?,?,'reader')",
            (project["id"], reader["id"]),
        )
        self.conn.commit()
        status, payload = self.forge.json(
            "/api/projects/anne/notes/path-rules",
            "POST",
            {"pattern": "secrets/**"},
            token=reader["token"],
        )
        self.assertEqual(status, 403, payload)
        owner_token = token
        status, payload = self.forge.json(
            "/api/projects/anne/notes/path-rules",
            "POST",
            {"pattern": "secrets/**"},
            token=owner_token,
        )
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["rule"]["pattern"], "secrets/**")

    def test_self_hostable_tag_is_refused_until_dependencies_exist(self):
        token = self.bootstrap("anne", admin=True)
        self.forge.project("anne", "notes")
        status, payload = self.forge.json(
            "/api/projects/anne/notes/tags",
            "POST",
            {"tag": "self-hostable"},
            token=token,
        )
        self.assertEqual(status, 400, payload)
        status, payload = self.forge.json(
            "/api/projects/anne/notes/dependencies",
            "POST",
            {
                "name": "git",
                "kind": "runtime",
                "version": ">=2.30",
                "url": "https://git-scm.com",
            },
            token=token,
        )
        self.assertEqual(status, 201, payload)
        status, payload = self.forge.json(
            "/api/projects/anne/notes/tags", "POST", {"tag": "self-hostable"}, token=token
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["tags"], ["self-hostable"])

    def test_manifest_endpoint(self):
        token = self.bootstrap("anne", admin=True)
        project = self.forge.project("anne", "notes", description="scratch")
        metatags.add_dependency(
            self.conn, project["id"], "python3", "runtime", ">=3.9", "https://python.org", True, ""
        )
        metatags.add_tag(self.conn, project["id"], "self-hostable")
        status, payload = self.forge.json("/anne/notes/manifest.json")
        self.assertEqual(status, 200)
        self.assertEqual(payload["project"]["path"], "anne/notes")
        self.assertTrue(payload["self_hostable"])
        self.assertEqual(payload["dependencies"][0]["name"], "python3")

    def test_manifest_for_a_private_project_is_hidden(self):
        self.bootstrap("anne", admin=True)
        private = self.forge.project("anne", "vault", visibility="private")
        status, _ = self.forge.json("/anne/vault/manifest.json")
        self.assertEqual(status, 404)

    def test_promotion_via_api(self):
        token = self.bootstrap("anne", admin=True)
        project = self.forge.project("anne", "notes")
        status, payload = self.forge.json(
            "/api/promote",
            "POST",
            {"owner": "anne", "project": "notes", "alias": "notes-now"},
            token=token,
        )
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["url"], "/@notes-now")
        status, _, page = self.forge.call("/@notes-now")
        self.assertEqual(status, 200)

    def test_branch_group_semantics_are_validated_over_http(self):
        token = self.bootstrap("anne", admin=True)
        self.forge.project("anne", "notes")
        status, payload = self.forge.json(
            "/api/projects/anne/notes/branch-groups",
            "POST",
            {"slug": "release", "semantics": {"channel": "release"}},
            token=token,
        )
        self.assertEqual(status, 400, payload)
        status, payload = self.forge.json(
            "/api/projects/anne/notes/branch-groups",
            "POST",
            {"slug": "release", "semantics": {"channel": "release", "stability": "stable"}},
            token=token,
        )
        self.assertEqual(status, 201, payload)

    def test_explore_json_lists_every_owner(self):
        self.bootstrap("anne", admin=True)
        self.forge.project("anne", "a")
        self.forge.project("bo", "b") if False else None
        from .. import repos as repo_mod

        repo_mod.create_user(self.conn, "bo")
        self.forge.project("bo", "b")
        status, payload = self.forge.json("/explore.json")
        self.assertEqual(status, 200)
        self.assertEqual(payload["schema"], "forge.catalog/v1")
        self.assertEqual(
            sorted(p["path"] for p in payload["projects"]), ["anne/a", "bo/b"]
        )

    def test_unknown_api_route_is_404(self):
        status, _ = self.forge.json("/api/nothing/here")
        self.assertEqual(status, 404)

    def test_html_pages_render(self):
        self.bootstrap("anne", admin=True)
        self.forge.project("anne", "notes", description="scratch notes")
        for path in ("/", "/u/anne", "/anne/notes"):
            status, headers, body = self.forge.call(path)
            self.assertEqual(status, 200, path)
            self.assertIn("text/html", headers["content-type"])
            self.assertIn(b"gitadata", body)

    def test_org_page_lists_suborgs(self):
        self.bootstrap("anne", admin=True)
        repos.create_org(self.conn, "stack", "", "stack", "")
        repos.create_org(self.conn, "labs", "stack", "labs", "")
        status, headers, body = self.forge.call("/o/stack")
        self.assertEqual(status, 200)
        self.assertIn(b"labs", body)


if __name__ == "__main__":
    unittest.main()
