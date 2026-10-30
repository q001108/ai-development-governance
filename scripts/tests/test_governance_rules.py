"""Fixed-source rule composition tests; no business project or external service access."""

import contextlib
import copy
import io
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import governance_rules as rules
sys.path.pop(0)


class RuleCompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        source = Path(__file__).resolve().parents[2]
        for name in ("rulepacks", "examples/rules"):
            shutil.copytree(source / name, self.repo / name)
        core = self.repo / rules.CORE_SOURCE
        core.parent.mkdir(parents=True)
        core.write_bytes((source / rules.CORE_SOURCE).read_bytes())
        self.project = "examples/rules/fullstack-react/project.json"

    def read(self, name):
        return json.loads((self.repo / name).read_text(encoding="utf-8"))

    def write(self, name, data):
        (self.repo / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def repin(self, pack_id):
        path = f"rulepacks/{pack_id}/1.0.0/pack.json"
        value = self.read(path)
        value["Document"]["Sha256"] = rules.sha256((self.repo / value["Document"]["Source"]).read_bytes())
        self.write(path, value)
        project = self.read(self.project)
        next(binding for binding in project["Packs"] if binding["PackId"] == pack_id)["Sha256"] = rules.sha256((self.repo / path).read_bytes())
        self.write(self.project, project)

    def compose(self, *args, **kwargs):
        return rules.compose_rules(self.repo, self.project, *args, **kwargs)

    def file_state(self):
        return {path.relative_to(self.repo).as_posix(): path.read_bytes() for path in self.repo.rglob("*") if path.is_file()}

    def test_backend_only_does_not_require_frontend_ui_or_data_capabilities(self):
        result = rules.compose_rules(self.repo, "examples/rules/backend-only/project.json")
        self.assertEqual([rule["RuleId"] for rule in result["ResolvedRules"] if not rule["RuleId"].startswith("GEN-")], ["BE-001"])
        self.assertTrue(any(item["RuleId"] == "BE-002" and item["Status"] == "NOT_APPLICABLE" for item in result["Applicability"]))
        self.assertFalse(any(rule["RuleId"].startswith(("FE-", "UI-")) for rule in result["ResolvedRules"]))

    def test_vue_and_react_reuse_common_packs_and_only_react_selects_extra_rule(self):
        vue = rules.compose_rules(self.repo, "examples/rules/fullstack-vue/project.json")
        react = self.compose()
        vue_ids = {rule["RuleId"] for rule in vue["ResolvedRules"]}
        react_ids = {rule["RuleId"] for rule in react["ResolvedRules"]}
        self.assertEqual(react_ids - vue_ids, {"FE-101"})
        self.assertTrue({"FE-001", "UI-001", "BE-001"} <= react_ids)
        extra = next(rule for rule in react["ResolvedRules"] if rule["RuleId"] == "FE-101")
        self.assertEqual(extra["Modules"], ["web"])
        self.assertIn("#react-module", extra["Parameters"][0]["Value"])
        self.assertIn("Parameters.FrontendConventions", extra["Parameters"][0]["DeclaredAt"])

    def test_affected_modules_project_existing_table_without_inventing_controls(self):
        result = self.compose(["web"], ["FE-001", "GEN-004"])
        self.assertEqual(result["Scope"], "AFFECTED_MODULES")
        self.assertFalse(any(rule["RuleId"].startswith("BE-") for rule in result["ResolvedRules"]))
        self.assertIn("规范文档 | 适用章节 | 直接相关 RuleId | 关联控制项 | 本需求适用说明", result["ApplicableRulesMarkdown"])
        self.assertTrue(any(row[2] == "FE-001" for row in result["ApplicableRules"]))
        self.assertTrue(all(row[3] == "N/A" for row in result["ApplicableRules"]))
        self.assertFalse(result["AcceptanceControlsCreated"])
        self.assertFalse(result["ValidationSetExpanded"])
        self.assertFalse(result["RequirementBound"])
        self.assertNotIn("API 输入、输出", result["ApplicableRulesMarkdown"])

    def test_core_and_optional_rules_are_source_pinned_and_deterministic(self):
        first = self.compose()
        second = self.compose()
        self.assertEqual(first, second)
        for source, digest in first["Sources"].items():
            self.assertEqual(digest, rules.sha256((self.repo / source).read_bytes()))
        self.assertIn(rules.CORE_SOURCE, first["Sources"])
        self.assertFalse(first["FilesWritten"])

    def test_generated_plan_source_reference_passes_existing_live_plan_validator(self):
        import test_governance_tools as fixtures
        fixture = fixtures.V3PlanValidationTests(methodName="test_valid_lean_and_readonly_cli")
        fixture.setUp()
        try:
            for name in ("rulepacks", "examples/rules"):
                shutil.copytree(self.repo / name, fixture.repo / name)
            (fixture.repo / rules.CORE_SOURCE).write_bytes((self.repo / rules.CORE_SOURCE).read_bytes())
            result = rules.compose_rules(fixture.repo, self.project, ["web"], ["FE-001"])
            reference = result["PlanSourceRefs"]["FE-001"]
            self.assertTrue(reference.startswith("../../../rulepacks/"))
            fixture.plan["Controls"][2]["SourceRefs"].append(reference)
            fixture.write()
            self.assertTrue(fixtures.g.validate_plan(fixture.profile_path, fixture.plan_path)["Valid"])
        finally:
            fixture.doCleanups()

    def test_raw_json_credential_text_is_rejected_before_parameter_output(self):
        with self.assertRaises(rules.g.Invalid):
            rules.safe_text('{"password":"synthetic-private-value"}')
        path = self.repo / "private-source.json"
        path.write_text('{"password":"synthetic-private-value"}', encoding="utf-8")
        with self.assertRaises(rules.g.Invalid):
            rules.read_source(self.repo, "private-source.json", {})

    def test_saved_projection_verifies_original_sources_without_following_newer_content(self):
        result = self.compose()
        self.assertTrue(rules.verify_composition_sources(self.repo, result)["Valid"])
        path = self.repo / "examples/rules/fullstack-react/web/conventions.md"
        path.write_bytes(path.read_bytes() + b"\nchanged project convention\n")
        with self.assertRaises(rules.g.Invalid):
            rules.verify_composition_sources(self.repo, result)
        self.assertFalse(result["RequirementBound"])

    def test_modified_rule_bytes_fail_fixed_digest_including_line_endings(self):
        path = self.repo / "rulepacks/frontend/1.0.0/rules.md"
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        with self.assertRaises(rules.g.Invalid):
            self.compose()

    def test_manifest_tampering_and_core_tampering_are_rejected(self):
        path = self.repo / "rulepacks/ui/1.0.0/pack.json"
        original = path.read_bytes()
        path.write_bytes(original + b" ")
        with self.assertRaises(rules.g.Invalid):
            self.compose()
        path.write_bytes(original)
        core = self.repo / rules.CORE_SOURCE
        core.write_bytes(core.read_bytes() + b"\n")
        with self.assertRaises(rules.g.Invalid):
            self.compose()

    def test_duplicate_rule_id_and_core_override_are_rejected(self):
        path = "rulepacks/frontend-react/1.0.0/pack.json"
        pack = self.read(path)
        pack["Rules"].append(copy.deepcopy(pack["Rules"][0]))
        self.write(path, pack)
        self.repin("frontend-react")
        with self.assertRaises(rules.g.Invalid):
            self.compose()
        pack["Rules"] = pack["Rules"][:1]
        pack["Rules"][0]["RuleId"] = "GEN-005"
        self.write(path, pack)
        self.repin("frontend-react")
        with self.assertRaises(rules.g.Invalid):
            self.compose()

    def test_duplicate_pack_versions_are_not_silently_selected(self):
        project = self.read(self.project)
        project["Packs"].append(copy.deepcopy(project["Packs"][0]))
        self.write(self.project, project)
        with self.assertRaises(rules.g.Invalid):
            self.compose()

    def test_governance_test_route_and_control_namespaces_cannot_be_redefined_by_a_pack(self):
        path = "rulepacks/frontend-react/1.0.0/pack.json"
        for ident in ("TST-009", "ROUTE-001", "BA-001"):
            pack = self.read(path)
            pack["Rules"][0]["RuleId"] = ident
            self.write(path, pack)
            self.repin("frontend-react")
            with self.assertRaises(rules.g.Invalid):
                self.compose()

    def test_existing_project_rule_id_conflict_is_rejected_without_overwriting_it(self):
        path = self.repo / "docs/开发规范/前端开发规范.md"
        content = "# Existing frontend\n\n## Project rule\n\n| RuleId | Rule |\n|---|---|\n| FE-001 | Existing different project meaning |\n"
        path.write_text(content, encoding="utf-8")
        with self.assertRaises(rules.g.Invalid):
            self.compose()
        self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_existing_project_rules_are_checked_for_conflicts_without_adding_unrelated_rules(self):
        path = self.repo / "docs/开发规范/项目领域规范.md"
        path.write_text("# Project rules\n\n## Other scope\n\n| RuleId | Rule |\n|---|---|\n| DOM-001 | unrelated source |\n", encoding="utf-8")
        result = self.compose(["web"])
        self.assertIn(path.relative_to(self.repo).as_posix(), result["Sources"])
        self.assertNotIn("DOM-001", [rule["RuleId"] for rule in result["ResolvedRules"]])

    def test_fixed_pack_identity_and_compatibility_are_enforced(self):
        project = self.read(self.project)
        project["Packs"][0]["Version"] = "2.0.0"
        self.write(self.project, project)
        with self.assertRaises(rules.g.Invalid):
            self.compose()
        project["Packs"][0]["Version"] = "1.0.0"
        self.write(self.project, project)
        path = "rulepacks/backend-api/1.0.0/pack.json"
        pack = self.read(path)
        pack["GovernanceVersions"] = ["v3.0"]
        self.write(path, pack)
        self.repin("backend-api")
        with self.assertRaises(rules.g.Invalid):
            self.compose()

    def test_simultaneous_rule_conflicts_and_unknown_conflict_targets_are_rejected(self):
        path = "rulepacks/frontend-react/1.0.0/pack.json"
        pack = self.read(path)
        for target in ("GEN-005", "BE-999"):
            pack["Rules"][0]["ConflictsWith"] = [target]
            self.write(path, pack)
            self.repin("frontend-react")
            with self.assertRaises(rules.g.Invalid):
                self.compose()

    def test_missing_applicable_parameter_is_not_filled_from_another_module(self):
        project = self.read(self.project)
        project["Modules"][1]["Parameters"].pop("DesignSource")
        self.write(self.project, project)
        with self.assertRaises(rules.g.Invalid):
            self.compose()

    def test_adding_data_capability_requires_its_real_data_contract(self):
        project = self.read(self.project)
        project["Modules"][0]["Capabilities"].append("data")
        self.write(self.project, project)
        with self.assertRaises(rules.g.Invalid):
            self.compose()

    def test_missing_parameter_file_or_anchor_is_rejected(self):
        project = self.read(self.project)
        for reference in ("examples/rules/missing.md", "examples/rules/fullstack-react/web/conventions.md#not-there"):
            project["Modules"][1]["Parameters"]["DesignSource"] = reference
            self.write(self.project, project)
            with self.assertRaises(rules.g.Invalid):
                self.compose()

    def test_invalid_rule_definition_or_section_is_rejected(self):
        path = "rulepacks/frontend-react/1.0.0/pack.json"
        pack = self.read(path)
        pack["Rules"][0]["Section"] = "missing section"
        self.write(path, pack)
        self.repin("frontend-react")
        with self.assertRaises(rules.g.Invalid):
            self.compose()

    def test_undeclared_template_parameter_and_unknown_parameter_kind_are_rejected(self):
        path = "rulepacks/frontend-react/1.0.0/rules.md"
        original = (self.repo / path).read_text(encoding="utf-8")
        (self.repo / path).write_text(original.replace("{{FrontendConventions}}", "{{OtherSource}}"), encoding="utf-8")
        self.repin("frontend-react")
        with self.assertRaises(rules.g.Invalid):
            self.compose()

        (self.repo / path).write_text(original, encoding="utf-8")
        pack_path = "rulepacks/frontend-react/1.0.0/pack.json"
        pack = self.read(pack_path)
        pack["Parameters"]["FrontendConventions"] = {"Kind": "RUN_COMMAND"}
        self.write(pack_path, pack)
        self.repin("frontend-react")
        with self.assertRaises(rules.g.Invalid):
            self.compose()

    def test_shared_parameter_name_cannot_have_ambiguous_definitions_across_packs(self):
        path = "rulepacks/frontend-react/1.0.0/pack.json"
        pack = self.read(path)
        pack["Parameters"]["FrontendConventions"] = {"Kind": "CHOICE", "Values": ["examples/rules/fullstack-react/web/conventions.md#react-module"]}
        self.write(path, pack)
        self.repin("frontend-react")
        with self.assertRaises(rules.g.Invalid):
            self.compose()

    def test_empty_affected_selection_is_rejected_and_empty_direct_selection_keeps_chapter_defaults(self):
        with self.assertRaises(rules.g.Invalid):
            self.compose([])
        result = self.compose(["web"], [])
        self.assertTrue(result["ResolvedRules"])
        self.assertTrue(all(row[2] == "N/A" for row in result["ApplicableRules"]))

    def test_unknown_direct_rule_or_unaffected_rule_is_rejected(self):
        for identifiers in (["FE-999"], ["BE-001"]):
            with self.assertRaises(rules.g.Invalid):
                self.compose(["web"], identifiers)
        with self.assertRaises(rules.g.Invalid):
            self.compose(["not-declared"])

    def test_path_escape_windows_ambiguous_and_sensitive_sources_are_rejected(self):
        project = self.read(self.project)
        for source in ("../outside.json", "C:/outside.json", "//host/pack.json", "rulepacks\\frontend\\pack.json", ".env.json", "pack%20.json", "pack#part.json"):
            project["Packs"][0]["Source"] = source
            self.write(self.project, project)
            with self.assertRaises(rules.g.Invalid):
                self.compose()

    def test_symbolic_source_and_overlapping_module_roots_are_rejected(self):
        path = self.repo / "rulepacks/backend-api/1.0.0/pack.json"
        with mock.patch.object(rules.Path, "is_symlink", lambda value: value == path):
            with self.assertRaises(rules.g.Invalid):
                self.compose()
        project = self.read(self.project)
        project["Modules"][1]["Root"] = project["Modules"][0]["Root"]
        self.write(self.project, project)
        with self.assertRaises(rules.g.Invalid):
            self.compose()

    def test_duplicate_json_keys_and_sensitive_values_are_rejected_without_echo(self):
        project = self.read(self.project)
        project["Modules"][1]["Parameters"]["DesignSource"] = "password=synthetic-private-value"
        self.write(self.project, project)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = rules.main(["--repo", str(self.repo), "--project", self.project])
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("synthetic-private-value", stderr.getvalue())
        (self.repo / self.project).write_text('{"RuleCompositionVersion":"v1.0","RuleCompositionVersion":"v1.0"}', encoding="utf-8")
        with self.assertRaises(rules.g.Invalid):
            self.compose()

    def test_composition_never_runs_processes_or_network_and_does_not_write(self):
        before = self.file_state()
        with mock.patch.object(socket, "socket", side_effect=AssertionError("no network")), \
             mock.patch.object(subprocess, "Popen", side_effect=AssertionError("no process")):
            self.compose()
        self.assertEqual(before, self.file_state())

    def test_source_change_during_composition_is_detected(self):
        actual = rules.parameter
        changed = False
        def mutate(*args, **kwargs):
            nonlocal changed
            result = actual(*args, **kwargs)
            if not changed:
                target = self.repo / "rulepacks/ui/1.0.0/rules.md"
                target.write_bytes(target.read_bytes() + b"\n")
                changed = True
            return result
        with mock.patch.object(rules, "parameter", side_effect=mutate):
            with self.assertRaises(rules.g.Invalid):
                self.compose()

    def test_real_cli_is_readonly_including_bytecode_and_prints_existing_table(self):
        before = self.file_state()
        result = subprocess.run([sys.executable, str(Path(rules.__file__).resolve()), "--repo", str(self.repo),
                                 "--project", self.project, "--module", "web", "--direct-rule", "FE-001", "--format", "markdown"],
                                capture_output=True, text=True, encoding="utf-8", check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FE-001", result.stdout)
        self.assertIn("本需求适用说明", result.stdout)
        self.assertEqual(before, self.file_state())


if __name__ == "__main__":
    unittest.main()
