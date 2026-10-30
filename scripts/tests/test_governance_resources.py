"""Release-owned sources stay pinned while project facts and Git sources stay local."""

import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import governance_tools as g
import governance_rules as rules
sys.path.pop(0)


class ReleaseResourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.source = Path(__file__).resolve().parents[2]
        for directory in ("docs", ".codex", "rulepacks", "examples", "scripts"):
            shutil.copytree(self.source / directory, self.repo / directory)
        shutil.copy2(self.source / "AGENTS.md", self.repo / "AGENTS.md")
        self.release = self.repo / ".governance/releases" / ("a" * 64)
        self.release.mkdir(parents=True)
        for directory in ("docs", "rulepacks"):
            shutil.copytree(self.source / directory, self.release / directory)
        self.repin()

    def repin(self):
        files = {path.relative_to(self.release).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in self.release.rglob("*") if path.is_file() and path.name != "release.json"}
        (self.release / "release.json").write_text(json.dumps({"Files": files}), encoding="utf-8")

    def selected(self):
        return mock.patch.object(g, "__file__", str(self.release / "scripts/governance_tools.py"))

    def test_legacy_source_tools_keep_project_paths(self):
        path = self.repo / "docs/开发规范/项目开发总则.md"
        self.assertEqual(g.resource_path(self.repo, path), path)
        self.assertEqual(g.reference_path(self.repo, self.repo / "plan.json", path.relative_to(self.repo).as_posix()), path)

    def test_selected_release_overrides_new_mirror_but_never_project_facts(self):
        source = "docs/开发规范/项目开发总则.md"
        (self.repo / source).write_text("new active project mirror", encoding="utf-8")
        requirement = self.repo / "docs/requirements/REQ-20261028-001"
        requirement.mkdir(parents=True)
        fact = requirement / "control-profile.json"
        fact.write_text("{}", encoding="utf-8")
        with self.selected():
            self.assertEqual(g.resource_path(self.repo, source), self.release / source)
            self.assertEqual(g.reference_path(self.repo, fact, "control-profile.json"), fact)
            self.assertEqual(g.resource_path(self.repo, "AGENTS.md"), self.repo / "AGENTS.md")
            self.assertEqual(g.resource_path(self.repo, ".codex/agents/writer.toml"), self.repo / ".codex/agents/writer.toml")

    def test_cannot_borrow_release_from_another_project(self):
        with self.selected():
            other = self.repo / "other-project"
            self.assertEqual(g.resource_path(other, "docs/开发规范/项目开发总则.md"), other / "docs/开发规范/项目开发总则.md")

    def test_missing_inventory_and_modified_resource_fail_closed(self):
        source = "docs/开发规范/项目开发总则.md"
        with self.selected():
            (self.release / source).write_bytes(b"changed")
            with self.assertRaises(g.Invalid):
                g.resource_path(self.repo, source)
            with self.assertRaises(g.Invalid):
                g.resource_path(self.repo, "rulepacks/missing/pack.json")

    def test_schema_comes_from_selected_release_not_project_mirror(self):
        source = "docs/Agent治理/contracts/control-profile.schema.json"
        (self.repo / source).write_text("invalid project mirror", encoding="utf-8")
        (self.release / source).write_text(json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "required": ["old-release-field"]}), encoding="utf-8")
        self.repin()
        with self.selected():
            g.validate_contract_schema({"old-release-field": True}, self.repo, "control-profile.schema.json", "fixture")
            with self.assertRaises(g.Invalid):
                g.validate_contract_schema({}, self.repo, "control-profile.schema.json", "fixture")

    def test_nested_pack_reference_keeps_logical_identity(self):
        containing = self.repo / "docs/requirements/REQ-20261028-001/acceptance-plan.json"
        source = "rulepacks/frontend/1.0.0/rules.md"
        with self.selected():
            self.assertEqual(g.reference_path(self.repo, containing, "../../../" + source), self.release / source)
            self.assertEqual(g.logical_reference_path(self.repo, containing, "../../../" + source).resolve(), self.repo / source)

    def test_rule_lookup_combines_package_core_and_project_domain(self):
        (self.repo / "docs/开发规范/项目开发总则.md").write_text("new mirror", encoding="utf-8")
        domain = self.repo / "docs/开发规范/领域规范.md"
        domain.write_text("| Rule | Meaning |\n|---|---|\n| DOM-001 | Project rule |\n", encoding="utf-8")
        with self.selected():
            g.validate_control_source_ref(self.repo, self.repo / "plan.json", "RULE:GEN-001", "LEAN")
            g.validate_control_source_ref(self.repo, self.repo / "plan.json", "RULE:DOM-001", "LEAN")
            self.assertEqual(g.resource_path(self.repo, domain), domain)

    def test_check_uses_package_documents_and_actual_project_roles(self):
        (self.repo / "docs/Agent治理/需求基线模板.md").write_text("[bad](missing.md)", encoding="utf-8")
        with self.selected():
            self.assertIn("mechanical checks only", g.check(self.repo))
            (self.repo / ".codex/agents/writer.toml").write_text('name = "wrong"', encoding="utf-8")
            with self.assertRaises(g.Invalid):
                g.check(self.repo)

    def test_composition_keeps_canonical_pins_and_project_parameters(self):
        core = "docs/开发规范/项目开发总则.md"
        (self.repo / core).write_text("new active mirror", encoding="utf-8")
        project = "examples/rules/fullstack-react/project.json"
        with self.selected():
            result = rules.compose_rules(self.repo, project, ["web"], ["FE-001"])
            self.assertEqual(result["Sources"][core], hashlib.sha256((self.release / core).read_bytes()).hexdigest())
            self.assertTrue(all(not source.startswith(".governance/") for source in result["Sources"]))
            self.assertTrue(rules.verify_composition_sources(self.repo, result)["Valid"])
            convention = self.repo / "examples/rules/fullstack-react/web/conventions.md"
            convention.write_bytes(convention.read_bytes() + b"\nupdated project parameter\n")
            with self.assertRaises(g.Invalid):
                rules.verify_composition_sources(self.repo, result)

    def test_revision_binding_reads_original_project_git_path(self):
        revision = "1" * 40
        governance = "docs/治理版本说明/v3.0.2治理修订说明.md"
        testing = "docs/开发规范/测试规范.md"
        texts = {governance: "版本：v3.0.2\n", testing: "版本：v3.0.1\n"}
        bindings = {"BusinessRevision": revision}
        for key, source, version in (("Governance", governance, "v3.0.2"), ("Testing", testing, "v3.0.1")):
            bindings[key] = {"Version": version, "SourceRef": source, "SourceDigest": g.content_digest(texts[source]), "Revision": revision}
        with self.selected(), mock.patch.object(g, "local_git"), mock.patch.object(g, "git_text", side_effect=lambda repo, revision, source, field: texts[source]) as read:
            g.validate_baseline_bindings(bindings, self.repo, self.repo / "profile.json", "v3.0.2")
            self.assertEqual([call.args[2] for call in read.call_args_list], [governance, testing])


if __name__ == "__main__":
    unittest.main()
