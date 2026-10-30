"""Offline release integrity and source isolation; no global plugin installation."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import governance_package as package
sys.path.pop(0)


class GovernancePackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "community"
        self.source.mkdir()
        self.output = self.root / "release"
        for name in package.REQUIRED_FILES:
            self.put(name, "# resource\n")
        self.put("plugin.json", json.dumps({"name": package.PLUGIN_NAME, "version": "0.1.0-dev.1"}))
        self.put("docs/需求版本管理.md", "# Empty ledger\n| RequirementId | Status |\n|---|---|\n")
        self.git("init", "--quiet")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "fixture")

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.source), *args], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.decode("utf-8").strip()

    def put(self, name, value):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value.encode("utf-8") if isinstance(value, str) else value)

    def build(self, **kwargs):
        return package.build_package(self.source, self.output, **kwargs)

    def manifest(self):
        return json.loads((self.output / "release.json").read_text(encoding="utf-8"))

    def write_manifest(self, value):
        (self.output / "release.json").write_text(json.dumps(value), encoding="utf-8")

    def test_clean_release_captures_commit_and_raw_bytes(self):
        result = self.build()
        self.assertTrue(result["Valid"])
        self.assertFalse(result["SourceDirty"])
        self.assertEqual(result["SourceCommit"], self.git("rev-parse", "HEAD"))
        self.assertEqual(result, package.verify_package(self.output))
        self.assertEqual((self.output / "AGENTS.md").read_bytes(), (self.source / "AGENTS.md").read_bytes())

    def test_dirty_candidate_requires_explicit_flag(self):
        self.put("AGENTS.md", "# changed\r\n")
        with self.assertRaisesRegex(ValueError, "allow_dirty"):
            self.build()
        self.assertFalse(self.output.exists())
        result = self.build(allow_dirty=True)
        self.assertTrue(result["SourceDirty"])
        self.assertEqual((self.output / "AGENTS.md").read_bytes(), b"# changed\r\n")

    def test_inherited_git_redirects_and_traces_are_not_used(self):
        with mock.patch.dict(os.environ, {"GIT_DIR": str(self.source / "missing"), "GIT_WORK_TREE": str(self.root), "GIT_TRACE": str(self.root / "trace") }):
            result = self.build()
        self.assertTrue(result["Valid"])
        self.assertFalse((self.root / "trace").exists())

    def test_no_overwrite_and_no_output_inside_source(self):
        self.output.mkdir()
        sentinel = self.output / "mine.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.build()
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        with self.assertRaises(ValueError):
            package.build_package(self.source, self.source / "dist", allow_dirty=True)

    def test_sibling_output_with_dot_components_is_normalized(self):
        result = package.build_package(self.source, self.source / ".." / "release")
        self.assertEqual(result["PackageDigest"], package.verify_package(self.output)["PackageDigest"])

    def test_source_facts_secrets_tests_examples_and_git_are_excluded(self):
        self.put("docs/requirements/REQ-DEV-20261028-01/context.md", "private business")
        self.put(".env", "private environment")
        self.put("scripts/tests/test_extra.py", "private fixture")
        self.put("examples/private.json", "private example")
        files = self.build(allow_dirty=True)["Files"]
        self.assertFalse(any(name.startswith(("docs/requirements/", "examples/", ".git/", "scripts/tests/")) for name in files))
        self.assertNotIn(".env", files)

    def test_missing_runtime_asset_fails_before_creating_output(self):
        (self.source / "scripts/governance_tools.py").unlink()
        with self.assertRaisesRegex(ValueError, "required"):
            self.build(allow_dirty=True)
        self.assertFalse(self.output.exists())

    def test_unexpected_file_in_packaged_directory_fails(self):
        self.put("docs/Agent治理/private.bin", b"private")
        with self.assertRaisesRegex(ValueError, "unexpected"):
            self.build(allow_dirty=True)

    def test_sensitive_concrete_assignment_and_ledger_facts_are_rejected(self):
        self.put("AGENTS.md", "api_key: " + "sample-concrete-value\n")
        with self.assertRaisesRegex(ValueError, "secret"):
            self.build(allow_dirty=True)
        self.put("AGENTS.md", "# normal\n")
        self.put("docs/需求版本管理.md", "| REQ-DEV-20261028-01 | completed |\n")
        with self.assertRaisesRegex(ValueError, "empty template"):
            self.build(allow_dirty=True)

    def test_tampered_bytes_extra_files_and_missing_files_are_rejected(self):
        self.build()
        file = self.output / "AGENTS.md"
        original = file.read_bytes()
        file.write_bytes(original.replace(b"\n", b"\r\n"))
        with self.assertRaisesRegex(ValueError, "hash"):
            package.verify_package(self.output)
        file.write_bytes(original)
        extra = self.output / "extra.txt"
        extra.write_text("extra", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "inventory"):
            package.verify_package(self.output)
        extra.unlink()
        file.unlink()
        with self.assertRaisesRegex(ValueError, "inventory"):
            package.verify_package(self.output)

    def test_digest_is_manifest_canonical_not_json_whitespace(self):
        result = self.build()
        value = self.manifest()
        self.write_manifest(value)
        self.assertEqual(package.verify_package(self.output)["PackageDigest"], result["PackageDigest"])

    def test_duplicate_manifest_fields_and_wrong_plugin_identity_fail(self):
        self.build()
        original = (self.output / "release.json").read_bytes()
        (self.output / "release.json").write_bytes(original[:-2] + b',"PluginName":"another"}\n')
        with self.assertRaisesRegex(ValueError, "duplicate"):
            package.verify_package(self.output)
        (self.output / "release.json").write_bytes(original)
        value = self.manifest()
        value["PluginVersion"] = "9.0.0"
        self.write_manifest(value)
        with self.assertRaisesRegex(ValueError, "identity"):
            package.verify_package(self.output)

    def test_traversal_case_collision_and_sensitive_paths_fail(self):
        self.build()
        original = self.manifest()
        for name in ("../outside.txt", "C:/outside.txt", "scripts/../secret.py", "docs/Agent治理/.env", "AGENTS.MD"):
            with self.subTest(name=name):
                value = json.loads(json.dumps(original))
                value["Files"][name] = "0" * 64
                self.write_manifest(value)
                with self.assertRaises(ValueError):
                    package.verify_package(self.output)

    def test_symlink_source_or_release_is_rejected(self):
        target = self.root / "outside.md"
        target.write_text("outside", encoding="utf-8")
        link = self.source / "docs/Agent治理/link.md"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("symbolic link privilege unavailable")
        with self.assertRaisesRegex(ValueError, "linked"):
            self.build(allow_dirty=True)
        link.unlink()
        self.build()
        linked = self.root / "linked-release"
        linked.symlink_to(self.output, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "linked"):
            package.verify_package(linked)

    def test_source_state_change_during_capture_is_rejected(self):
        commit = self.git("rev-parse", "HEAD")
        with mock.patch.object(package, "source_state", side_effect=[(commit, False), ("1" * 40, False)]):
            with self.assertRaisesRegex(ValueError, "changed"):
                self.build()
        self.assertFalse(self.output.exists())

    def test_relocated_package_verifies_without_source_repository(self):
        first = self.build()
        relocated = self.root / "a different project" / "offline-release"
        relocated.parent.mkdir()
        shutil.move(str(self.output), relocated)
        # Windows Git object files may be read-only; making the original source
        # location unavailable proves independence without deleting Git objects.
        self.source.rename(self.root / "source-unavailable")
        self.assertEqual(package.verify_package(relocated)["PackageDigest"], first["PackageDigest"])

    def test_cli_validation_returns_nonzero_on_corruption(self):
        self.build()
        script = Path(package.__file__)
        result = subprocess.run([sys.executable, str(script), "verify", "--package", str(self.output)], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.output / "AGENTS.md").write_text("corrupt", encoding="utf-8")
        result = subprocess.run([sys.executable, str(script), "verify", "--package", str(self.output)], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)["Valid"])


if __name__ == "__main__":
    unittest.main()
