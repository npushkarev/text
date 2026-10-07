import argparse
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location(
    "copy_sources", Path(__file__).resolve().parents[1] / "bitbucket-copy-sources.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def git(*args, cwd=None):
    return subprocess.check_output(["git", *args], cwd=cwd, universal_newlines=True,
                                   stderr=subprocess.PIPE).strip()


class FakeBitbucket:
    def __init__(self, source, target):
        self.source = source
        self.target = target
        self.calls = []
        self.default = ""
        self.fail_default = False
        self.bad_default = False
        self.race = None
        self.target_checks = 0

    def info(self, path, slug):
        return {"slug": slug, "project": {"key": "SU2"},
                "links": {"clone": [{"name": "http", "href": str(path)}]}}

    def request(self, path, method="GET", body=None, missing=False):
        self.calls.append((path, method, body))
        if path == "/projects/SU2":
            return {"key": "SU2", "name": "СУРА2"}
        if path == "/projects/SU2/repos/cs_doc":
            return self.info(self.source, "cs_doc")
        if path == "/projects/SU2/repos/cs_doc/branches/default":
            return {"id": "refs/heads/master"}
        if path == "/projects/SU2/repos/as_doc":
            self.target_checks += 1
            if self.race and self.target_checks == 2:
                self.race()
            return self.info(self.target, "as_doc") if self.target.exists() else None
        if path == "/projects/SU2/repos" and method == "POST":
            assert body == {"name": "as_doc", "scmId": "git", "forkable": True}
            git("init", "--bare", str(self.target))
            return self.info(self.target, "as_doc")
        if path == "/projects/SU2/repos/as_doc/branches/default":
            if method == "PUT":
                if self.fail_default:
                    raise RuntimeError("HTTP 403: default branch denied")
                self.default = body["id"]
                return None
            return {"id": "refs/heads/master" if self.bad_default else self.default}
        raise AssertionError("Unexpected REST request: " + method + " " + path)


class CopySourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = mock.patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "Local Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Local Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.source = self.root / "cs_doc"
        self.target = self.root / "as_doc.git"
        git("init", str(self.source))
        git("symbolic-ref", "HEAD", "refs/heads/master", cwd=self.source)
        (self.source / "README.md").write_text("old contents\n")
        git("add", "README.md", cwd=self.source)
        git("commit", "-m", "old history", cwd=self.source)
        (self.source / "README.md").write_text("current contents\n")
        (self.source / ".gitattributes").write_text("secret.txt export-ignore\n*.dat -text\n")
        (self.source / "secret.txt").write_text("also part of the source tree\n")
        (self.source / ".hidden").write_text("hidden\n")
        (self.source / "data.dat").write_bytes(b"\x00\xff\r\n\x80binary\n")
        (self.source / "run.sh").write_text("#!/bin/sh\nexit 0\n")
        (self.source / "run.sh").chmod(0o755)
        (self.source / "link").symlink_to("README.md")
        git("add", "README.md", ".gitattributes", "secret.txt", ".hidden", "data.dat", "run.sh", "link", cwd=self.source)
        git("commit", "-m", "current source", cwd=self.source)
        self.args = argparse.Namespace(
            project="SU2", source="cs_doc", target="as_doc", source_branch="",
            first_branch="master", second_branch="develop", default_branch="develop",
            git_name="Local Test", git_email="test@example.invalid", apply=True)
        self.api = FakeBitbucket(self.source, self.target)
        self.output = io.StringIO()
        self.errors = io.StringIO()
        original_mkdtemp = tempfile.mkdtemp
        patched = mock.patch.object(module.tempfile, "mkdtemp",
                                    side_effect=lambda **kwargs: original_mkdtemp(dir=self.root, **kwargs))
        patched.start()
        self.addCleanup(patched.stop)

    def run_copy(self):
        with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.errors):
            module.copy_sources(self.args, self.api)

    def writes(self):
        return [call for call in self.api.calls if call[1] != "GET"]

    def assert_snapshot(self):
        source_tree = git("rev-parse", "HEAD^{tree}", cwd=self.source)
        target_tree = git("--git-dir=" + str(self.target), "rev-parse", "master^{tree}")
        self.assertEqual(source_tree, target_tree)
        self.assertEqual(git("--git-dir=" + str(self.target), "rev-list", "--count", "master"), "1")
        self.assertEqual(git("--git-dir=" + str(self.target), "rev-parse", "master"),
                         git("--git-dir=" + str(self.target), "rev-parse", "develop"))
        old_commit = git("rev-parse", "HEAD~1", cwd=self.source)
        with self.assertRaises(subprocess.CalledProcessError):
            git("--git-dir=" + str(self.target), "cat-file", "-e", old_commit)

    def test_create_exact_snapshot_without_history(self):
        self.run_copy()
        self.assert_snapshot()
        self.assertEqual(self.api.default, "refs/heads/develop")
        self.assertIn("ГОТОВО", self.output.getvalue())
        self.assertEqual([call[1] for call in self.writes()], ["POST", "PUT"])
        self.assertEqual(git("rev-list", "--count", "HEAD", cwd=self.source), "2")

    def test_dry_run_does_not_create_remote(self):
        self.args.apply = False
        self.run_copy()
        self.assertFalse(self.target.exists())
        self.assertEqual(self.writes(), [])
        self.assertIn("DRY RUN", self.output.getvalue())

    def test_existing_empty_target_is_initialized(self):
        git("init", "--bare", str(self.target))
        self.run_copy()
        self.assert_snapshot()
        self.assertEqual([call[1] for call in self.writes()], ["PUT"])

    def seed_target(self):
        if not self.target.exists():
            git("init", "--bare", str(self.target))
        git("push", str(self.target), "HEAD:refs/heads/existing", cwd=self.source)

    def test_nonempty_target_is_not_changed(self):
        self.seed_target()
        before = git("ls-remote", "--refs", str(self.target))
        with self.assertRaisesRegex(RuntimeError, "уже содержит"):
            self.run_copy()
        self.assertEqual(self.writes(), [])
        self.assertEqual(git("ls-remote", "--refs", str(self.target)), before)

    def test_target_becomes_nonempty_during_preparation(self):
        self.api.race = self.seed_target
        with self.assertRaisesRegex(RuntimeError, "Push остановлен"):
            self.run_copy()
        self.assertEqual(self.writes(), [])
        self.assertIn("refs/heads/existing", git("ls-remote", "--refs", str(self.target)))
        self.assertNotIn("ГОТОВО", self.output.getvalue())

    def test_default_branch_failure_preserves_pushed_sources(self):
        self.api.fail_default = True
        with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
            self.run_copy()
        self.assert_snapshot()
        self.assertIn("выбор ветки по умолчанию", self.errors.getvalue())
        self.assertNotIn("ГОТОВО", self.output.getvalue())

    def test_verification_mismatch_not_reported_as_success(self):
        self.api.bad_default = True
        with self.assertRaisesRegex(RuntimeError, "не соответствует"):
            self.run_copy()
        self.assertNotIn("ГОТОВО", self.output.getvalue())

    def test_lfs_is_rejected_before_mutation(self):
        (self.source / ".gitattributes").write_text("*.dat filter=lfs diff=lfs merge=lfs -text\n")
        git("add", ".gitattributes", cwd=self.source)
        git("commit", "-m", "lfs attributes", cwd=self.source)
        with self.assertRaisesRegex(RuntimeError, "Git LFS"):
            self.run_copy()
        self.assertFalse(self.target.exists())
        self.assertEqual(self.writes(), [])

    def test_submodule_is_rejected_before_mutation(self):
        commit = git("rev-parse", "HEAD", cwd=self.source)
        git("update-index", "--add", "--cacheinfo", "160000," + commit + ",dependency", cwd=self.source)
        git("commit", "-m", "submodule entry", cwd=self.source)
        with self.assertRaisesRegex(RuntimeError, "submodule"):
            self.run_copy()
        self.assertFalse(self.target.exists())
        self.assertEqual(self.writes(), [])

    def test_explicit_source_branch(self):
        git("branch", "release/docs", "HEAD~1", cwd=self.source)
        self.args.source_branch = "release/docs"
        self.run_copy()
        self.assertEqual(git("--git-dir=" + str(self.target), "rev-parse", "master^{tree}"),
                         git("rev-parse", "release/docs^{tree}", cwd=self.source))
        self.assertNotIn("/projects/SU2/repos/cs_doc/branches/default", [c[0] for c in self.api.calls])

    def test_invalid_configuration_does_not_contact_rest(self):
        for changes in ({"target": "cs_doc"}, {"target": "Bad Name"},
                        {"second_branch": "master"}, {"second_branch": "master/sub"},
                        {"default_branch": "missing"}):
            with self.subTest(changes=changes):
                args = argparse.Namespace(**vars(self.args))
                for key, value in changes.items():
                    setattr(args, key, value)
                with self.assertRaises(RuntimeError):
                    module.copy_sources(args, self.api)
                self.assertEqual(self.api.calls, [])


if __name__ == "__main__":
    unittest.main()
