"""the cross-user list, folders and per-viewer filtering."""

from __future__ import annotations

import unittest

from .. import access, catalog, metatags, repos
from ..access import Viewer
from .helpers import Forge, cleanup, tempdir


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempdir()
        self.forge = Forge(self.tmp)
        self.conn = self.forge.conn
        self.anne_user = self.forge.user("anne")
        self.bo_user = self.forge.user("bo")
        self.forge.org("stack")
        self.anne_notes = self.forge.project("anne", "notes", description="scratch")
        self.bo_thing = self.forge.project("bo", "thing")
        self.shared = self.forge.project("stack", "shared")
        self.secret = self.forge.project("bo", "diary", visibility="private")
        self.anon = Viewer.anonymous()

    def tearDown(self):
        self.conn.close()
        cleanup(self.tmp)

    def paths(self, viewer):
        return [e["path"] for e in catalog.entries(self.conn, self.forge.store, viewer)]

    def test_list_spans_every_owner(self):
        found = self.paths(self.anon)
        self.assertIn("anne/notes", found)
        self.assertIn("bo/thing", found)
        self.assertIn("stack/shared", found)
        self.assertEqual(len(found), 3)

    def test_private_project_is_not_listed_anonymously(self):
        self.assertNotIn("bo/diary", self.paths(self.anon))

    def test_private_project_is_listed_for_its_owner(self):
        viewer = Viewer(user_id=self.bo_user["id"], login="bo")
        self.assertIn("bo/diary", self.paths(viewer))

    def test_stats_count_owners(self):
        stats = catalog.stats(self.conn, self.forge.store, self.anon)
        self.assertEqual(stats["projects"], 3)
        self.assertEqual(stats["owners"], 3)
        self.assertTrue(stats["multi_user"])

    def test_tag_filter(self):
        metatags.add_dependency(self.conn, self.anne_notes["id"], "git", "runtime", "any", "https://git-scm.com", True, "")
        metatags.add_tag(self.conn, self.anne_notes["id"], "self-hostable")
        tagged = catalog.entries(self.conn, self.forge.store, self.anon, tag="self-hostable")
        self.assertEqual([e["path"] for e in tagged], ["anne/notes"])

    def test_dependency_index_for_self_hostable_projects(self):
        metatags.add_dependency(self.conn, self.anne_notes["id"], "git", "runtime", ">=2.30", "https://git-scm.com", True, "")
        metatags.add_tag(self.conn, self.anne_notes["id"], "self-hostable")
        index = catalog.dependency_index(self.conn, self.anon)
        self.assertEqual(len(index), 1)
        self.assertEqual(index[0]["path"], "anne/notes")
        self.assertEqual(index[0]["dependencies"][0]["version"], ">=2.30")

    def test_folders_group_a_users_preview(self):
        folder = repos.create_folder(self.conn, "anne", "tools", "tools", 0)
        repos.add_to_folder(self.conn, folder["id"], self.anne_notes["id"])
        items = catalog.entries(self.conn, self.forge.store, self.anon, owner_path="anne")
        groups = repos.grouped_projects(self.conn, "anne", items)
        self.assertEqual([g["slug"] for g in groups], ["tools"])
        self.assertEqual(groups[0]["projects"][0]["slug"], "notes")

    def test_ungrouped_projects_land_in_unsorted(self):
        items = catalog.entries(self.conn, self.forge.store, self.anon, owner_path="bo")
        groups = repos.grouped_projects(self.conn, "bo", items)
        self.assertEqual(groups[0]["slug"], "unsorted")
        self.assertTrue(groups[0]["is_default"])

    def test_owner_filter_keeps_the_namespace(self):
        self.assertEqual(self.paths_for_owner("bo"), ["bo/thing"])

    def paths_for_owner(self, owner):
        return [
            e["path"]
            for e in catalog.entries(self.conn, self.forge.store, self.anon, owner_path=owner)
        ]

    def test_member_of_org_inherits_read_on_its_projects(self):
        conn = self.conn
        org = repos.resolve_org(conn, "stack")
        conn.execute(
            "insert into org_members (org_id, user_id, role) values (?,?,'reader')",
            (org["id"], self.anne_user["id"]),
        )
        conn.commit()
        project = repos.find_project(conn, "stack", "shared")
        viewer = Viewer(user_id=self.anne_user["id"], login="anne")
        self.assertEqual(access.project_role(conn, viewer, project), "reader")
        self.assertTrue(access.can_read(conn, viewer, project))
        self.assertFalse(access.can_push(conn, viewer, project))

    def test_org_role_inherits_down_a_suborg(self):
        conn = self.conn
        parent = repos.resolve_org(conn, "stack")
        repos.create_org(conn, "labs", "stack", "labs", "")
        project = self.forge.project("stack/labs", "tiny")
        conn.execute(
            "insert into org_members (org_id, user_id, role) values (?,?,'maintainer')",
            (parent["id"], self.bo_user["id"]),
        )
        conn.commit()
        viewer = Viewer(user_id=self.bo_user["id"], login="bo")
        self.assertEqual(access.project_role(conn, viewer, project), "maintainer")

    def test_folder_collision_refused(self):
        repos.create_folder(self.conn, "anne", "tools", "tools", 0)
        with self.assertRaises(repos.Conflict):
            repos.create_folder(self.conn, "anne", "tools", "tools", 1)


if __name__ == "__main__":
    unittest.main()
