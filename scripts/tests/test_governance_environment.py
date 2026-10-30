"""Synthetic local manifests; the diagnostic never executes their commands or writes facts."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import governance_environment as env
sys.path.pop(0)


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.manifest = self.repo / "environment.json"
        (self.repo / "fixtures").mkdir()
        (self.repo / "check.py").write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")
        self.data = {
            "ToolProtocolVersion": "v1.0", "RequirementId": "REQ-TEST-20261019-01",
            "RepositoryRoot": str(self.repo), "Paths": [{"PathId": "DATA", "Path": "fixtures", "Kind": "DIRECTORY"}],
            "Tools": [{"ToolId": "CHECK", "Kind": "REPO_FILE", "Value": "check.py"}],
            "Ports": [{"PortId": "API", "Port": 18080}],
            "Validations": [{"ValidationId": "V-01", "Triggered": True, "Owner": "QA", "GateClass": "BLOCKING",
                             "CommandRef": "check.py", "Prerequisites": [{
                                 "PrerequisiteId": "FIXTURE", "Readiness": "READY", "Responsible": "TEST_TEAM",
                                 "CompletionCondition": "Isolated fixture available", "ReadinessCheck": "COMMAND:fixture-check",
                             }]}],
            "CandidateRef": "CAND-SELF-DECLARED",
        }
        self.write()
        self.git = mock.patch.object(env.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, str(self.repo) + "\n"))
        self.run = self.git.start()
        self.addCleanup(self.git.stop)

    def write(self):
        self.manifest.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")

    def cli(self, phase="execution", owner="QA", manifest="environment.json", requirement="REQ-TEST-20261019-01"):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = env.main(["--repo", str(self.repo), "--manifest", manifest, "--requirement-id", requirement,
                             "--phase", phase, "--owner", owner])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_valid_report_stays_diagnostic_and_does_not_echo_local_values(self):
        code, stdout, stderr = self.cli()
        self.assertEqual((code, stderr), (0, ""))
        report = json.loads(stdout)
        self.assertTrue(report["DiagnosticOnly"])
        self.assertFalse(report["GatesChanged"])
        self.assertFalse(report["QualityPassed"])
        self.assertEqual(report["CandidateBinding"], "UNKNOWN")
        self.assertEqual(report["ServiceOwnership"], "UNKNOWN")
        self.assertEqual(report["OwnerScope"], "QA")
        self.assertEqual(report["RequirementId"], self.data["RequirementId"])
        for forbidden in (str(self.repo), "CAND-SELF-DECLARED", "check.py", "CanProceed", "G2Eligible"):
            self.assertNotIn(forbidden, stdout)
        self.assertTrue(any(item["Status"] == "DECLARED_READY" and item["EvidenceKind"] == "DECLARED"
                            for item in report["Unverified"]))
        self.assertTrue(any(item.get("ExistsOnly") for item in report["ReportChecks"]))

    def test_wrong_repository_and_requirement_are_conflicts(self):
        self.data["RepositoryRoot"] = str(self.repo / "other")
        self.write()
        code, stdout, _ = self.cli(requirement="REQ-OTHER-20261019-01")
        self.assertEqual(code, 1)
        self.assertEqual({item["Check"] for item in json.loads(stdout)["BlockingIssues"]},
                         {"DECLARED_REPOSITORY_ROOT", "REQUIREMENT_BINDING"})
        self.data["RepositoryRoot"] = str(self.repo)
        self.write()
        self.run.return_value = subprocess.CompletedProcess([], 0, str(self.repo.parent) + "\n")
        self.assertEqual(self.cli()[0], 1)

    def test_port_values_and_duplicate_declarations(self):
        self.data["Ports"].append({"PortId": "OTHER", "Port": 18080})
        self.write()
        code, stdout, _ = self.cli(phase="planning")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout)["BlockingIssues"][0]["Status"], "MISMATCH")
        for value in (True, False, 0, 65536, "18080"):
            with self.subTest(value=value):
                self.data["Ports"] = [{"PortId": "API", "Port": value}]
                self.write()
                self.assertEqual(self.cli()[0], 2)

    def test_planned_and_unknown_prerequisites_are_not_g2_decisions(self):
        prerequisite = self.data["Validations"][0]["Prerequisites"][0]
        for readiness, responsible, expected in (("PLANNED", "TEAM", "PLANNED"), ("PLANNED", "", "INCOMPLETE_PLAN"),
                                                  ("UNKNOWN", "TEAM", "UNKNOWN")):
            with self.subTest(readiness=readiness, responsible=responsible):
                prerequisite.update(Readiness=readiness, Responsible=responsible)
                self.write()
                code, stdout, _ = self.cli(phase="planning")
                self.assertEqual(code, 0)
                self.assertTrue(any(item["Status"] == expected for item in json.loads(stdout)["Unverified"]))
                self.assertEqual(self.cli(phase="execution")[0], 1)

    def test_missing_explicit_resources_only_block_execution(self):
        self.data["Paths"][0]["Path"] = "not-created-yet"
        self.data["Tools"] = [{"ToolId": "PYTHON", "Kind": "PATH", "Value": "python"}]
        self.write()
        with mock.patch.object(env.shutil, "which", return_value=None):
            code, stdout, _ = self.cli(phase="planning")
            self.assertEqual(code, 0)
            self.assertEqual(len([item for item in json.loads(stdout)["Unverified"] if item["Status"] == "MISSING"]), 2)
            self.assertEqual(self.cli()[0], 1)

    def test_untriggered_or_other_owner_items_do_not_inspect_prerequisites(self):
        validation = self.data["Validations"][0]
        for changes in ({"Triggered": False}, {"Owner": "WRITER"}, {"Owner": "REVIEWER"}):
            with self.subTest(changes=changes):
                validation.update({"Triggered": True, "Owner": "QA", **changes})
                validation["Prerequisites"] = [{"NotAValidPrerequisite": "ignored outside selected scope"}]
                self.write()
                code, stdout, _ = self.cli()
                self.assertEqual(code, 0)
                self.assertTrue(any(item["Status"] == "NOT_APPLICABLE" for item in json.loads(stdout)["ReportChecks"]))
        validation.update(Triggered=True, Owner="WRITER", Prerequisites=copy.deepcopy(self.data["Validations"][0]["Prerequisites"]))
        self.write()
        self.assertEqual(self.cli(owner="WRITER")[0], 2)

    def test_advisory_unknown_is_visible_without_blocking(self):
        self.data["Validations"][0]["GateClass"] = "ADVISORY"
        self.data["Validations"][0]["Prerequisites"][0]["Readiness"] = "UNKNOWN"
        self.write()
        code, stdout, _ = self.cli()
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(stdout)["BlockingIssues"])
        self.assertTrue(any(item.get("ValidationId") == "V-01" for item in json.loads(stdout)["Unverified"]))

    def test_path_traversal_network_and_absolute_paths_are_rejected(self):
        for value in ("../outside", "fixtures/../../outside", str(self.repo / "fixtures"), "//host/share/x", "\\\\host\\share\\x", "C:\\outside", "fixtures/file:stream"):
            with self.subTest(value=value):
                self.data["Paths"][0]["Path"] = value
                self.write()
                self.assertEqual(self.cli()[0], 2)
        for value in ("../environment.json", "//host/share/environment.json", ".env", "secrets.json"):
            with self.subTest(manifest=value):
                self.assertEqual(self.cli(manifest=value)[0], 2)

    def test_symbolic_or_junction_path_is_rejected(self):
        target = self.repo / "fixtures"
        with mock.patch.object(env.Path, "is_symlink", lambda path: path == target):
            self.assertEqual(self.cli()[0], 2)
        with mock.patch.object(env.Path, "is_junction", lambda path: path == target):
            self.assertEqual(self.cli()[0], 2)

    def test_path_tool_names_reject_parameters_or_directories(self):
        for value in ("../python", "C:\\python.exe", "//host/python", "python -c dangerous", "python;dangerous"):
            self.data["Tools"] = [{"ToolId": "PYTHON", "Kind": "PATH", "Value": value}]
            self.write()
            with mock.patch.object(env.shutil, "which") as which:
                self.assertEqual(self.cli()[0], 2)
                which.assert_not_called()

    def test_sensitive_input_and_read_errors_do_not_echo_values(self):
        self.data["Validations"][0]["Prerequisites"][0]["CompletionCondition"] = "password=synthetic-private-value"
        self.write()
        code, stdout, stderr = self.cli()
        self.assertEqual((code, stdout), (2, ""))
        self.assertNotIn("synthetic-private-value", stderr)
        self.assertEqual(self.cli(manifest="missing.json")[0], 2)
        self.manifest.write_text('{"ToolProtocolVersion":"v1.0","ToolProtocolVersion":"v1.0"}', encoding="utf-8")
        self.assertEqual(self.cli()[0], 2)

    def test_no_command_network_process_scan_or_file_write(self):
        before = {path.relative_to(self.repo): path.read_bytes() for path in self.repo.rglob("*") if path.is_file()}
        with mock.patch.object(socket, "socket", side_effect=AssertionError("no network")), \
             mock.patch.object(env.subprocess, "Popen", side_effect=AssertionError("no extra process")), \
             mock.patch.object(env.os, "system", side_effect=AssertionError("no shell")):
            self.assertEqual(self.cli()[0], 0)
        self.assertEqual(before, {path.relative_to(self.repo): path.read_bytes() for path in self.repo.rglob("*") if path.is_file()})
        self.assertEqual(self.run.call_count, 1)
        command = self.run.call_args.args[0]
        self.assertEqual(command, ["git", "-C", str(self.repo), "rev-parse", "--show-toplevel"])

    def test_git_probe_does_not_inherit_repository_redirects(self):
        overrides = {key: "unused local fixture" for key in (
            "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG_COUNT", "GIT_TRACE",
        )}
        before = dict(os.environ)
        with mock.patch.dict(os.environ, overrides):
            self.assertEqual(self.cli()[0], 0)
            child = self.run.call_args.kwargs["env"]
            self.assertFalse(any(key.upper().startswith("GIT_") for key in child))
            self.assertEqual(child.get("PATH"), os.environ.get("PATH"))
            self.assertTrue(all(os.environ[key] == value for key, value in overrides.items()))
        self.assertEqual(dict(os.environ), before)

    def test_cli_subprocess_does_not_create_files_including_bytecode(self):
        self.git.stop()
        tools = self.repo / "tooling"
        tools.mkdir()
        script = tools / "governance_environment.py"
        script.write_bytes(Path(env.__file__).read_bytes())
        (tools / "governance_tools.py").write_bytes(Path(env.g.__file__).read_bytes())
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        before = {path.relative_to(self.repo): path.read_bytes() for path in self.repo.rglob("*") if path.is_file()}
        command = [sys.executable, str(script), "--repo", str(self.repo), "--manifest", "environment.json",
                   "--requirement-id", self.data["RequirementId"]]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, {path.relative_to(self.repo): path.read_bytes() for path in self.repo.rglob("*") if path.is_file()})
        # Even redirecting Git to a real fixture cannot make a non-repository root match.
        ordinary = self.repo / "ordinary"
        ordinary.mkdir()
        redirected = dict(self.data, RepositoryRoot=str(ordinary))
        (ordinary / "environment.json").write_text(json.dumps(redirected), encoding="utf-8")
        altered_env = dict(os.environ, GIT_DIR=str(self.repo / ".git"), GIT_WORK_TREE=str(ordinary))
        altered_command = list(command)
        altered_command[altered_command.index("--repo") + 1] = str(ordinary)
        rejected = subprocess.run(altered_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, check=False, env=altered_env)
        self.assertEqual(rejected.returncode, 1)
        self.assertTrue(any(item["Check"] == "GIT_WORKTREE_ROOT" and item["Status"] == "MISMATCH"
                            for item in json.loads(rejected.stdout)["BlockingIssues"]))

    def test_argument_errors_do_not_echo_unrecognized_values(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
            env.main(["--phase", "password=synthetic-private-value"])
        self.assertEqual(error.exception.code, 2)
        self.assertNotIn("synthetic-private-value", stderr.getvalue())


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def write(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def cli(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = env.main(["--repo", str(self.repo), *extra])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_without_manifest_discovers_metadata_and_only_returns_command_references(self):
        self.write("frontend/package.json", json.dumps({"engines": {"node": ">=20", "npm": ">=10"},
                   "packageManager": "npm@10.9.9", "scripts": {"dev": "must-not-run", "test": "must-not-run"}}))
        self.write("backend/pom.xml", "<project/>")
        self.write("backend/mvnw.cmd", "must-not-run")
        self.write("worker/pyproject.toml", '[project]\nrequires-python = ">=3.12"\n[project.scripts]\nworker = "private.entry:main"\n')
        with mock.patch.object(env, "local_tool_exists", return_value=True):
            code, stdout, stderr = self.cli()
        self.assertEqual((code, stderr), (1, ""))  # Maven's effective model still needs a project-specific check.
        report = json.loads(stdout)
        self.assertEqual(report["ResultType"], "LOCAL_ENVIRONMENT_DISCOVERY")
        self.assertIsNone(report["RequirementId"])
        self.assertTrue(report["SelectionRequired"])
        node = next(item for item in report["Projects"] if item["Kind"] == "NODE")
        self.assertEqual(node["DeclaredVersions"], {"node": ">=20", "npm": ">=10"})
        self.assertEqual(node["PackageManager"], {"Tool": "npm", "DeclaredVersion": "10.9.9"})
        self.assertEqual(node["EntryRefs"], ["frontend/package.json#scripts.dev", "frontend/package.json#scripts.test"])
        maven = next(item for item in report["Projects"] if item["Kind"] == "MAVEN")
        self.assertEqual(maven["MavenWrapper"], "PRESENT")
        self.assertNotIn("mvn", [item["Tool"] for item in maven["Tools"]])
        self.assertIn("TOOL_VERSION_COMPATIBILITY", report["Unverified"])
        for forbidden in ("must-not-run", "private.entry", str(self.repo), "CanProceed", "G2Eligible"):
            self.assertNotIn(forbidden, stdout)

    def test_scope_ignored_generated_and_deleted_metadata(self):
        self.write(".gitignore", "ignored/\n")
        for name in ("frontend/package.json", "backend/package.json", "ignored/package.json", "node_modules/pkg/package.json"):
            self.write(name, '{"scripts": {"test": "unused"}, "packageManager": "npm@10.9.9"}')
        self.write("deleted/package.json", "{}")
        subprocess.run(["git", "-C", str(self.repo), "add", "deleted/package.json"], check=True)
        (self.repo / "deleted/package.json").unlink()
        with mock.patch.object(env, "local_tool_exists", return_value=True):
            report = env.discover(self.repo, scope="frontend")
            self.assertEqual([item["Source"] for item in report["Projects"]], ["frontend/package.json"])
            self.assertFalse(report["SelectionRequired"])
            full = env.discover(self.repo)
        self.assertEqual(len(full["Projects"]), 2)
        self.assertEqual(full["Findings"], [])

    def test_missing_tools_and_ambiguous_package_manager_are_visible(self):
        self.write("package.json", "{}")
        for name in ("package-lock.json", "yarn.lock"):
            self.write(name, "")
        with mock.patch.object(env, "local_tool_exists", return_value=False):
            report = env.discover(self.repo)
        self.assertTrue(any(item["Check"] == "PACKAGE_MANAGER" and item["Status"] == "UNKNOWN" for item in report["Findings"]))
        self.assertTrue(any(item["Check"] == "TOOL_EXISTS" and item["Status"] == "MISSING" for item in report["Findings"]))
        self.assertTrue(report["DiagnosticOnly"])
        self.assertFalse(report["GatesChanged"])
        self.assertFalse(report["QualityPassed"])
        self.assertEqual(report["RequirementBinding"], "UNKNOWN")

    def test_single_lock_file_discovers_manager_without_reading_lock_contents(self):
        self.write("package.json", "{}")
        self.write("pnpm-lock.yaml", "password=synthetic-private-value")
        with mock.patch.object(env, "local_tool_exists", return_value=True):
            code, stdout, stderr = self.cli()
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual([item["Tool"] for item in json.loads(stdout)["Projects"][0]["Tools"]], ["node", "pnpm"])
        self.assertNotIn("synthetic-private-value", stdout)

    def test_invalid_sensitive_and_oversized_metadata_never_echo_contents(self):
        for content in ('{"scripts": {"test": "password=synthetic-private-value"}}',
                        '{"scripts": {}, "scripts": {}}', '{"scripts": []}', '{"scripts": {"test": 1}}',
                        '{"engines": {"node": "password=synthetic-private-value"}}', "x" * (256 * 1024 + 1)):
            with self.subTest(size=len(content)):
                self.write("package.json", content)
                with mock.patch.object(env, "local_tool_exists", return_value=True):
                    code, stdout, stderr = self.cli()
                self.assertEqual((code, stderr), (1, ""))
                self.assertEqual(json.loads(stdout)["Projects"][0]["Status"], "INVALID_OR_UNREADABLE")
                self.assertNotIn("synthetic-private-value", stdout + stderr)

    def test_no_supported_metadata_is_unknown_not_a_readiness_success(self):
        self.write("README.md", "ordinary project")
        report = env.discover(self.repo)
        self.assertEqual(report["Projects"], [])
        self.assertIn("NO_SUPPORTED_PROJECT_METADATA", report["Unverified"])
        self.assertFalse(report["QualityPassed"])

    def test_discovery_rejects_wrong_roots_scope_escapes_and_symbolic_metadata(self):
        self.write("package.json", "{}")
        self.assertEqual(self.cli("--scope", "../outside")[0], 2)
        self.assertEqual(self.cli("--scope", "//host/share")[0], 2)
        with self.assertRaises(env.g.Invalid):
            env.discover(self.repo / "not-a-repository")
        target = self.repo / "package.json"
        with mock.patch.object(env.Path, "is_symlink", lambda path: path == target):
            self.assertEqual(self.cli()[0], 2)

    def test_python_markers_do_not_execute_build_code_or_read_requirement_urls(self):
        self.write("pyproject.toml", '[project]\nrequires-python = ">=3.12"\n')
        self.write("requirements.txt", "https://password=synthetic-private-value@invalid/")
        self.write("setup.py", "raise RuntimeError('must not execute')")
        with mock.patch.object(env, "local_tool_exists", return_value=True):
            code, stdout, stderr = self.cli()
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(len(json.loads(stdout)["Projects"]), 2)
        self.assertNotIn("synthetic-private-value", stdout)
        self.write("pyproject.toml", "[invalid")
        self.assertEqual(self.cli()[0], 1)

    def test_mode_arguments_are_not_silently_ignored_and_git_timeout_is_sanitized(self):
        for extra in (("--phase", "planning"), ("--owner", "WRITER"),
                      ("--manifest", "environment.json"),
                      ("--manifest", "environment.json", "--requirement-id", "REQ-TEST-20261019-01", "--scope", ".")):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                self.cli(*extra)
            self.assertEqual(error.exception.code, 2)
        with mock.patch.object(env.subprocess, "run", side_effect=subprocess.TimeoutExpired("synthetic-private-value", 10)):
            code, stdout, stderr = self.cli()
        self.assertEqual((code, stdout), (2, ""))
        self.assertNotIn("synthetic-private-value", stderr)

    def test_discovery_is_readonly_and_only_runs_fixed_git_queries(self):
        self.write("package.json", '{"packageManager": "npm@10.9.9", "scripts": {"test": "must-not-run"}}')
        before = {path.relative_to(self.repo): path.read_bytes() for path in self.repo.rglob("*") if path.is_file()}
        real_run = subprocess.run
        calls = []

        def read_only_git(command, **kwargs):
            self.assertEqual(command[:3], ["git", "-C", str(self.repo)])
            self.assertIn(command[3:], [["rev-parse", "--show-toplevel"], ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]])
            self.assertEqual(kwargs["env"].get("GIT_OPTIONAL_LOCKS"), "0")
            self.assertFalse(any(key.startswith("GIT_") and key != "GIT_OPTIONAL_LOCKS" for key in kwargs["env"]))
            calls.append(command)
            return real_run(command, **kwargs)

        with mock.patch.object(env.subprocess, "run", side_effect=read_only_git), \
             mock.patch.object(env, "local_tool_exists", return_value=True), \
             mock.patch.object(socket, "socket", side_effect=AssertionError("no network")), \
             mock.patch.dict(os.environ, {"GIT_DIR": "unused", "GIT_TRACE": "unused"}):
            self.assertEqual(self.cli()[0], 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(before, {path.relative_to(self.repo): path.read_bytes() for path in self.repo.rglob("*") if path.is_file()})

    def test_discovery_cli_creates_no_files_and_ignores_git_redirects(self):
        self.write("requirements.txt", "")
        before = {path.relative_to(self.repo): path.read_bytes() for path in self.repo.rglob("*") if path.is_file()}
        command = [sys.executable, str(Path(env.__file__)), "--repo", str(self.repo)]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["Projects"][0]["Kind"], "PYTHON")
        self.assertEqual(before, {path.relative_to(self.repo): path.read_bytes() for path in self.repo.rglob("*") if path.is_file()})
        ordinary = self.repo / "ordinary"
        ordinary.mkdir()
        altered = dict(os.environ, GIT_DIR=str(self.repo / ".git"), GIT_WORK_TREE=str(ordinary))
        rejected = subprocess.run([*command[:-1], str(ordinary)], capture_output=True, text=True, check=False, env=altered)
        self.assertEqual(rejected.returncode, 2)


if __name__ == "__main__":
    unittest.main()
