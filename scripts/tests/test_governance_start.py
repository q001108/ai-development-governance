"""Startup context selection, retry safety, read-only behavior and pinned routing."""
import hashlib
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
import governance_requirements as r
import governance_start as startup
import governance_status as status
sys.path.pop(0)


class StartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory()
        cls.release = Path(cls.shared.name) / "release"
        package.build_package(Path(__file__).resolve().parents[2], cls.release, allow_dirty=True)

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "project with spaces"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Startup Fixture")
        self.git("config", "user.email", "startup@test.invalid")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "governance.developerId", "TEST")
        (self.repo / "AGENTS.md").write_text("# Synthetic project\n", encoding="utf-8")
        self.commit()
        project.apply_project(self.repo, project.preview_project(self.repo, self.release))
        r.enable(self.repo)
        (self.repo / ".governance/local").rmdir()
        self.ident = "REQ-TEST-20261029-01"

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True,
                                env={k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")})
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.decode("utf-8").strip()

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "synthetic startup fixture")
        return self.git("rev-parse", "HEAD")

    def snapshot(self):
        return {p.relative_to(self.repo).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.repo.rglob("*") if p.is_file() and ".git" not in p.relative_to(self.repo).parts}

    def start(self, intent="continue", ident=None, **kwargs):
        return startup.start(self.repo, intent, ident, fetch=False, **kwargs)

    def new(self, ident=None):
        return self.start("new", ident or self.ident, title="部门筛选", instruction="实现员工列表按部门筛选。")

    def policy(self, branch="feature/approved-delivery", origin=False):
        config = project.project_config(self.repo)
        direct = {"Branches": [branch], "PolicyRef": "AGENTS.md", "Instruction": "沿用项目明确批准的直接开发要求。"}
        if origin:
            direct["IsolatedOriginRef"] = ".governance/local/worktree-origin.json"
        config["Startup"] = {"ProjectName": "Example", "DirectDevelopment": direct}
        (self.repo / project.PROJECT).write_bytes(project.canonical(config) + b"\n")

    def test_empty_query_creates_no_runtime_directory_or_other_file(self):
        before = self.snapshot()
        result = self.start("query")
        self.assertEqual(result["Outcome"], "NO_ACTIVE_REQUIREMENT")
        self.assertTrue(result["ReadOnly"])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.repo / ".governance/local").exists())

    def test_single_unassociated_requirement_is_not_implicitly_selected(self):
        r.register(self.repo, "Existing", "Implement existing request.", self.ident)
        before = self.snapshot()
        result = self.start()
        self.assertEqual(result["Outcome"], "SELECTION_REQUIRED")
        self.assertIsNone(result["RequirementId"])
        self.assertEqual(len(result["Candidates"]), 1)
        self.assertEqual(self.snapshot(), before)

    def test_new_then_continue_reuses_the_record_and_preserves_dirty_code(self):
        source = self.repo / "user_code.py"
        source.write_bytes(b"# existing uncommitted user work\n")
        first = self.new()
        second = self.start()
        self.assertEqual(second["RequirementId"], first["RequirementId"])
        self.assertEqual(second["SelectionBasis"], "LOCAL_WORKTREE_ASSOCIATION")
        self.assertEqual(second["Requirement"]["CurrentStage"], "WAITING_FOR_G1")
        self.assertEqual(source.read_bytes(), b"# existing uncommitted user work\n")
        root = r.root_for(self.repo, self.ident)
        self.assertFalse((root / "facts").exists())
        self.assertFalse((root / "开发记录.md").exists())
        self.assertFalse(second["ExecutionAuthorized"])
        self.assertFalse(second["TestsStarted"])

    def test_repeated_new_with_explicit_id_is_idempotent(self):
        self.new(); self.new()
        state = r.records(self.repo, self.ident)
        self.assertEqual([e["Action"] for e in state["Events"]], ["BEGIN"])

    def test_request_receipt_prevents_duplicate_after_partial_failure(self):
        real = startup.invoke
        def fail_registration(repo, tool, *args, **kwargs):
            if tool == "governance_requirements" and args[0] == "init":
                raise OSError("synthetic interruption after reservation")
            return real(repo, tool, *args, **kwargs)
        with mock.patch.object(startup, "invoke", side_effect=fail_registration):
            with self.assertRaises(OSError):
                self.start("new", title="New", instruction="Implement new work.", request_key="stable-01")
        receipt = next((self.repo / ".governance/local/startup/requests").glob("*.json"))
        ident = json.loads(receipt.read_bytes())["RequirementId"]
        first = self.start("new", title="New", instruction="Implement new work.", request_key="stable-01")
        second = self.start("new", title="New", instruction="Implement new work.", request_key="stable-01")
        self.assertEqual(first["RequirementId"], ident)
        self.assertEqual(second["RequirementId"], ident)
        self.assertEqual(len(r.records(self.repo, ident)["Events"]), 1)
        with self.assertRaises(ValueError):
            self.start("new", title="Changed", instruction="Implement other work.", request_key="stable-01")

    def test_new_missing_developer_or_request_key_returns_action_without_writes(self):
        self.git("config", "--unset", "governance.developerId")
        before = self.snapshot()
        result = self.start("new", title="New", instruction="Implement new work.", request_key="key")
        self.assertEqual(result["NextActions"][0]["Action"], "CONFIGURE_LOCAL_DEVELOPER_ID_ONCE")
        self.assertEqual(self.snapshot(), before)
        self.git("config", "governance.developerId", "TEST")
        result = self.start("new", title="New", instruction="Implement new work.")
        self.assertEqual(result["NextActions"][0]["Action"], "PROVIDE_STABLE_NEW_REQUEST_KEY")
        self.assertEqual(self.snapshot(), before)

    def test_query_with_context_never_refreshes_or_updates_association(self):
        self.new()
        before = self.snapshot()
        result = self.start("query")
        self.assertEqual(result["RequirementId"], self.ident)
        self.assertTrue(result["ReadOnly"])
        self.assertEqual(self.snapshot(), before)

    def test_multiple_unassociated_candidates_require_selection(self):
        for ident in (self.ident, "REQ-TEST-20261029-02"):
            r.register(self.repo, ident, "Implement separate work.", ident)
        before = self.snapshot()
        self.assertEqual(self.start()["Outcome"], "SELECTION_REQUIRED")
        self.assertEqual(self.snapshot(), before)
        selected = self.start(ident=self.ident)
        self.assertEqual(selected["RequirementId"], self.ident)
        self.assertEqual(self.start()["RequirementId"], self.ident)

    def test_branch_switch_does_not_inherit_another_branch_association(self):
        self.new()
        self.git("checkout", "-qb", "feature/other")
        self.assertEqual(self.start()["Outcome"], "SELECTION_REQUIRED")
        self.git("checkout", "-q", "main")
        self.assertEqual(self.start("query")["RequirementId"], self.ident)

    def test_branch_id_is_evidence_and_conflicting_context_requires_selection(self):
        r.register(self.repo, "First", "Implement first work.", self.ident)
        other = "REQ-TEST-20261029-02"
        r.register(self.repo, "Second", "Implement second work.", other)
        self.git("checkout", "-qb", "feature/" + self.ident + "-export")
        self.assertEqual(self.start("query")["SelectionBasis"], "BRANCH_REQUIREMENT_ID")
        self.start(ident=other)
        before = self.snapshot()
        result = self.start()
        self.assertEqual(result["Outcome"], "SELECTION_REQUIRED")
        self.assertIn("BRANCH_AND_LOCAL_ASSOCIATION_CONFLICT", result["Problems"])
        self.assertEqual(self.snapshot(), before)

    def test_corrupt_context_is_reported_without_automatic_repair(self):
        self.new()
        path = self.repo / startup.context_name(startup.branch_state(self.repo))
        path.write_bytes(b"{}\n")
        before = self.snapshot()
        result = self.start("query")
        self.assertEqual(result["Outcome"], "SELECTION_REQUIRED")
        self.assertIn("STALE_OR_INVALID_LOCAL_ASSOCIATION", result["Problems"])
        self.assertEqual(self.snapshot(), before)

    def test_project_direct_mode_persists_and_explicit_resume_wins(self):
        self.git("checkout", "-qb", "feature/approved-delivery")
        self.policy()
        first = self.new()
        self.assertEqual(first["Requirement"]["Mode"], "MANUAL")
        self.assertEqual(first["Requirement"]["CurrentStage"], "MANUAL_TRACKING")
        self.assertEqual(self.start()["Requirement"]["Mode"], "MANUAL")
        r.resume(self.repo, self.ident, "明确恢复治理。", "当前仅有需求草稿，尚无业务修改。")
        result = self.start()
        self.assertEqual(result["Requirement"]["Mode"], "GOVERNED")
        self.assertEqual(result["Requirement"]["CurrentStage"], "WAITING_FOR_G1")

    def test_query_does_not_apply_direct_policy_and_dev_does_not_inherit_it(self):
        self.policy()
        self.git("checkout", "-qb", "feature/approved-delivery")
        r.register(self.repo, "Draft", "Implement a draft.", self.ident)
        before = self.snapshot()
        result = self.start("query", self.ident)
        self.assertEqual(result["ProjectPolicy"]["Mode"], "MANUAL")
        self.assertEqual(result["Requirement"]["Mode"], "GOVERNED")
        self.assertEqual(self.snapshot(), before)
        self.git("checkout", "-qb", "dev")
        self.assertEqual(self.start("query", self.ident)["ProjectPolicy"]["Mode"], "GOVERNED")

    def test_isolated_policy_requires_exact_creation_commit(self):
        self.policy(origin=True)
        base = self.git("rev-parse", "HEAD")
        self.git("checkout", "--detach", "-q", base)
        target = self.repo / ".governance/local/worktree-origin.json"
        target.parent.mkdir(parents=True)
        value = {"Purpose": "ISOLATED_ADAPTER_TEST", "SourceRef": "refs/heads/feature/approved-delivery", "BaseCommit": base}
        target.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(self.start("query")["ProjectPolicy"]["Mode"], "MANUAL")
        value["BaseCommit"] = "0" * 40
        target.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(self.start("query")["ProjectPolicy"]["Mode"], "GOVERNED")

    def test_closed_manual_iteration_is_not_restarted(self):
        self.new()
        r.takeover(self.repo, self.ident, "当前需求退出治理。")
        commit = self.commit()
        r.close_iteration(self.repo, self.ident, "明确终止。", "TERMINATED", "本轮终止。", commit)
        before = self.snapshot()
        result = self.start(ident=self.ident)
        self.assertEqual(result["Outcome"], "ITERATION_CLOSED")
        self.assertEqual(result["Requirement"]["CurrentStage"], "ITERATION_CLOSED")
        self.assertEqual(result["Status"]["CurrentStage"], "ITERATION_CLOSED")
        self.assertEqual(self.snapshot(), before)

    def test_incomplete_plan_returns_structured_blocked_state(self):
        self.new()
        (r.root_for(self.repo, self.ident) / "control-profile.json").write_bytes(b"{}\n")
        result = self.start("query", self.ident)
        self.assertEqual(result["Outcome"], "BLOCKED")
        self.assertEqual(result["Requirement"]["CurrentStage"], "UNDETERMINED")
        self.assertTrue(result["Problems"])
        self.assertFalse(result["ExecutionAuthorized"])

    def test_historical_and_tampered_bindings_are_not_adopted(self):
        self.new()
        path = r.root_for(self.repo, self.ident) / "plugin-binding.json"
        value = json.loads(path.read_bytes())
        value["PackageDigest"] = "f" * 64
        path.write_text(json.dumps(value), encoding="utf-8")
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.start("query", self.ident)
        self.assertEqual(self.snapshot(), before)

    def test_pinned_runner_alias_and_mode_exit_are_usable(self):
        self.new()
        r.takeover(self.repo, self.ident, "退出治理继续普通开发。")
        result = subprocess.run([sys.executable, "-B", str(self.repo / ".governance/run.py"), "start", "--repo", str(self.repo),
                                 "--intent", "query", "--requirement-id", self.ident], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["Requirement"]["Mode"], "MANUAL")
        self.assertEqual(report["NextActions"][0]["Action"], "CONTINUE_ORDINARY_DEVELOPMENT_AND_MINIMAL_TRACKING")

    def test_upgrade_router_still_calls_older_requirement_status(self):
        old = Path(self.temp.name) / "old-release"
        shutil.copytree(self.release, old)
        manifest = json.loads((old / "release.json").read_bytes())
        manifest["PluginVersion"] = "0.1.0-dev.0"
        plugin = json.loads((old / "plugin.json").read_bytes())
        plugin["version"] = manifest["PluginVersion"]
        (old / "plugin.json").write_bytes(project.canonical(plugin) + b"\n")
        (old / "scripts/governance_status.py").write_text(
            'import json\nprint(json.dumps({"QueryStatus":"VERIFIED_SUPPORTED_STATE","CurrentStage":"OLDER_BOUND_STAGE","NextActions":[]}))\n', encoding="utf-8")
        for name in ("scripts/governance_start.py", "docs/工具说明/统一启动与续接.md"):
            manifest["Files"].pop(name)
            (old / name).unlink()
        manifest["Files"] = {name: tracking_hash(old / name) for name in manifest["Files"]}
        (old / "release.json").write_bytes(project.canonical(manifest) + b"\n")
        self.commit()
        project.apply_project(self.repo, project.preview_project(self.repo, old))
        self.new()
        self.commit()
        project.apply_project(self.repo, project.preview_project(self.repo, self.release))
        result = self.start("query", self.ident)
        self.assertEqual(result["Requirement"]["CurrentStage"], "OLDER_BOUND_STAGE")
        self.assertEqual(result["Requirement"]["PluginVersion"], "0.1.0-dev.0")


def tracking_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
