"""units: glob rules, semantic tags, metadata tags, url resolution."""

from __future__ import annotations

import unittest

from .. import branches, metatags, paths, repos, urls
from ..access import Viewer
from ..repos import Conflict, ForgeError, NotFound
from .helpers import Forge, cleanup, tempdir


class PathRuleTests(unittest.TestCase):
    def test_star_does_not_cross_directories(self):
        self.assertTrue(paths.matches("notes.txt", "*.txt"))
        self.assertFalse(paths.matches("deep/notes.txt", "*.txt"))

    def test_double_star_crosses_directories(self):
        self.assertTrue(paths.matches("deep/notes.txt", "**/*.txt"))
        self.assertTrue(paths.matches("notes.txt", "**/*.txt"))
        self.assertTrue(paths.matches("secrets/keys/a.pem", "secrets/**"))
        self.assertFalse(paths.matches("public/a.pem", "secrets/**"))

    def test_question_mark_is_one_character(self):
        self.assertTrue(paths.matches("a1", "a?"))
        self.assertFalse(paths.matches("a10", "a?"))

    def test_matches_any(self):
        self.assertTrue(paths.matches_any("x/secret.pem", ["nothing", "x/*.pem"]))
        self.assertFalse(paths.matches_any("y/public.pem", ["nothing", "x/*.pem"]))

    def test_static_prefix_and_subtree_skipping(self):
        self.assertEqual(paths.static_prefix("secrets/keys/**"), "secrets/keys")
        self.assertEqual(paths.static_prefix("**/*.key"), "")
        self.assertTrue(paths.could_match_under("secrets/**", "secrets"))
        self.assertTrue(paths.could_match_under("secrets/**", "secrets/deep"))
        self.assertFalse(paths.could_match_under("secrets/**", "src"))
        self.assertFalse(paths.could_match_under("a/b/c.txt", "a/z"))
        # a wildcard at the root can match anything below it
        self.assertTrue(paths.could_match_under("**/*.key", "anything/at/all"))


class SemanticsTests(unittest.TestCase):
    def test_valid_group_semantics(self):
        clean = branches.validate_semantics(
            {"channel": "release", "stability": "stable", "semver": "1.4.0"}
        )
        self.assertEqual(clean["semver"], "1.4.0")

    def test_release_needs_stability(self):
        with self.assertRaises(ForgeError):
            branches.validate_semantics({"channel": "release"})

    def test_unknown_channel_rejected(self):
        with self.assertRaises(ForgeError):
            branches.validate_semantics({"channel": "sidequest", "stability": "stable"})

    def test_bad_semver_rejected(self):
        with self.assertRaises(ForgeError):
            branches.validate_semantics({"semver": "v1.4"})

    def test_json_string_accepted(self):
        self.assertEqual(branches.validate_semantics('{"stability":"beta"}'), {"stability": "beta"})

    def test_uppercase_key_rejected(self):
        with self.assertRaises(ForgeError):
            branches.validate_semantics({"Channel": "release"})


class MetadataTagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempdir()
        self.forge = Forge(self.tmp)
        self.forge.user("anne")
        self.project = self.forge.project("anne", "notes")

    def tearDown(self):
        self.forge.conn.close()
        cleanup(self.tmp)

    def test_unknown_tag_is_refused_with_the_vocabulary(self):
        with self.assertRaises(ForgeError) as ctx:
            metatags.add_tag(self.forge.conn, self.project["id"], "made-up")
        self.assertIn("self-hostable", str(ctx.exception))

    def test_self_hostable_needs_a_declared_dependency(self):
        with self.assertRaises(ForgeError) as ctx:
            metatags.add_tag(self.forge.conn, self.project["id"], "self-hostable")
        self.assertIn("dependency", str(ctx.exception))

    def test_dependency_needs_kind_version_and_url(self):
        conn = self.forge.conn
        metatags.add_dependency(conn, self.project["id"], "git", "runtime", "any", "", True, "")
        with self.assertRaises(ForgeError) as ctx:
            metatags.set_tags(conn, self.project["id"], ["self-hostable"])
        self.assertIn("url", str(ctx.exception))

    def test_self_hostable_passes_with_a_complete_dependency(self):
        conn = self.forge.conn
        metatags.add_dependency(
            conn, self.project["id"], "git", "runtime", ">=2.30", "https://git-scm.com", True, ""
        )
        tags = metatags.add_tag(conn, self.project["id"], "self-hostable")
        self.assertEqual(tags, ["self-hostable"])
        manifest = metatags.manifest(conn, self.project, "")
        self.assertTrue(manifest["self_hostable"])
        self.assertEqual(manifest["dependencies"][0]["name"], "git")
        self.assertEqual(manifest["dependencies"][0]["kind"], "runtime")

    def test_bad_dependency_kind_refused(self):
        with self.assertRaises(ForgeError):
            metatags.add_dependency(
                self.forge.conn, self.project["id"], "git", "teleporter", "any", "u", True, ""
            )

    def test_dropping_a_dependency_breaks_the_tag_and_is_caught(self):
        conn = self.forge.conn
        metatags.add_dependency(conn, self.project["id"], "git", "runtime", "any", "https://git-scm.com", True, "")
        metatags.add_tag(conn, self.project["id"], "self-hostable")
        metatags.remove_dependency(conn, self.project["id"], "git")
        with self.assertRaises(ForgeError):
            metatags.validate(conn, self.project["id"])

    def test_manifest_lists_branch_semantics(self):
        conn = self.forge.conn
        group = branches.create_group(
            conn, self.project["id"], "release", "release", {"channel": "release", "stability": "stable"}
        )
        branches.ensure_branch(conn, self.project["id"], "main", group["id"], "inherit", {"semver": "1.0.0"})
        branches.ensure_branch(conn, self.project["id"], "internal", None, "private")
        manifest = metatags.manifest(conn, self.project, "")
        self.assertEqual(manifest["schema"], "forge.manifest/v1")
        self.assertEqual(manifest["branches"]["private_branches"], ["internal"])
        group_payload = manifest["branches"]["groups"][0]
        self.assertEqual(group_payload["semantics"]["channel"], "release")
        self.assertEqual(group_payload["branches"][0]["semantics"]["semver"], "1.0.0")


class UrlResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempdir()
        self.forge = Forge(self.tmp)
        conn = self.forge.conn
        self.forge.user("anne")
        self.forge.user("bo")
        stack = repos.create_org(conn, "stack", "", "stack", "")
        repos.create_org(conn, "labs", "stack", "labs", "")
        self.anne = self.forge.project("anne", "notes")
        self.tool = self.forge.project("stack/labs", "tinygit")

    def tearDown(self):
        self.forge.conn.close()
        cleanup(self.tmp)

    def test_plain_path(self):
        route = urls.resolve(self.forge.conn, "/anne/notes")
        self.assertEqual(route["kind"], "project")
        self.assertEqual(route["project"]["slug"], "notes")

    def test_nested_org_path(self):
        route = urls.resolve(self.forge.conn, "/stack/labs/tinygit")
        self.assertEqual(route["kind"], "project")
        self.assertEqual(route["project"]["id"], self.tool["id"])

    def test_git_path_suffix(self):
        route = urls.resolve(self.forge.conn, "/stack/labs/tinygit.git/info/refs")
        self.assertEqual(route["kind"], "git")
        self.assertEqual(route["service"], "info/refs")

    def test_org_page_and_suborg_page_are_separate_urls(self):
        parent = urls.resolve(self.forge.conn, "/o/stack")
        child = urls.resolve(self.forge.conn, "/o/stack/labs")
        self.assertEqual(parent["kind"], "org")
        self.assertEqual(child["kind"], "org")
        self.assertEqual(repos.org_path(self.forge.conn, child["org"]), "stack/labs")

    def test_promotion_gives_a_bare_url(self):
        conn = self.forge.conn
        repos.promote(conn, project_id=self.tool["id"], alias="tinygit")
        route = urls.resolve(conn, "/@tinygit")
        self.assertEqual(route["kind"], "project")
        self.assertEqual(route["project"]["id"], self.tool["id"])
        self.assertEqual(route["alias"], "tinygit")
        git_route = urls.resolve(conn, "/@tinygit/info/refs")
        self.assertEqual(git_route["kind"], "git")

    def test_promoted_suborg_can_be_opened_on_its_own(self):
        conn = self.forge.conn
        org = repos.resolve_org(conn, "stack/labs")
        repos.promote(conn, org_id=org["id"], alias="lab")
        route = urls.resolve(conn, "/@lab")
        self.assertEqual(route["kind"], "org")
        self.assertEqual(repos.org_path(conn, route["org"]), "stack/labs")

    def test_alias_cannot_shadow_a_user(self):
        with self.assertRaises(Conflict):
            repos.promote(self.forge.conn, project_id=self.tool["id"], alias="anne")

    def test_alias_is_unique(self):
        conn = self.forge.conn
        repos.promote(conn, project_id=self.tool["id"], alias="tinygit")
        with self.assertRaises(Conflict):
            repos.promote(conn, project_id=self.anne["id"], alias="tinygit")

    def test_unknown_alias(self):
        with self.assertRaises(NotFound):
            urls.resolve(self.forge.conn, "/@nope")

    def test_user_page(self):
        route = urls.resolve(self.forge.conn, "/u/anne")
        self.assertEqual(route["kind"], "user")
        self.assertEqual(route["user"]["login"], "anne")


if __name__ == "__main__":
    unittest.main()
