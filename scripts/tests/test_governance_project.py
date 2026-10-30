"""Pinned project lifecycle in isolated synthetic repositories, including real launchers."""

import contextlib
import io
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
import governance_project as project
sys.path.pop(0)


class GovernanceProjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory()
        cls.source_package = Path(cls.shared.name) / "release"
        cls.first = package.build_package(Path(__file__).resolve().parents[2], cls.source_package, allow_dirty=True)

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "项目 with space"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Synthetic Project")
        self.git("config", "user.email", "project@test.invalid")
        self.git("config", "core.autocrlf", "false")
        self.write("AGENTS.md", b"# Company instructions\r\nKeep project boundaries.\r\n")
        self.commit()
        self.req = "REQ-FIXTURE-20261028-01"

    def git(self, *args):
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        result = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "synthetic project fixture")

    def write(self, name, data):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def install(self, source=None):
        plan = project.preview_project(self.repo, source or self.source_package)
        self.assertTrue(plan["Applicable"], plan["Conflicts"])
        return project.apply_project(self.repo, plan)

    def changed_release(self, changes=None, version="0.1.0-dev.999"):
        output = self.root / version
        shutil.copytree(self.source_package, output)
        manifest = json.loads((output / "release.json").read_text(encoding="utf-8"))
        plugin = json.loads((output / "plugin.json").read_text(encoding="utf-8"))
        plugin["version"] = version
        updates = {"plugin.json": project.canonical(plugin), **(changes or {})}
        for name, data in updates.items():
            path = output / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            manifest["Files"][name] = project.digest(data)
        manifest["PluginVersion"] = version
        (output / "release.json").write_bytes(project.canonical(manifest))
        verified = package.verify_package(output)
        return output, verified

    def call(self, *args, script=".governance/run.py"):
        return subprocess.run([sys.executable, "-B", str(self.repo / script), *args], cwd=self.root,
                              capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})

    def test_fresh_init_preserves_agents_project_facts_and_is_idempotent(self):
        original = (self.repo / "AGENTS.md").read_bytes()
        self.write("src/business.txt", b"company business remains private")
        report = self.install()
        self.assertTrue((self.repo / "AGENTS.md").read_bytes().startswith(original))
        self.assertEqual((self.repo / "src/business.txt").read_bytes(), b"company business remains private")
        self.assertIn(self.first["PackageDigest"], report["RetainedReleases"])
        before = {str(p.relative_to(self.repo)): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.repo.rglob("*") if p.is_file() and ".git" not in p.parts}
        self.assertEqual(self.install()["WrittenPaths"], [])
        after = {str(p.relative_to(self.repo)): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.repo.rglob("*") if p.is_file() and ".git" not in p.parts}
        self.assertEqual(before, after)
        lock_text = (self.repo / project.LOCK).read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), lock_text)
        self.assertNotIn("TargetRepo", lock_text)
        self.assertEqual(project.project_config(self.repo)["LocalRuntimeDirectory"], ".governance/local")
        self.write(".governance/local/synthetic-report.json", b"local observation")
        self.assertNotIn(b"synthetic-report", self.git("status", "--porcelain=v1", "--untracked-files=all"))

    def test_runtime_ignore_preserves_project_patterns_and_rejects_custom_block(self):
        original = b"# Company patterns\r\nnode_modules/\r\n"
        self.write(".gitignore", original)
        self.install()
        self.assertTrue((self.repo / ".gitignore").read_bytes().startswith(original))
        self.assertEqual(self.install()["WrittenPaths"], [])
        self.write(".gitignore", (self.repo / ".gitignore").read_bytes().replace(b"/.governance/local/", b"/src/"))
        with self.assertRaisesRegex(ValueError, "runtime ignore block"):
            project.preview_project(self.repo, self.source_package)

    def test_unmanaged_different_file_conflicts_identical_file_is_adopted(self):
        name = ".codex/agents/writer.toml"
        self.write(name, b'company = "keep"\n')
        plan = project.preview_project(self.repo, self.source_package)
        self.assertIn(name, plan["Conflicts"])
        with self.assertRaises(ValueError):
            project.apply_project(self.repo, plan)
        self.assertEqual((self.repo / name).read_bytes(), b'company = "keep"\n')
        self.write(name, (self.source_package / name).read_bytes())
        self.assertTrue(project.preview_project(self.repo, self.source_package)["Applicable"])

    def test_explicit_preservation_keeps_legacy_files_and_uses_pinned_runner(self):
        name = "scripts/governance_tools.py"
        self.write(name, b'print("legacy project script")\n')
        original = (self.repo / name).read_bytes()
        plan = project.preview_project(self.repo, self.source_package, preserve_existing=True)
        self.assertTrue(plan["Applicable"], plan["Conflicts"])
        self.assertEqual(next(x for x in plan["Files"] if x["Path"] == name)["Action"], "PRESERVE_UNMANAGED")
        project.apply_project(self.repo, plan)
        self.assertEqual((self.repo / name).read_bytes(), original)
        self.assertNotIn(name, project.load_lock(self.repo)["ManagedFiles"])
        result = self.call("governance_tools", "check", "--repo", str(self.repo))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("legacy project script", result.stdout)
        self.assertEqual(project.apply_project(self.repo, project.preview_project(self.repo, self.source_package))["WrittenPaths"], [])

    def test_upgrade_updates_managed_baseline_and_retains_old_release(self):
        self.install()
        self.commit()
        name = "docs/工具说明/规则组合.md"
        updated = (self.source_package / name).read_bytes() + "\n升级说明。\n".encode("utf-8")
        incoming, verified = self.changed_release({name: updated})
        self.install(incoming)
        self.assertEqual((self.repo / name).read_bytes(), updated)
        lock = project.load_lock(self.repo)
        self.assertEqual(lock["ActivePackageDigest"], verified["PackageDigest"])
        self.assertEqual(set(lock["Releases"]), {verified["PackageDigest"], self.first["PackageDigest"]})
        self.assertTrue((self.repo / ".governance/releases" / self.first["PackageDigest"]).is_dir())

    def test_local_customization_preserved_when_upstream_unchanged(self):
        self.install()
        self.commit()
        name = ".codex/agents/writer.toml"
        customized = (self.repo / name).read_bytes() + b"\n# Project customization\n"
        self.write(name, customized)
        incoming, _ = self.changed_release()
        self.install(incoming)
        self.assertEqual((self.repo / name).read_bytes(), customized)

    def test_both_changed_conflict_and_uncommitted_baseline_protection(self):
        self.install()
        name = ".codex/agents/writer.toml"
        updated = (self.repo / name).read_bytes() + b"\n# Upstream change\n"
        incoming, _ = self.changed_release({name: updated})
        self.assertIn(name, project.preview_project(self.repo, incoming)["Conflicts"])
        self.commit()
        self.write(name, b"project customization\n")
        plan = project.preview_project(self.repo, incoming)
        self.assertIn(name, plan["Conflicts"])
        with self.assertRaises(ValueError):
            project.apply_project(self.repo, plan)
        self.assertEqual((self.repo / name).read_bytes(), b"project customization\n")

    def test_stale_preview_checks_files_configuration_and_git(self):
        plan = project.preview_project(self.repo, self.source_package)
        self.write("AGENTS.md", b"new company instructions\n")
        with self.assertRaisesRegex(ValueError, "stale"):
            project.apply_project(self.repo, plan)
        self.assertFalse((self.repo / project.LOCK).exists())
        self.install()
        plan = project.preview_project(self.repo, self.source_package)
        self.write(project.PROJECT, b'{"ProjectFormatVersion":"v1.0","RuleDeclaration":"rules.json"}')
        with self.assertRaisesRegex(ValueError, "stale"):
            project.apply_project(self.repo, plan)

    def test_preview_plan_cannot_inject_a_write_path(self):
        plan = project.preview_project(self.repo, self.source_package)
        plan["Files"].append({"Path": "src/business.txt", "Action": "WRITE"})
        with self.assertRaises(ValueError):
            project.apply_project(self.repo, plan)
        self.assertFalse((self.repo / "src/business.txt").exists())

    def test_apply_failure_rolls_back_own_files_and_preserves_agents(self):
        original = (self.repo / "AGENTS.md").read_bytes()
        plan = project.preview_project(self.repo, self.source_package)
        actual = project.write_atomic
        count = 0
        def failing(path, data):
            nonlocal count
            count += 1
            if count == 4:
                raise OSError("synthetic failure")
            return actual(path, data)
        with mock.patch.object(project, "write_atomic", side_effect=failing), self.assertRaises(OSError):
            project.apply_project(self.repo, plan)
        self.assertEqual((self.repo / "AGENTS.md").read_bytes(), original)
        self.assertFalse((self.repo / project.LOCK).exists())
        self.assertFalse((self.repo / ".governance/releases" / self.first["PackageDigest"]).exists())

    def test_lock_path_tampering_and_release_tampering_are_rejected(self):
        self.install()
        lock = project.load_lock(self.repo)
        lock["ManagedFiles"]["../outside"] = "a" * 64
        self.write(project.LOCK, project.canonical(lock))
        with self.assertRaises(ValueError):
            project.preview_project(self.repo, self.source_package)
        lock["ManagedFiles"].pop("../outside")
        self.write(project.LOCK, project.canonical(lock))
        self.write(".governance/releases/" + self.first["PackageDigest"] + "/scripts/governance_tools.py", b"raise SystemExit(77)\n")
        result = self.call("governance_tools", "check", "--repo", str(self.repo))
        self.assertEqual(result.returncode, 2)
        self.assertNotEqual(result.returncode, 77)

    def test_binding_is_immutable_and_existing_unbound_requirement_rejected(self):
        self.install()
        lock = project.load_lock(self.repo)
        first = project.binding(self.repo, lock, self.req, create=True)
        self.commit()
        incoming, _ = self.changed_release()
        self.install(incoming)
        second = project.binding(self.repo, project.load_lock(self.repo), self.req, create=True)
        self.assertEqual(first, second)
        other = "REQ-FIXTURE-20261028-02"
        self.write("docs/requirements/" + other + "/control-profile.json", b"{}")
        with self.assertRaisesRegex(ValueError, "existing unbound"):
            project.binding(self.repo, project.load_lock(self.repo), other, create=True)

    def test_actual_runner_checks_project_and_compatibility_launcher(self):
        self.install()
        result = self.call("governance_tools", "check", "--repo", str(self.repo))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mechanical checks only", result.stdout)
        result = self.call("check", "--repo", str(self.repo), script="scripts/governance_tools.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        bind = self.call("bind", "--requirement-id", self.req)
        self.assertEqual(bind.returncode, 0, bind.stderr)
        self.assertEqual(json.loads(bind.stdout)["PackageDigest"], self.first["PackageDigest"])
        result = self.call("governance_status", "--repo", str(self.repo), "--requirement-id", "REQ-FIXTURE-20261028-99")
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.repo / "docs/requirements/REQ-FIXTURE-20261028-99").exists())

    def test_actual_runner_routes_old_requirement_to_old_release(self):
        self.install()
        self.assertEqual(self.call("bind", "--requirement-id", self.req).returncode, 0)
        self.commit()
        # Only the new release is instrumented: receiving its output proves routing.
        code = b'import json; print(json.dumps({"Selected":"new"}))\n'
        incoming, verified = self.changed_release({"scripts/governance_status.py": code})
        self.install(incoming)
        old = self.call("governance_status", "--repo", str(self.repo), "--requirement-id", self.req)
        self.assertNotIn('"Selected": "new"', old.stdout)
        self.assertIn("UNDETERMINED", old.stdout)
        new_req = "REQ-FIXTURE-20261028-02"
        self.assertEqual(self.call("bind", "--requirement-id", new_req).returncode, 0)
        new = self.call("governance_status", "--repo", str(self.repo), "--requirement-id", new_req)
        self.assertEqual(new.returncode, 0, new.stderr)
        self.assertEqual(json.loads(new.stdout), {"Selected": "new"})
        self.assertEqual(project.binding(self.repo, project.load_lock(self.repo), new_req)["PackageDigest"], verified["PackageDigest"])

    def test_installed_b1_prepares_runs_and_cleans_local_test_resources(self):
        import test_governance_local_tests as fixtures
        case = fixtures.LocalTests(methodName="test_prepare_run_and_cleanup_share_port_data_and_leave_repository_unchanged")
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.install()
        requirement = case.requirement
        project.binding(self.repo, project.load_lock(self.repo), requirement, create=True)
        source = case.fixture.requirement_dir
        target = self.repo / "docs/requirements" / requirement
        shutil.copytree(source, target, dirs_exist_ok=True)
        for name in ("support.py", "governance.local-tests.json"):
            shutil.copy2(case.repo / name, self.repo / name)
        before = self.git("status", "--porcelain=v1", "-z", "--untracked-files=all")
        result = self.call("governance_local_tests", "--repo", str(self.repo), "--requirement-id", requirement, "--run")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["Cleanup"], "COMPLETED")
        self.assertTrue(all(item["Observation"]["Status"] == "SUCCEEDED" for item in report["Validations"]), report)
        self.assertFalse(report["QualityPassed"])
        case.assert_cleaned(report)
        self.assertEqual(before, self.git("status", "--porcelain=v1", "-z", "--untracked-files=all"))

    def test_runner_selector_is_consumed_and_must_match_real_inputs(self):
        self.install()
        self.assertEqual(self.call("bind", "--requirement-id", self.req).returncode, 0)
        result = self.call("governance_tools", "--for-requirement", self.req, "check", "--repo", str(self.repo))
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.call("governance_status", "--for-requirement", self.req, "--repo", str(self.repo), "--requirement-id", "REQ-FIXTURE-20261028-02")
        self.assertEqual(result.returncode, 2)
        with self.assertRaises(ValueError):
            project.requirement_from_args(self.repo, ["--control-profile", str(self.root / "other-project/control-profile.json")])

    def test_paths_multiple_requirements_and_other_repo_arguments_rejected(self):
        for name in ("../outside", "/outside", "C:/outside", ".git/../config", "file:stream"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                project.path_at(self.repo, name)
        with self.assertRaises(ValueError):
            project.requirement_from_args(self.repo, ["--requirement-id", self.req, "--requirement-dir", "docs/requirements/REQ-FIXTURE-20261028-02"])
        with self.assertRaises(ValueError):
            project.requirement_from_args(self.repo, ["--repo", str(self.root)])
        target = self.repo / "AGENTS.md"
        with mock.patch.object(Path, "is_symlink", lambda path: path == target), self.assertRaises(ValueError):
            project.preview_project(self.repo, self.source_package)

    def test_modified_managed_agents_block_is_not_overwritten(self):
        self.install()
        self.write("AGENTS.md", (self.repo / "AGENTS.md").read_bytes().replace(b"Development governance", b"Custom governance"))
        with self.assertRaisesRegex(ValueError, "AGENTS managed block"):
            project.preview_project(self.repo, self.source_package)

    def test_upgrade_does_not_restore_deleted_project_owned_files(self):
        self.install()
        (self.repo / "docs/需求版本管理.md").unlink()
        self.assertEqual(self.install()["WrittenPaths"], [])
        self.assertFalse((self.repo / "docs/需求版本管理.md").exists())
        (self.repo / "AGENTS.md").unlink()
        with self.assertRaisesRegex(ValueError, "was removed"):
            project.preview_project(self.repo, self.source_package)
        self.assertFalse((self.repo / "AGENTS.md").exists())

    def test_template_default_preview_is_explicit_and_readonly(self):
        self.install()
        before = {p.relative_to(self.repo).as_posix(): p.read_bytes() for p in self.repo.rglob("*") if p.is_file() and ".git" not in p.parts}
        result = self.call("template", "--kind", "baseline", "--default")
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertTrue(value["ReadOnly"])
        self.assertEqual(value["Selection"], "EXPLICIT_DEFAULT_PREVIEW")
        self.assertEqual((self.repo / value["ResourceRef"]).read_bytes(), (self.source_package / project.TEMPLATES["baseline"]).read_bytes())
        self.assertEqual(self.call("template", "--kind", "baseline").returncode, 2)
        self.assertEqual(before, {p.relative_to(self.repo).as_posix(): p.read_bytes() for p in self.repo.rglob("*") if p.is_file() and ".git" not in p.parts})

    def test_template_selector_keeps_old_requirement_after_default_upgrade(self):
        self.install()
        project.binding(self.repo, project.load_lock(self.repo), self.req, create=True)
        self.commit()
        name = project.TEMPLATES["baseline"]
        incoming, verified = self.changed_release({name: (self.source_package / name).read_bytes() + b"\nnew default template\n"})
        self.install(incoming)
        original = self.call("template", "--kind", "baseline", "--requirement-id", self.req)
        current = self.call("template", "--kind", "baseline", "--default")
        self.assertEqual(original.returncode, 0, original.stderr)
        self.assertEqual(json.loads(original.stdout)["PackageDigest"], self.first["PackageDigest"])
        self.assertEqual(json.loads(current.stdout)["PackageDigest"], verified["PackageDigest"])

    def test_template_does_not_adopt_unbound_history_or_guess_default(self):
        self.install()
        self.write("docs/requirements/" + self.req + "/需求基线.md", b"historical requirement\n")
        result = self.call("template", "--kind", "baseline", "--requirement-id", self.req)
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.repo / "docs/requirements" / self.req / "plugin-binding.json").exists())

    def test_release_only_layout_does_not_regrow_template_mirrors(self):
        self.install()
        config = project.project_config(self.repo)
        config["TemplateLayout"] = "RELEASE_ONLY"
        self.write(project.PROJECT, project.canonical(config) + b"\n")
        original = (self.repo / project.TEMPLATES["baseline"]).read_bytes()
        self.install()
        # Convergence is explicit; changing layout alone must not delete a file.
        self.assertEqual((self.repo / project.TEMPLATES["baseline"]).read_bytes(), original)
        for name in project.TEMPLATES.values():
            (self.repo / name).unlink()
        self.install()
        self.assertTrue(all(not (self.repo / name).exists() for name in project.TEMPLATES.values()))
        checked = self.call("governance_tools", "check", "--repo", str(self.repo))
        self.assertEqual(checked.returncode, 0, checked.stderr)
        for kind in project.TEMPLATES:
            result = self.call("template", "--kind", kind, "--default")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((self.repo / json.loads(result.stdout)["ResourceRef"]).is_file())

    def test_new_requirement_uses_release_template_without_root_mirror(self):
        self.install()
        config = project.project_config(self.repo)
        config["TemplateLayout"] = "RELEASE_ONLY"
        self.write(project.PROJECT, project.canonical(config) + b"\n")
        for name in project.TEMPLATES.values():
            (self.repo / name).unlink()
        self.install()
        enabled = self.call("governance_requirements", "enable", "--repo", str(self.repo))
        self.assertEqual(enabled.returncode, 0, enabled.stderr)
        created = self.call("governance_requirements", "init", "--repo", str(self.repo), "--requirement-id", self.req,
                            "--title", "synthetic requirement", "--instruction", "Implement the synthetic fixture.")
        self.assertEqual(created.returncode, 0, created.stderr)
        result = self.call("template", "--kind", "baseline", "--requirement-id", self.req)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["Selection"], "REQUIREMENT_BINDING")
        self.assertIn("Implement the synthetic fixture.", (self.repo / "docs/requirements" / self.req / "需求基线.md").read_text(encoding="utf-8"))

    def test_release_only_checker_resolves_template_anchor_but_not_missing_project_rule(self):
        self.install()
        config = project.project_config(self.repo)
        config["TemplateLayout"] = "RELEASE_ONLY"
        self.write(project.PROJECT, project.canonical(config) + b"\n")
        name = project.TEMPLATES["baseline"]
        (self.repo / name).unlink()
        agents = (self.repo / "AGENTS.md").read_bytes()
        self.write("AGENTS.md", agents + f"\n[template]({name}#填写规则)\n".encode("utf-8"))
        result = self.call("governance_tools", "check", "--repo", str(self.repo))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.write("AGENTS.md", (self.repo / "AGENTS.md").read_bytes() + b"\n[missing project rule](docs/company-missing.md)\n")
        rejected = self.call("governance_tools", "check", "--repo", str(self.repo))
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("local link", rejected.stderr)

    def test_template_ignores_local_copy_and_rejects_tampered_release(self):
        self.install()
        name = project.TEMPLATES["baseline"]
        self.write(name, b"project navigation, not a template\n")
        result = self.call("template", "--kind", "baseline", "--default")
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.repo / json.loads(result.stdout)["ResourceRef"]
        self.assertNotEqual(target.read_bytes(), (self.repo / name).read_bytes())
        target.write_bytes(target.read_bytes() + b"tampered\n")
        self.assertEqual(self.call("template", "--kind", "baseline", "--default").returncode, 2)

    def test_git_autocrlf_clone_preserves_release_and_managed_bytes(self):
        self.git("config", "core.autocrlf", "true")
        original = b"*.txt text\r\n# Company attributes\r\n"
        self.write(".gitattributes", original)
        self.install()
        self.assertTrue((self.repo / ".gitattributes").read_bytes().startswith(original))
        self.assertEqual(self.install()["WrittenPaths"], [])
        self.commit()
        clone = self.root / "fresh clone"
        result = subprocess.run(["git", "-c", "core.autocrlf=true", "clone", "-q", str(self.repo), str(clone)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

        lock = project.load_lock(clone)
        project.verify_release(clone, lock, lock["ActivePackageDigest"])
        self.assertTrue(project.preview_project(clone, self.source_package)["Applicable"])
        for name, pin in lock["ManagedFiles"].items():
            self.assertEqual(project.digest((clone / name).read_bytes()), pin, name)
        result = subprocess.run([sys.executable, "-B", str(clone / ".governance/run.py"), "governance_tools", "check", "--repo", str(clone)],
                                capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_nested_git_attributes_cannot_silently_renormalize_managed_files(self):
        self.write(".codex/.gitattributes", b"agents/*.toml text\n")
        plan = project.preview_project(self.repo, self.source_package)
        with self.assertRaisesRegex(ValueError, "attributes override"):
            project.apply_project(self.repo, plan)
        self.assertFalse((self.repo / project.LOCK).exists())
        self.assertEqual((self.repo / ".codex/.gitattributes").read_bytes(), b"agents/*.toml text\n")


if __name__ == "__main__":
    unittest.main()
