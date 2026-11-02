"""Offline tests for the bounded exporter and Git publication lifecycle."""
from contextlib import redirect_stderr
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "sync.py"
SPEC = importlib.util.spec_from_file_location("learning_sync", MODULE)
sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sync
SPEC.loader.exec_module(sync)


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.repo = self.base / "publication"
        self.state = self.base / "state"
        self.remote = self.base / "remote.git"
        self.source.mkdir()
        self.repo.mkdir()
        self.state.mkdir()
        self.css = self.base / "style.css"
        self.css.write_text(".learning { color: inherit; }\n")
        (self.source / "README.md").write_text("# 学习项目\n\n[[01 起步|从这里开始]]\n")
        (self.source / "01 起步.md").write_text('---\ntype: "concept"\n---\n\n## 路线 A：通用多模态\n\n内容。\n')
        self.config_path = self.base / "config.json"
        self.config_path.write_text(json.dumps({
            "source_dir": str(self.source), "css_source": str(self.css),
            "repo_dir": str(self.repo), "state_dir": str(self.state),
            "remote_url": str(self.remote), "branch": "main",
            "poll_seconds": 0.01, "debounce_seconds": 0, "retry_seconds": 0.01,
        }))
        self.cfg = sync.Config.read(self.config_path)

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, cwd=None, check=True):
        result = subprocess.run(["git", *args], cwd=cwd or self.repo, capture_output=True, text=True)
        if check and result.returncode:
            self.fail("Git fixture setup failed: " + result.stderr)
        return result

    def init_git(self):
        self.git("init", "--bare", str(self.remote), cwd=self.base)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Publisher Test")
        self.git("config", "user.email", "publisher@example.invalid")
        self.git("remote", "add", "origin", str(self.remote))

    def count(self):
        return self.git("rev-list", "--count", "HEAD").stdout.strip()

    def test_public_links_titles_canvas_and_private_references(self):
        (self.source / "README.md").write_text('---\ntype: "moc"\nstatus: "待学习"\n---\n\n# 学习项目\n\n[[01 起步#路线 A：通用多模态|开始]]\n\n| [[01 起步\\|章节]] |\n\n> [!agent-chapter-nav]\n> [[01 起步|上一章]]\n>\n> [[01 起步|下一章]]\n')
        (self.source / "09 Retrieval 与 RAG.md").write_text('参考：[公开](https://example.org/)。已有学习入口：[[private-secret-note|私人笔记]]。\n')
        (self.source / "24 多模态与实时语音.md").write_text('正文。\n\n已有笔记入口：[[private-voice-project]]。\n\n## 保留\n内容\n')
        (self.source / "92 学习工作台与资料.md").write_text('''## 状态
```base
filters:
  and:
    - 'file.inFolder("Agent 应用开发")'
    - 'note.type == "concept"'
```
> path:"Agent 应用开发/" task-todo:/验收/

## 关联已有笔记
[[private-one]]
## 从已有知识中找案例
-path:"Agent 应用开发/"
## 官方资料
保留。[[unknown-note|普通标签]]
''')
        (self.source / "入口.canvas").write_text(json.dumps({"nodes": [{"id": "one", "type": "text", "text": "[[Agent 应用开发/README|入口]]"}], "edges": []}))
        before = {p.name: p.read_bytes() for p in self.source.iterdir()}
        changed = sync.export(self.cfg)
        readme = (self.repo / "README.md").read_text()
        self.assertIn("01%20%E8%B5%B7%E6%AD%A5.md#%E8%B7%AF%E7%BA%BF-a%E9%80%9A%E7%94%A8%E5%A4%9A%E6%A8%A1%E6%80%81", readme)
        self.assertIn("[章节](01%20", readme)
        self.assertTrue(readme.startswith("# 学习项目"))
        self.assertNotIn('type: "moc"', readme)
        self.assertNotIn("[!agent-chapter-nav]", readme)
        self.assertIn("| :--- | ---: |", readme)
        chapter = (self.repo / "01 起步.md").read_text()
        self.assertTrue(chapter.startswith('---\ntype: "concept"\n---\n'))
        self.assertIn("# 01 起步", chapter)
        self.assertEqual(readme.count("# 学习项目"), 1)
        rag = (self.repo / "09 Retrieval 与 RAG.md").read_text()
        self.assertIn("https://example.org/", rag)
        self.assertNotIn("private-secret", rag)
        self.assertNotIn("private-voice", (self.repo / "24 多模态与实时语音.md").read_text())
        workspace = (self.repo / "92 学习工作台与资料.md").read_text()
        self.assertNotIn("private-one", workspace)
        self.assertNotIn("从已有知识", workspace)
        self.assertNotIn("file.inFolder", workspace)
        self.assertNotIn('path:"Agent 应用开发/"', workspace)
        self.assertIn("普通标签", workspace)
        self.assertNotIn("[[unknown-note", workspace)
        self.assertEqual(json.loads((self.repo / "入口.canvas").read_text())["nodes"][0]["text"], "[[README|入口]]")
        self.assertIn(sync.CSS_TARGET, changed)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.source.iterdir()})

    def test_missing_empty_and_symlink_sources_do_not_delete_exports(self):
        sync.export(self.cfg)
        original = (self.repo / "README.md").read_bytes()
        (self.source / "README.md").write_text("")
        with self.assertRaises(sync.SyncError):
            sync.export(self.cfg)
        self.assertEqual(original, (self.repo / "README.md").read_bytes())
        (self.source / "README.md").unlink()
        (self.source / "01 起步.md").unlink()
        with self.assertRaises(sync.SyncError):
            sync.export(self.cfg)
        (self.source / "bad.md").symlink_to(self.css)
        with self.assertRaises(sync.SyncError):
            sync.export(self.cfg)
        self.assertTrue((self.repo / "01 起步.md").exists())

    def test_only_manifest_files_are_deleted_and_local_edits_are_protected(self):
        sync.export(self.cfg)
        (self.repo / "unmanaged.md").write_text("Keep this unrelated repository file.\n")
        (self.source / "01 起步.md").unlink()
        sync.export(self.cfg)
        self.assertFalse((self.repo / "01 起步.md").exists())
        self.assertTrue((self.repo / "unmanaged.md").exists())
        (self.repo / "README.md").write_text("Local unpublished edit.\n")
        with self.assertRaisesRegex(sync.SyncError, "local edits"):
            sync.export(self.cfg)
        self.assertEqual((self.repo / "README.md").read_text(), "Local unpublished edit.\n")

    def test_first_export_preserves_existing_files_and_accepts_identical_output(self):
        (self.repo / "README.md").write_text("Preexisting local introduction.\n")
        with self.assertRaisesRegex(sync.SyncError, "not managed"):
            sync.export(self.cfg)
        self.assertEqual((self.repo / "README.md").read_text(), "Preexisting local introduction.\n")
        self.assertFalse((self.repo / "01 起步.md").exists())
        rendered, _ = sync.render(sync.capture(self.cfg))
        (self.repo / "README.md").write_bytes(rendered["README.md"])
        sync.export(self.cfg)
        self.assertTrue((self.repo / "01 起步.md").exists())

    def test_tampered_manifest_and_symlink_destination_are_rejected(self):
        sync.export(self.cfg)
        manifest = sync.load_json(self.state / "manifest.json")
        manifest["files"]["../outside.txt"] = "bad"
        (self.state / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(sync.SyncError, "out-of-scope"):
            sync.export(self.cfg)
        manifest["files"].pop("../outside.txt")
        (self.state / "manifest.json").write_text(json.dumps(manifest))
        (self.repo / "README.md").unlink()
        (self.repo / "README.md").symlink_to(self.source / "README.md")
        with self.assertRaisesRegex(sync.SyncError, "symlink"):
            sync.export(self.cfg)

    def test_git_once_push_noop_and_deletion(self):
        self.init_git()
        with sync.publisher_lock(self.cfg):
            first = sync.sync_once(self.cfg)
        self.assertEqual(first["status"], "synced")
        self.assertEqual(self.count(), "1")
        pushed = self.git("--git-dir", str(self.remote), "rev-parse", "main", cwd=self.base).stdout.strip()
        self.assertEqual(pushed, first["last_pushed_commit"])
        before = (self.state / "status.json").read_text()
        second = sync.sync_once(self.cfg)
        self.assertEqual(second["status"], "no_changes")
        self.assertEqual(self.count(), "1")
        self.assertEqual(second["last_push"], first["last_push"])
        (self.source / "01 起步.md").unlink()
        sync.sync_once(self.cfg)
        self.assertEqual(self.count(), "2")
        tree = self.git("ls-tree", "-r", "--name-only", "HEAD").stdout
        self.assertNotIn("01 起步.md", tree)

    def test_file_removed_between_export_and_first_commit_is_not_staged(self):
        sync.export(self.cfg)
        (self.source / "01 起步.md").unlink()
        self.init_git()
        sync.sync_once(self.cfg)
        self.assertEqual(self.count(), "1")
        tree = self.git("ls-tree", "-r", "--name-only", "HEAD").stdout
        self.assertNotIn("01 起步.md", tree)

    def test_push_failure_retains_commit_then_retries_without_duplicate_commit(self):
        self.init_git()
        sync.sync_once(self.cfg)
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        (self.source / "01 起步.md").write_text("变更后的内容。\n")
        with self.assertRaisesRegex(sync.SyncError, "Git push failed"):
            sync.sync_once(self.cfg)
        self.assertEqual(self.count(), "2")
        self.assertEqual(sync.load_json(self.state / "status.json")["status"], "error")
        hook.unlink()
        result = sync.sync_once(self.cfg)
        self.assertEqual(result["status"], "synced")
        self.assertEqual(self.count(), "2")

    def test_unexpected_staging_wrong_remote_branch_and_merge_are_blocked(self):
        self.init_git()
        sync.sync_once(self.cfg)
        (self.repo / "private.txt").write_text("Unrelated unpublished content.\n")
        self.git("add", "private.txt")
        with self.assertRaisesRegex(sync.SyncError, "Unexpected staged"):
            sync.sync_once(self.cfg)
        self.git("reset", "--", "private.txt")
        self.git("remote", "set-url", "origin", str(self.base / "other.git"))
        with self.assertRaisesRegex(sync.SyncError, "Origin"):
            sync.sync_once(self.cfg)
        self.git("remote", "set-url", "origin", str(self.remote))
        self.git("remote", "set-url", "--push", "origin", str(self.base / "unapproved.git"))
        with self.assertRaisesRegex(sync.SyncError, "push destination"):
            sync.sync_once(self.cfg)
        self.git("remote", "set-url", "--push", "origin", str(self.remote))
        self.git("remote", "set-url", "--push", "--add", "origin", str(self.base / "second-destination.git"))
        with self.assertRaisesRegex(sync.SyncError, "push destination"):
            sync.sync_once(self.cfg)
        self.git("config", "--unset-all", "remote.origin.pushurl")
        self.git("checkout", "-b", "other")
        with self.assertRaisesRegex(sync.SyncError, "branch"):
            sync.sync_once(self.cfg)
        self.git("checkout", "main")
        (self.repo / ".git" / "MERGE_HEAD").write_text(self.git("rev-parse", "HEAD").stdout)
        with self.assertRaisesRegex(sync.SyncError, "unfinished"):
            sync.sync_once(self.cfg)

    def test_lock_and_external_config(self):
        with sync.publisher_lock(self.cfg):
            with self.assertRaisesRegex(sync.SyncError, "already running"):
                with sync.publisher_lock(self.cfg):
                    self.fail("Lock was not enforced")
        unsafe = self.repo / "config.json"
        unsafe.write_bytes(self.config_path.read_bytes())
        with self.assertRaisesRegex(sync.SyncError, "outside"):
            sync.Config.read(unsafe)

    def test_credentials_blocked_without_exposure(self):
        secret = "ghp_" + "A" * 40
        (self.source / "01 起步.md").write_text(secret)
        with self.assertRaises(sync.SyncError) as caught:
            sync.export(self.cfg)
        self.assertNotIn(secret, str(caught.exception))
        self.assertFalse((self.repo / "README.md").exists())

    def test_staged_secret_is_rejected_even_when_worktree_is_clean(self):
        self.init_git()
        sync.sync_once(self.cfg)
        secret = "ghp_" + "B" * 40
        (self.repo / "README.md").write_text(secret)
        self.git("add", "README.md")
        # Restore the exported worktree without changing the staged index blob.
        expected, _ = sync.render(sync.capture(self.cfg))
        (self.repo / "README.md").write_bytes(expected["README.md"])
        with self.assertRaisesRegex(sync.SyncError, "staged content") as caught:
            sync.sync_once(self.cfg)
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(self.count(), "1")


if __name__ == "__main__":
    unittest.main()
