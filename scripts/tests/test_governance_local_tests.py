"""Synthetic local processes and isolated data; no business project or server access."""

import copy
import ctypes
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
import contextlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import governance_local_tests as local
sys.path.pop(0)
import test_governance_tools as fixtures


SUPPORT = r'''
import json, os, socket, subprocess, sys, time
from pathlib import Path
action = sys.argv[1]
audit = Path(os.environ['GOV_TEST_AUDIT_DIR'])
audit.mkdir(exist_ok=True)
if 'B1_RUN_DIR' in os.environ:
    (audit / 'run_dir').write_text(os.environ['B1_RUN_DIR'], encoding='utf-8')
if action in ('server', 'server_child'):
    if action == 'server_child':
        child = subprocess.Popen([sys.executable, __file__, 'sleep'])
        (audit / 'child_pid').write_text(str(child.pid))
    (audit / 'server_pid').write_text(str(os.getpid()))
    listener = socket.socket()
    if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    listener.bind(('127.0.0.1', int(os.environ['B1_API_PORT'])))
    listener.listen()
    while True:
        connection, _ = listener.accept()
        with connection:
            connection.recv(128)
            connection.sendall(os.environ['B1_RUN_ID'].encode())
elif action == 'api_check':
    try:
        with socket.create_connection(('127.0.0.1', int(os.environ['B1_API_PORT'])), timeout=.2) as connection:
            connection.sendall(b'check')
            assert connection.recv(128).decode() == os.environ['B1_RUN_ID']
    except (OSError, AssertionError):
        sys.exit(1)
elif action == 'seed':
    (Path(os.environ['B1_DATA_DIR']) / 'fixture.json').write_text(json.dumps({'RunId': os.environ['B1_RUN_ID']}))
elif action == 'data_check':
    try:
        assert json.loads((Path(os.environ['B1_DATA_DIR']) / 'fixture.json').read_text())['RunId'] == os.environ['B1_RUN_ID']
    except (OSError, AssertionError):
        sys.exit(1)
elif action == 'unit':
    (audit / 'unit_ran').write_text('observed')
elif action == 'integration':
    assert (Path(os.environ['B1_DATA_DIR']) / 'fixture.json').exists()
    (audit / 'test_port').write_text(os.environ['B1_API_PORT'])
elif action == 'fail':
    print('password=synthetic-private-value')
    sys.exit(1)
elif action == 'mutate':
    Path('AGENTS.md').write_text('unexpected project modification')
elif action == 'sleep':
    time.sleep(60)
'''


def process_alive(pid):
    if os.name == "nt":
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        kernel.WaitForSingleObject.argtypes, kernel.WaitForSingleObject.restype = [w.HANDLE, w.DWORD], w.DWORD
        kernel.CloseHandle.argtypes = [w.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 1000) == 0x102
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class LocalTests(unittest.TestCase):
    def setUp(self):
        # Reuse the repository's existing valid plan fixture without inheriting its tests.
        self.fixture = fixtures.V3PlanValidationTests(methodName="test_valid_lean_and_readonly_cli")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo = self.fixture.repo
        self.requirement = self.fixture.profile["RequirementId"]
        self.audit_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.audit_temp.cleanup)
        self.audit = Path(self.audit_temp.name)
        self.env_patch = mock.patch.dict(os.environ, {"GOV_TEST_AUDIT_DIR": str(self.audit)})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        (self.repo / "support.py").write_text(SUPPORT, encoding="utf-8")
        self.methods = {"RB-001": "python support.py unit", "QG-001": "python support.py integration"}
        self.config = {
            "LocalTestProtocolVersion": "v1.0",
            "PassThrough": ["GOV_TEST_AUDIT_DIR"],
            "Commands": {method: {"Argv": ["{python}", "support.py", action], "TimeoutSeconds": 5}
                         for method, action in zip(self.methods.values(), ("unit", "integration"))},
            "Resources": {
                "API": {"Kind": "SERVICE", "Requires": [], "Start": ["{python}", "support.py", "server"],
                        "Check": ["{python}", "support.py", "api_check"], "TimeoutSeconds": 3,
                        "Port": 0, "AllowPortOverride": True},
                "DATA": {"Kind": "FIXTURE", "Requires": ["API"], "Prepare": ["{python}", "support.py", "seed"],
                         "Check": ["{python}", "support.py", "data_check"], "TimeoutSeconds": 3},
            },
        }
        for control in self.fixture.plan["Controls"]:
            if control["ControlId"] in self.methods:
                control.update(MethodKind="COMMAND", Method=self.methods[control["ControlId"]],
                               ExecutionPlatform={"win32": "WINDOWS", "darwin": "MACOS", "linux": "LINUX"}[sys.platform])
            elif control["Class"] == "AD":
                control.update(Triggered=False, EstimatedMinutes=0, RiskTargetId=None)
        self.fixture.profile["PlatformScope"]["LocalDevelopment"] = [control["ExecutionPlatform"] for control in self.fixture.plan["Controls"] if control["Class"] == "QG"]
        self.fixture.profile["ExecutionBudget"]["AdvisoryMinutes"] = 0
        self.context = self.fixture.requirement_dir / "开发上下文包.md"
        self.body = (
            "# 开发上下文包\n\n- **产物版本：** v1.0\n- **产物当前状态：** FROZEN\n\n"
            "#### 5.2.1 验证定义\n\n"
            "| `ValidationId` | 对应 ControlId | 行为 | 已核实的方法或命令 | 证据 |\n|---|---|---|---|---|\n"
            "| V-01 | RB-001 | unit | python support.py unit | report |\n"
            "| V-02 | QG-001 | local integration | python support.py integration | report |\n\n"
            "#### 5.2.2 验证执行控制\n\n"
            "| `ValidationId` | `Source` | Gate | Owner | Prerequisites | Reuse | Cost |\n|---|---|---|---|---|---|---|\n"
            "| V-01 | RB-001 | BLOCKING / CANDIDATE_QUALITY / TRUE | WRITER / WP-01 | NONE / N/A / N/A | RERUN_REQUIRED | S |\n"
            "| V-02 | QG-001 | BLOCKING / CANDIDATE_QUALITY / TRUE | WRITER / WP-01 | DATABASE / PLANNED / LOCAL:DATA | RERUN_REQUIRED | S |\n"
        )
        self.context.write_text(self.body, encoding="utf-8")
        gate = copy.deepcopy(self.fixture.gate)
        gate.update(EventId="GATE-002", ContextPackVersion="v1.0")
        gate["Payload"].update(Gate="G2", Decision="FROZEN", ArtifactRef=self.context.relative_to(self.repo).as_posix(), ArtifactVersion="v1.0")
        (self.fixture.requirement_dir / "facts/gates/GATE-002.json").write_text(json.dumps(gate, ensure_ascii=False), encoding="utf-8")
        self.fixture.write()
        self.save_config()

    def save_config(self):
        (self.repo / local.CONFIG_NAME).write_text(json.dumps(self.config), encoding="utf-8")

    def request(self):
        return local.project(self.repo, self.requirement)

    def assert_cleaned(self, report):
        self.assertEqual(report["Cleanup"], "COMPLETED", report)
        if (self.audit / "run_dir").exists():
            self.assertFalse(Path((self.audit / "run_dir").read_text(encoding="utf-8")).exists())
        for name in ("server_pid", "child_pid"):
            if (self.audit / name).exists():
                self.assertFalse(process_alive(int((self.audit / name).read_text())), name)

    def test_project_sources_generate_selection_without_a_task_manifest(self):
        before = local.source_state(self.repo)
        request = self.request()
        self.assertEqual([item["ValidationId"] for item in request["Selected"]], ["V-01", "V-02"])
        self.assertEqual(request["Selected"][1]["Requires"], ["DATA"])
        self.assertFalse(any(item["InputIssue"] for item in request["Selected"]))
        self.assertEqual(before, local.source_state(self.repo))
        self.assertFalse((self.audit / "server_pid").exists())

    def test_prepare_run_and_cleanup_share_port_data_and_leave_repository_unchanged(self):
        before = local.source_state(self.repo)
        report = local.execute(self.request(), run_tests=True)
        self.assertEqual(report["Issues"], [], report)
        self.assertTrue(all(item["Observation"]["Status"] == "SUCCEEDED" for item in report["Validations"]))
        self.assertEqual(int((self.audit / "test_port").read_text()), report["Resources"]["API"]["Port"])
        self.assertEqual(before, local.source_state(self.repo))
        self.assertFalse(report["QualityPassed"])
        self.assertFalse(report["FormalReadinessWritten"])
        self.assert_cleaned(report)

    def test_busy_port_is_overridden_without_touching_existing_listener(self):
        with socket.socket() as other:
            other.bind(("127.0.0.1", 0))
            other.listen()
            occupied = other.getsockname()[1]
            self.config["Resources"]["API"]["Port"] = occupied
            self.save_config()
            report = local.execute(self.request(), run_tests=True)
            self.assertNotEqual(report["Resources"]["API"]["Port"], occupied)
            self.assertTrue(report["Resources"]["API"]["PortChanged"])
            self.assertEqual(other.getsockname()[1], occupied)
        self.assert_cleaned(report)

    def test_fixed_conflicting_port_blocks_only_the_affected_validation(self):
        with socket.socket() as other:
            other.bind(("127.0.0.1", 0))
            other.listen()
            self.config["Resources"]["API"].update(Port=other.getsockname()[1], AllowPortOverride=False)
            self.save_config()
            report = local.execute(self.request(), run_tests=True)
        self.assertEqual(report["Resources"]["API"]["Reason"], "PORT_CONFLICT")
        self.assertEqual(report["Validations"][0]["Observation"]["Status"], "SUCCEEDED")
        self.assertEqual(report["Validations"][1]["Execution"], "NOT_EXECUTED")
        self.assert_cleaned(report)

    def test_service_failure_does_not_prevent_an_unrelated_unit_test(self):
        self.config["Resources"]["API"]["Start"][-1] = "fail"
        self.save_config()
        report = local.execute(self.request(), run_tests=True)
        self.assertEqual(report["Validations"][0]["Observation"]["Status"], "SUCCEEDED")
        self.assertEqual(report["Validations"][1]["Readiness"], "NOT_READY")
        self.assertNotIn("synthetic-private-value", json.dumps(report))
        self.assert_cleaned(report)

    def test_failed_fixture_is_not_repeated_and_is_not_a_quality_failure(self):
        self.config["Resources"]["DATA"]["Prepare"][-1] = "fail"
        self.save_config()
        report = local.execute(self.request(), run_tests=True)
        self.assertEqual(report["Resources"]["DATA"]["Reason"], "PREPARATION_FAILED")
        self.assertEqual(report["Resources"]["DATA"]["Preparation"]["ExitCode"], 1)
        self.assertEqual(report["Validations"][1]["Execution"], "NOT_EXECUTED")
        self.assert_cleaned(report)

    def test_missing_entry_and_unmapped_prerequisites_are_explicit(self):
        self.config["Commands"].pop(self.methods["QG-001"])
        self.save_config()
        request = self.request()
        self.assertEqual(request["Selected"][1]["InputIssue"], "MISSING_PROJECT_ENTRY")
        self.context.write_text(self.body.replace("DATABASE / PLANNED / LOCAL:DATA", "DATABASE / PLANNED / ask developer"), encoding="utf-8")
        self.config["Commands"][self.methods["QG-001"]] = {"Argv": ["{python}", "support.py", "integration"], "TimeoutSeconds": 5}
        self.save_config()
        request = self.request()
        self.assertEqual(request["Selected"][1]["InputIssue"], "UNMAPPED_PREREQUISITE")
        report = local.execute(request, run_tests=True)
        self.assertNotIn("API", report["Resources"])
        self.assertEqual(report["Validations"][0]["Observation"]["Status"], "SUCCEEDED")

    def test_frozen_sources_gates_owners_and_methods_cannot_be_guessed(self):
        self.context.write_text(self.body.replace("FROZEN", "DRAFT"), encoding="utf-8")
        with self.assertRaises(local.g.Invalid):
            self.request()
        self.context.write_text(self.body.replace("python support.py unit | report", "python support.py other | report"), encoding="utf-8")
        with self.assertRaises(local.g.Invalid):
            self.request()
        self.context.write_text(self.body, encoding="utf-8")
        with self.assertRaises(local.g.Invalid):
            local.project(self.repo, self.requirement, "QA")
        (self.fixture.requirement_dir / "facts/gates/GATE-002.json").unlink()
        with self.assertRaises(local.g.Invalid):
            self.request()

    def test_cycles_shells_and_sensitive_configuration_are_rejected_before_execution(self):
        self.config["Resources"]["API"]["Requires"] = ["DATA"]
        self.save_config()
        with self.assertRaises(local.g.Invalid):
            self.request()
        self.config["Resources"]["API"]["Requires"] = []
        self.config["Commands"][self.methods["RB-001"]]["Argv"] = ["cmd.exe", "/c", "anything"]
        self.save_config()
        with self.assertRaises(local.g.Invalid):
            self.request()
        self.config["Commands"][self.methods["RB-001"]]["Argv"] = ["{python}", "password=synthetic-private-value"]
        self.save_config()
        with self.assertRaises(local.g.Invalid):
            self.request()
        self.assertFalse((self.audit / "server_pid").exists())

    def test_changed_pinned_inputs_are_rejected_before_preparation(self):
        request = self.request()
        self.config["Resources"]["API"]["Port"] = 18081
        self.save_config()
        with self.assertRaises(local.g.Invalid):
            local.execute(request, run_tests=True)
        self.assertFalse((self.audit / "server_pid").exists())

    def test_repository_changes_are_reported_and_never_reverted(self):
        self.config["Commands"][self.methods["RB-001"]]["Argv"][-1] = "mutate"
        self.save_config()
        report = local.execute(self.request(), run_tests=True)
        self.assertIn("REPOSITORY_CHANGED", report["Issues"])
        self.assertEqual((self.repo / "AGENTS.md").read_text(), "unexpected project modification")
        self.assertEqual(report["Validations"][1]["Execution"], "NOT_EXECUTED")
        self.assert_cleaned(report)

    def test_timeout_and_test_failure_keep_observations_and_cleanup_descendants(self):
        self.config["Resources"]["API"]["Start"][-1] = "server_child"
        self.config["Commands"][self.methods["RB-001"]] = {"Argv": ["{python}", "support.py", "sleep"], "TimeoutSeconds": 1}
        self.config["Commands"][self.methods["QG-001"]]["Argv"][-1] = "fail"
        self.save_config()
        report = local.execute(self.request(), run_tests=True)
        self.assertEqual(report["Validations"][0]["Observation"]["Status"], "TIMED_OUT")
        self.assertEqual(report["Validations"][1]["Observation"]["Status"], "NONZERO_EXIT")
        self.assertNotIn("synthetic-private-value", json.dumps(report))
        self.assert_cleaned(report)

    def test_runtime_dispatch_holds_environment_alive_and_releases_it_on_exception(self):
        request = self.request()
        def dispatch(ready, child_env, observations):
            self.assertEqual([item["ValidationId"] for item in ready], ["V-01", "V-02"])
            self.assertTrue(Path(child_env["B1_DATA_DIR"]).exists())
            self.assertTrue(process_alive(int((self.audit / "server_pid").read_text())))
            self.assertEqual(observations["Resources"]["DATA"]["Status"], "READY")
            raise RuntimeError("synthetic dispatcher interrupted")
        with self.assertRaisesRegex(RuntimeError, "dispatcher interrupted"):
            local.execute(request, dispatch=dispatch)
        self.assertFalse(Path((self.audit / "run_dir").read_text(encoding="utf-8")).exists())
        self.assertFalse(process_alive(int((self.audit / "server_pid").read_text())))

    def test_cli_preview_is_readonly_and_input_errors_are_sanitized(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = local.main(["--repo", str(self.repo), "--requirement-id", self.requirement])
        self.assertEqual((code, stderr.getvalue()), (0, ""))
        self.assertFalse(json.loads(stdout.getvalue())["CommandsExecuted"])
        with mock.patch.dict(os.environ, {"GIT_DIR": "password=synthetic-private-value"}), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            self.assertEqual(local.main(["--repo", str(self.repo), "--requirement-id", self.requirement]), 2)
        self.assertNotIn("synthetic-private-value", stderr.getvalue())
        self.assertFalse((self.audit / "server_pid").exists())

    def test_existing_package_metadata_is_a_single_reusable_configuration_source(self):
        (self.repo / local.CONFIG_NAME).unlink()
        (self.repo / "package.json").write_text(json.dumps({"name": "synthetic", "governance": {"localTests": self.config}}), encoding="utf-8")
        request = self.request()
        self.assertIn("package.json", request["Sources"])
        self.assertFalse(any(item["InputIssue"] for item in request["Selected"]))
        self.save_config()
        with self.assertRaises(local.g.Invalid):
            self.request()

    def test_absent_project_configuration_produces_missing_entry_instead_of_a_new_checklist(self):
        (self.repo / local.CONFIG_NAME).unlink()
        request = self.request()
        self.assertTrue(all(item["InputIssue"] == "MISSING_PROJECT_ENTRY" for item in request["Selected"]))
        report = local.execute(request, run_tests=True)
        self.assertEqual(report["Resources"], {})
        self.assertEqual(report["PreparationSummary"], "NOT_READY")
        self.assertTrue(report["NextActions"])

    def test_resume_subset_only_prepares_and_runs_its_selected_validation(self):
        report = local.execute(self.request(), run_tests=True, validation_ids=["V-01"])
        self.assertEqual(report["InvocationValidationIds"], ["V-01"])
        self.assertEqual(report["Resources"], {})
        self.assertTrue((self.audit / "unit_ran").exists())
        self.assertFalse((self.audit / "server_pid").exists())
        with self.assertRaises(local.g.Invalid):
            local.execute(self.request(), run_tests=True, validation_ids=["V-99"])

    def test_runtime_can_append_readiness_without_mutating_the_candidate(self):
        def dispatch(ready, child_env, observations):
            target = self.fixture.requirement_dir / "facts/readiness/TR-001.json"
            target.parent.mkdir()
            target.write_text(json.dumps({"synthetic": "runtime owns this fact"}), encoding="utf-8")
        report = local.execute(self.request(), dispatch=dispatch)
        self.assertEqual(report["Issues"], [])
        self.assertEqual(report["DispatchedValidationIds"], ["V-01", "V-02"])
        self.assert_cleaned(report)

    def test_hybrid_unmapped_prerequisite_text_is_not_partially_accepted(self):
        self.context.write_text(self.body.replace("LOCAL:DATA", "LOCAL:DATA plus unknown login"), encoding="utf-8")
        request = self.request()
        self.assertEqual(request["Selected"][1]["InputIssue"], "UNMAPPED_PREREQUISITE")

    def test_corrections_are_reported_until_runtime_resolves_them(self):
        path = self.fixture.requirement_dir / "facts/corrections/FIX-001.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"EventType": "FACT_CORRECTION"}), encoding="utf-8")
        with self.assertRaises(local.g.Invalid):
            self.request()

    def test_missing_tool_is_detected_before_starting_its_services(self):
        self.config["Commands"][self.methods["QG-001"]]["Argv"][0] = "missing-synthetic-tool"
        self.save_config()
        report = local.execute(self.request(), run_tests=True)
        self.assertEqual(report["Validations"][1]["Reason"], "MISSING_TOOL")
        self.assertFalse((self.audit / "server_pid").exists())
        self.assertEqual(report["Resources"], {})

    def test_failure_report_is_retained_without_process_output_or_private_values(self):
        self.config["Resources"]["DATA"]["Prepare"][-1] = "fail"
        self.save_config()
        report = local.execute(self.request(), run_tests=True)
        path = Path(report["LocalReportPath"])
        self.assertTrue(path.is_file())
        self.assertTrue(path.is_relative_to(self.repo / ".git"))
        retained = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(retained["RunId"], report["RunId"])
        self.assertEqual(retained["Cleanup"], "COMPLETED")
        self.assertNotIn("synthetic-private-value", path.read_text(encoding="utf-8"))

    def test_adopted_workspace_runtime_runs_without_git_writes_or_system_temp(self):
        adaptation = self.repo / ".governance/project.json"
        adaptation.parent.mkdir()
        adaptation.write_text(json.dumps({"ProjectFormatVersion": "v1.0", "LocalRuntimeDirectory": ".governance/local"}), encoding="utf-8")
        (self.repo / ".gitignore").write_text("/.governance/local/\n", encoding="utf-8")
        before = local.source_state(self.repo)
        with mock.patch.object(local.tempfile, "mkdtemp", side_effect=AssertionError("adopted runtime must stay in workspace")):
            report = local.execute(self.request(), run_tests=True)
        self.assertEqual(report["Cleanup"], "COMPLETED", report)
        self.assertEqual(report["Issues"], [])
        self.assertTrue(all(item["Observation"]["Status"] == "SUCCEEDED" for item in report["Validations"]))
        self.assertTrue(Path(report["LocalReportPath"]).is_relative_to(self.repo / ".governance/local"))
        self.assertEqual(list((self.repo / ".governance/local/runs").iterdir()), [])
        self.assertEqual(before, local.source_state(self.repo))
        self.assert_cleaned(report)

    def test_workspace_runtime_requires_exact_adaptation_and_git_ignore(self):
        adaptation = self.repo / ".governance/project.json"
        adaptation.parent.mkdir()
        for value in ("src", "../outside", ".governance/local"):
            with self.subTest(directory=value):
                adaptation.write_text(json.dumps({"ProjectFormatVersion": "v1.0", "LocalRuntimeDirectory": value}), encoding="utf-8")
                with self.assertRaises(local.g.Invalid):
                    local.runtime_root(self.repo)
        self.assertFalse((self.repo / ".governance/local").exists())

    def test_workspace_runtime_cannot_hide_existing_tracked_files(self):
        directory = self.repo / ".governance/local"
        directory.mkdir(parents=True)
        (directory / "tracked.txt").write_text("project-owned", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", ".governance/local/tracked.txt"], check=True, capture_output=True)
        (self.repo / ".gitignore").write_text("/.governance/local/\n", encoding="utf-8")
        (self.repo / ".governance/project.json").write_text(json.dumps({"ProjectFormatVersion": "v1.0", "LocalRuntimeDirectory": ".governance/local"}), encoding="utf-8")
        with self.assertRaisesRegex(local.g.Invalid, "no tracked files"):
            local.runtime_root(self.repo)

    def test_runtime_adaptation_is_bound_to_prepared_request(self):
        adaptation = self.repo / ".governance/project.json"
        adaptation.parent.mkdir()
        adaptation.write_text(json.dumps({"ProjectFormatVersion": "v1.0", "LocalRuntimeDirectory": ".governance/local"}), encoding="utf-8")
        (self.repo / ".gitignore").write_text("/.governance/local/\n", encoding="utf-8")
        request = self.request()
        self.assertIn(".governance/project.json", request["Sources"])
        self.assertIn(".gitignore", request["Sources"])
        adaptation.write_text(json.dumps({"ProjectFormatVersion": "v1.0"}), encoding="utf-8")
        with self.assertRaises(local.g.Invalid):
            local.execute(request, run_tests=True)

    def test_standard_qa_uses_runtime_dispatch_instead_of_writer_command_execution(self):
        self.fixture.profile.update(SelectedProfile="STANDARD", MinimumProfile="STANDARD")
        trigger = next(item for item in self.fixture.profile["Classification"]["UpgradeTriggers"] if item["Code"] == "INDEPENDENT_QUALITY_JUDGMENT")
        trigger["Matched"] = True
        for name in ("临时质量验证记录.md", "只读评审记录.md"):
            self.fixture.profile["RecordRefs"].append("docs/requirements/" + self.requirement + "/" + name)
        self.fixture.plan["Controls"][1].update(Owner="REVIEWER", MethodKind="STATIC_REVIEW", Method="static review")
        self.fixture.plan["Controls"][2]["Owner"] = "QA"
        self.context.write_text(self.body.replace("| WRITER / WP-01 | DATABASE", "| QA / WP-01 | DATABASE"), encoding="utf-8")
        self.fixture.write()
        request = self.request()
        self.assertEqual(request["Owner"], "QA")
        with self.assertRaises(local.g.Invalid):
            local.execute(request, run_tests=True)
        received = []
        report = local.execute(request, dispatch=lambda ready, _env, _observations: received.extend(item["ValidationId"] for item in ready))
        self.assertEqual(received, ["V-02"])
        self.assertFalse(report["QualityPassed"])
        self.assert_cleaned(report)

    def test_inherited_application_secrets_are_not_forwarded_or_recorded(self):
        observed = []
        with mock.patch.dict(os.environ, {"DATABASE_URL": "synthetic-private-value", "B1_OLD_PORT": "1"}):
            report = local.execute(self.request(), dispatch=lambda _ready, child_env, _observations: observed.append(child_env))
        self.assertNotIn("DATABASE_URL", observed[0])
        self.assertNotIn("B1_OLD_PORT", observed[0])
        self.assertNotIn("synthetic-private-value", json.dumps(report))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.case = LocalTests(methodName="test_prepare_run_and_cleanup_share_port_data_and_leave_repository_unchanged")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.fixture.profile.update(SelectedProfile="STANDARD", MinimumProfile="STANDARD")
        trigger = next(item for item in self.case.fixture.profile["Classification"]["UpgradeTriggers"] if item["Code"] == "INDEPENDENT_QUALITY_JUDGMENT")
        trigger["Matched"] = True
        for name in ("临时质量验证记录.md", "只读评审记录.md"):
            self.case.fixture.profile["RecordRefs"].append("docs/requirements/" + self.case.requirement + "/" + name)
        self.case.fixture.plan["Controls"][1].update(Owner="REVIEWER", MethodKind="STATIC_REVIEW", Method="static review")
        self.case.fixture.plan["Controls"][2]["Owner"] = "QA"
        self.case.context.write_text(self.case.body.replace("| WRITER / WP-01 | DATABASE", "| QA / WP-01 | DATABASE"), encoding="utf-8")
        self.case.fixture.write()
        self.case.config["Automatic"] = True
        self.case.save_config()
        sys.path.insert(0, str(Path(local.__file__).parent))
        import governance_test_session
        sys.path.pop(0)
        self.session = governance_test_session

    def test_atomic_session_write_keeps_temporary_sibling_path_short(self):
        root = self.case.repo
        folder = root / ("p" * max(1, 190 - len(str(root)) - 1))
        folder.mkdir()
        target = folder / ("a" * 32 + ".request.json")
        self.session.publish(target, {"ValidationId": "V-02"})
        self.assertEqual(self.session.read(target), {"ValidationId": "V-02"})
        self.assertEqual(list(folder.iterdir()), [target])

    def test_independent_qa_command_uses_the_live_prepared_environment(self):
        started = self.session.start(self.case.repo, self.case.requirement, seconds=30)
        ident = started["SessionId"]
        try:
            self.assertEqual(started["ReadyValidationIds"], ["V-02"])
            self.assertTrue(process_alive(int((self.case.audit / "server_pid").read_text())))
            observed = self.session.test(self.case.repo, ident, "V-02")
            self.assertEqual(observed["Observation"]["Status"], "SUCCEEDED")
            self.assertFalse(observed["QualityPassed"])
            self.assertTrue(observed["RepositoryUnchanged"])
            with self.assertRaises(local.g.Invalid):
                self.session.test(self.case.repo, ident, "V-99")
        finally:
            report = self.session.stop(self.case.repo, ident)
        self.case.assert_cleaned(report)

    def test_session_expiry_cleans_resources_without_a_stop_request(self):
        started = self.session.start(self.case.repo, self.case.requirement, seconds=1)
        folder = self.session.directory(self.case.repo, started["SessionId"])
        deadline = __import__("time").monotonic() + 12
        while not (folder / "error.json").exists() and __import__("time").monotonic() < deadline:
            __import__("time").sleep(.1)
        error = self.session.read(folder / "error.json")
        self.assertEqual(error["Cleanup"], "COMPLETED")
        self.assertFalse(process_alive(int((self.case.audit / "server_pid").read_text())))
        self.assertFalse(Path((self.case.audit / "run_dir").read_text(encoding="utf-8")).exists())

    def test_workspace_runtime_preserves_qa_session_across_calls(self):
        adaptation = self.case.repo / ".governance/project.json"
        adaptation.parent.mkdir()
        adaptation.write_text(json.dumps({"ProjectFormatVersion": "v1.0", "LocalRuntimeDirectory": ".governance/local"}), encoding="utf-8")
        (self.case.repo / ".gitignore").write_text("/.governance/local/\n", encoding="utf-8")
        started = self.session.start(self.case.repo, self.case.requirement, seconds=30)
        ident = started["SessionId"]
        try:
            self.assertTrue(self.session.directory(self.case.repo, ident).is_relative_to(self.case.repo / ".governance/local"))
            observed = self.session.test(self.case.repo, ident, "V-02")
            self.assertEqual(observed["Observation"]["Status"], "SUCCEEDED")
            self.assertTrue(observed["RepositoryUnchanged"])
        finally:
            stopped = self.session.stop(self.case.repo, ident)
        self.case.assert_cleaned(stopped)

    def test_automatic_session_requires_explicit_project_adoption(self):
        self.case.config["Automatic"] = False
        self.case.save_config()
        with self.assertRaises(local.g.Invalid):
            self.session.start(self.case.repo, self.case.requirement)
        self.assertFalse((self.case.audit / "server_pid").exists())

    def test_cli_start_exits_while_environment_remains_available_for_later_qa_calls(self):
        script = str(Path(self.session.__file__).resolve())
        common = ["--repo", str(self.case.repo)]
        started = subprocess.run([sys.executable, script, "start", *common, "--requirement-id", self.case.requirement, "--seconds", "30"],
                                 capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(started.returncode, 0, started.stderr)
        ident = json.loads(started.stdout)["SessionId"]
        try:
            self.assertTrue(process_alive(int((self.case.audit / "server_pid").read_text())))
            observed = subprocess.run([sys.executable, script, "test", *common, "--session-id", ident, "--validation-id", "V-02"],
                                      capture_output=True, text=True, encoding="utf-8", timeout=15)
            self.assertEqual(observed.returncode, 0, observed.stderr)
            self.assertEqual(json.loads(observed.stdout)["Observation"]["Status"], "SUCCEEDED")
        finally:
            stopped = subprocess.run([sys.executable, script, "stop", *common, "--session-id", ident],
                                     capture_output=True, text=True, encoding="utf-8", timeout=25)
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.case.assert_cleaned(json.loads(stopped.stdout))


if __name__ == "__main__":
    unittest.main()
