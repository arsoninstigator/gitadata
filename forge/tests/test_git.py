"""what a client can actually fetch.

these tests speak the real smart http protocol against a real server, because
the whole point of the branch is that hiding a path or a branch survives an
ordinary `git clone`.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from .. import access, branches, repos
from .helpers import Forge, cleanup, clone, live_server, pkt_lines, run, tempdir


class FetchVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempdir()
        self.forge = Forge(self.tmp)
        self.conn = self.forge.conn
        self.anne = self.forge.user("anne")
        self.project = self.forge.project("anne", "demo")
        self.sha = self.forge.push(
            self.project,
            {
                "README.md": "# demo\n",
                "src/app.py": "print('hi')\n",
                "secrets/key.pem": "PRIVATE KEY\n",
                "secrets/notes.md": "do not publish\n",
            },
            branch="main",
            message="seed",
        )
        self.forge.push(
            self.project,
            {"README.md": "# demo\n", "internal/plan.md": "branch only\n"},
            branch="notes",
            message="private branch",
        )
        branches.ensure_branch(self.conn, self.project["id"], "notes", None, "private")
        branches.add_path_rule(self.conn, self.project["id"], "secrets/**", "**", "keys stay local")

    def tearDown(self):
        self.conn.close()
        cleanup(self.tmp)

    def test_private_branch_is_not_advertised(self):
        with live_server(self.forge) as base:
            out = run(["ls-remote", f"{base}/anne/demo.git"])
        self.assertIn("refs/heads/main", out)
        self.assertNotIn("refs/heads/notes", out)

    def test_private_branch_is_advertised_to_a_maintainer(self):
        with live_server(self.forge) as base:
            out = run(
                [
                    "-c",
                    f"http.extraHeader=Authorization: Bearer {self.anne['token']}",
                    "ls-remote",
                    f"{base}/anne/demo.git",
                ]
            )
        self.assertIn("refs/heads/main", out)
        self.assertIn("refs/heads/notes", out)

    def test_anonymous_clone_has_no_private_paths(self):
        dest = Path(self.tmp) / "clone-anon"
        with live_server(self.forge) as base:
            clone(f"{base}/anne/demo.git", dest)
        self.assertTrue((dest / "README.md").exists())
        self.assertTrue((dest / "src" / "app.py").exists())
        self.assertFalse((dest / "secrets").exists())
        files = run(["ls-files"], cwd=dest).split()
        self.assertNotIn("secrets/key.pem", files)

    def test_anonymous_clone_rewrites_history_so_the_tree_hash_changes(self):
        dest = Path(self.tmp) / "clone-hash"
        with live_server(self.forge) as base:
            clone(f"{base}/anne/demo.git", dest)
        head = run(["rev-parse", "HEAD"], cwd=dest).strip()
        self.assertNotEqual(head, self.sha)
        message = run(["log", "-1", "--format=%s"], cwd=dest).strip()
        self.assertEqual(message, "seed")

    def test_anonymous_clone_does_not_get_the_private_branch(self):
        dest = Path(self.tmp) / "clone-branches"
        with live_server(self.forge) as base:
            clone(f"{base}/anne/demo.git", dest)
        refs = run(["branch", "-a"], cwd=dest)
        self.assertIn("main", refs)
        self.assertNotIn("notes", refs)

    def test_maintainer_clone_keeps_everything(self):
        dest = Path(self.tmp) / "clone-anne"
        with live_server(self.forge) as base:
            clone(
                f"{base}/anne/demo.git",
                dest,
                "-c",
                f"http.extraHeader=Authorization: Bearer {self.anne['token']}",
            )
        self.assertTrue((dest / "secrets" / "key.pem").exists())
        head = run(["rev-parse", "HEAD"], cwd=dest).strip()
        self.assertEqual(head, self.sha)

    def test_push_without_a_token_is_refused(self):
        with live_server(self.forge) as base:
            out = Path(self.tmp) / "push-anon"
            clone(f"{base}/anne/demo.git", out)
            (out / "README.md").write_text("# changed\n")
            run(["config", "user.email", "x@y.z"], cwd=out)
            run(["config", "user.name", "x"], cwd=out)
            run(["commit", "-q", "-am", "edits"], cwd=out)
            output = run(["push", "origin", "HEAD:main"], cwd=out, check=False)
        self.assertRegex(output.lower(), r"403|refused|denied|forbidden")
        self.assertRegex(output, r"\[exit 1")

    def test_private_repository_is_invisible(self):
        private = self.forge.project("anne", "vault", visibility="private")
        self.forge.push(private, {"a.txt": "a\n"})
        with live_server(self.forge) as base:
            status, _ = self.forge.json("/anne/vault.git/info/refs?service=git-upload-pack")
        self.assertEqual(status, 404)

    def test_advertisement_is_pkt_line_framed(self):
        self.forge.push(self.forge.project("anne", "plain"), {"a.txt": "a\n"})
        status, headers, payload = self.forge.call(
            "/anne/plain.git/info/refs", query="service=git-upload-pack"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/x-git-upload-pack-advertisement")
        lines = pkt_lines(payload)
        self.assertEqual(lines[0], "# service=git-upload-pack")
        self.assertTrue(any(line.startswith("# service=git-upload-pack") for line in lines))

    def test_a_clean_clone_keeps_the_original_commit_ids(self):
        clean = self.forge.project("anne", "clean")
        sha = self.forge.push(clean, {"a.txt": "a\n", "b.txt": "b\n"})
        dest = Path(self.tmp) / "clone-clean"
        with live_server(self.forge) as base:
            clone(f"{base}/anne/clean.git", dest)
        self.assertEqual(run(["rev-parse", "HEAD"], cwd=dest).strip(), sha)

    def test_promoted_alias_is_clonable(self):
        repos.promote(self.conn, project_id=self.project["id"], alias="demo")
        dest = Path(self.tmp) / "clone-alias"
        with live_server(self.forge) as base:
            clone(f"{base}/@demo", dest)
        self.assertTrue((dest / "README.md").exists())
        self.assertFalse((dest / "secrets").exists())

    def test_path_rule_scope_limits_which_branches_are_filtered(self):
        project = self.forge.project("anne", "scoped")
        self.forge.push(project, {"drafts/a.md": "draft\n", "b.txt": "b\n"}, branch="main")
        self.forge.push(project, {"drafts/a.md": "draft\n", "b.txt": "b\n"}, branch="release-1")
        branches.add_path_rule(self.conn, project["id"], "drafts/**", "release-*", "drafts on releases")
        anon = access.Viewer.anonymous()
        refs = self.forge.store.refs(project["id"])["branches"]
        visible = access.visible_branch_names(self.conn, anon, project, refs)
        view = self.forge.store.view_for(
            project["id"],
            {name: (sha, access.private_patterns(self.conn, project["id"], name)) for name, sha in refs.items() if name in visible},
        )
        release_tree = run(["-C", str(view), "ls-tree", "-r", "--name-only", "release-1"])
        main_tree = run(["-C", str(view), "ls-tree", "-r", "--name-only", "main"])
        self.assertNotIn("drafts/a.md", release_tree)
        self.assertIn("drafts/a.md", main_tree)


if __name__ == "__main__":
    unittest.main()
