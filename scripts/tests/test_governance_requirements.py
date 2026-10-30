"""Synthetic registration, takeover, serial delivery and remote overview tests."""
import copy
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
import governance_overview as view
import governance_status as status
import governance_local_tests as local
import governance_tools as g
sys.path.pop(0)


class RequirementTrackingTests(unittest.TestCase):
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
        self.base = Path(self.temp.name)
        self.repo = self.base / "project"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Synthetic Developer")
        self.git("config", "user.email", "tracking@test.invalid")
        self.git("config", "core.autocrlf", "false")
        (self.repo / "AGENTS.md").write_text("# Synthetic project\n", encoding="utf-8")
        self.commit()
        project.apply_project(self.repo, project.preview_project(self.repo, self.release))
        self.req = "REQ-20260915-099"
        self.root = self.repo / "docs/requirements" / self.req

    def git(self, *args, repo=None):
        p = subprocess.run(["git", "-C", str(repo or self.repo), *args], capture_output=True,
                           env={k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")})
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.decode("utf-8").strip()

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "synthetic tracking fixture")
        return self.git("rev-parse", "HEAD")

    def enable(self, **kw):
        return r.enable(self.repo, **kw)

    def register(self):
        return r.register(self.repo, "导出优化", "实现导出优化；保留既有保存行为。", self.req)

    def frozen(self):
        import test_governance_local_tests as fixtures
        case = fixtures.LocalTests(methodName="test_prepare_run_and_cleanup_share_port_data_and_leave_repository_unchanged")
        case.setUp()
        self.addCleanup(case.doCleanups)
        shutil.copytree(case.fixture.requirement_dir, self.root, dirs_exist_ok=True)
        (self.root / "开发记录.md").unlink()  # Legacy fixture placeholder is not a G2 record.
        context = self.root / "开发上下文包.md"
        body = context.read_text(encoding="utf-8").replace("- **产物当前状态：** FROZEN", "- **产物当前状态：** FROZEN\n- **事实协议版本：** v3.0")
        body = body.replace("#### 5.2.1", "## 2. 工作包\n\n| WorkPackageId | Description |\n|---|---|\n| WP-01 | bounded local work |\n\n## 5. 验证\n\n#### 5.2.1")
        context.write_text(body, encoding="utf-8")
        gate_path = self.root / "facts/gates/GATE-002.json"
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
        gate["CreatedAt"] = r.now()
        gate_path.write_text(json.dumps(gate, ensure_ascii=False), encoding="utf-8")
        return case

    def test_enable_excludes_existing_directories_and_is_idempotent(self):
        old = self.repo / "docs/requirements/REQ-OLD-20261029-01"
        old.mkdir(parents=True)
        (old / "private-business.md").write_text("historical content", encoding="utf-8")
        first = self.enable()
        self.assertIn(old.name, first["ExcludedRequirementIds"])
        self.assertEqual(first, self.enable())
        self.assertEqual(view.overview(self.repo)["Entries"], [])
        with self.assertRaises(ValueError):
            r.register(self.repo, "legacy", "do not import legacy", old.name)

    def test_registration_creates_only_draft_and_bound_identity(self):
        self.enable()
        result = self.register()
        self.assertIsNone(result["Iteration"])
        self.assertFalse(result["GatesPassed"])
        self.assertTrue((self.root / "plugin-binding.json").is_file())
        self.assertTrue((self.root / "需求基线.md").is_file())
        self.assertFalse((self.root / "开发记录.md").exists())
        self.assertFalse((self.root / "facts").exists())
        self.assertFalse(self.register()["Created"])
        self.assertEqual(status.query_status(self.repo, self.req)["CurrentStage"], "WAITING_FOR_G1")
        with self.assertRaises(ValueError):
            r.register(self.repo, "different title", "different instruction", self.req)

    def test_allocator_uses_ignored_workspace_and_original_id_format(self):
        self.enable()
        self.git("config", "governance.developerId", "SYNTHETIC")
        first = r.register(self.repo, "new request", "implement local behavior")
        second = r.register(self.repo, "second request", "implement another local behavior")
        self.assertTrue(g.valid_requirement_id(first["RequirementId"]))
        self.assertNotEqual(first["RequirementId"], second["RequirementId"])
        self.assertTrue((self.repo / ".governance/local/id-reservations" / first["RequirementId"]).is_dir())
        self.assertFalse((self.repo / ".git/governance-id-reservations").exists())

    def test_query_is_readonly_and_refresh_does_not_commit(self):
        self.enable(); self.register()
        before = self.git("status", "--porcelain=v1", "--untracked-files=all")
        head = self.git("rev-parse", "HEAD")
        report = view.overview(self.repo)
        self.assertEqual(report["Entries"][0]["Stage"], "WAITING_FOR_G1")
        self.assertEqual(self.git("status", "--porcelain=v1", "--untracked-files=all"), before)
        self.assertFalse((self.root / view.PROJECTION).exists())
        refreshed = view.refresh(self.repo, fetch=False)
        self.assertTrue((self.repo / r.TABLE).read_text(encoding="utf-8").startswith(view.MARKER))
        self.assertEqual(refreshed["Entries"][0]["Publication"], "LOCAL_NOT_SHARED")
        self.assertEqual(self.git("rev-parse", "HEAD"), head)

    def test_unmanaged_table_is_never_overwritten(self):
        self.enable(); self.register()
        target = self.repo / r.TABLE
        target.write_text("company-maintained", encoding="utf-8")
        with self.assertRaises(ValueError):
            view.refresh(self.repo, fetch=False)
        self.assertEqual(target.read_text(encoding="utf-8"), "company-maintained")

    def test_takeover_persists_and_never_creates_acceptance(self):
        self.enable(); self.register()
        first = r.takeover(self.repo, self.req, "这条需求先退出治理，直接协助我开发")
        self.assertEqual(first["Mode"], "MANUAL")
        r.takeover(self.repo, self.req, "同一退出指令")
        self.assertEqual(len(r.records(self.repo, self.req)["Events"]), 2)
        self.assertFalse(status.query_status(self.repo, self.req)["HumanAccepted"])
        self.assertEqual(status.query_status(self.repo, self.req)["CurrentStage"], "MANUAL_TRACKING")
        with self.assertRaises(ValueError):
            r.require_governed(self.repo, self.req)
        r.delivery(self.repo, self.req, "我已经手动完成", "实现导出，无正式验收结论")
        self.assertEqual(view.overview(self.repo)["Entries"][0]["Stage"], "MANUAL_DELIVERY_RECORDED")
        self.assertFalse((self.root / "facts").exists())

    def test_resume_requires_review_and_old_candidate_cannot_be_reused(self):
        self.enable(); self.register()
        p = self.root / "facts/candidates/CAND-001.json"
        p.parent.mkdir(parents=True)
        p.write_text("{}", encoding="utf-8")
        r.takeover(self.repo, self.req, "退出治理")
        with self.assertRaises(ValueError):
            r.resume(self.repo, self.req, "恢复", "")
        r.resume(self.repo, self.req, "恢复治理", "已核对当前修改；旧候选需要重新采集")
        result = status.query_status(self.repo, self.req)
        self.assertEqual(result["CurrentStage"], "FRESH_CANDIDATE_REQUIRED_AFTER_RESUME")
        self.assertFalse(result["QualityPassed"])

    def test_g2_record_is_delayed_and_assignment_is_stable(self):
        self.enable(); self.register()
        with self.assertRaises((ValueError, OSError)):
            r.initialize_record(self.repo, self.req)
        self.frozen()
        result = r.initialize_record(self.repo, self.req)
        self.assertEqual(result["Iteration"], "V001")
        before = (self.root / "开发记录.md").read_bytes()
        self.assertEqual(r.initialize_record(self.repo, self.req)["Iteration"], "V001")
        self.assertEqual((self.root / "开发记录.md").read_bytes(), before)
        self.assertIn(b"IN_PROGRESS", before)
        self.assertEqual(r.records(self.repo, self.req)["Current"]["Iteration"], "V001")

    def test_planning_drafts_require_recorded_g1_and_do_not_guess_fields(self):
        self.enable(); self.register()
        with self.assertRaises(ValueError): r.prepare_planning(self.repo, self.req)
        case = self.frozen()
        (self.root / "control-profile.json").unlink()
        (self.root / "acceptance-plan.json").unlink()
        (self.root / "开发上下文包.md").unlink()
        result = r.prepare_planning(self.repo, self.req)
        self.assertEqual(len(result["CreatedDrafts"]), 3)
        self.assertFalse(result["GatesPassed"])
        self.assertIsNone(json.loads((self.root / "control-profile.json").read_text(encoding="utf-8"))["SelectedProfile"])
        self.assertFalse((self.root / "开发记录.md").exists())
        self.assertEqual(r.prepare_planning(self.repo, self.req)["CreatedDrafts"], [])

    def test_second_iteration_uses_new_g2_and_keeps_prior_git_record(self):
        self.enable(); self.register(); case = self.frozen()
        r.initialize_record(self.repo, self.req)
        before_record = (self.root / "开发记录.md").read_bytes()
        previous = self.commit()
        r.close_iteration(self.repo, self.req, "终止本轮", "TERMINATED", "保留历史", previous)
        r.begin_iteration(self.repo, self.req, "下一轮", "继续同一业务目标")
        profile = json.loads((self.root / "control-profile.json").read_text(encoding="utf-8"))
        plan = json.loads((self.root / "acceptance-plan.json").read_text(encoding="utf-8"))
        profile.update(ProfileVersion="v2.0", PreviousProfileRef="control-profile.json", PreviousProfileRevision=previous)
        plan.update(PlanVersion="v2.0", PreviousPlanRef="acceptance-plan.json", PreviousPlanRevision=previous)
        for name, data in (("control-profile.json", profile), ("acceptance-plan.json", plan)):
            (self.root / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        context = self.root / "开发上下文包.md"
        context.write_text(context.read_text(encoding="utf-8").replace("产物版本：** v1.0", "产物版本：** v2.0"), encoding="utf-8")
        gate = json.loads((self.root / "facts/gates/GATE-002.json").read_text(encoding="utf-8"))
        gate.update(EventId="GATE-003", ContextPackVersion="v2.0", CreatedAt=r.now())
        gate["Payload"]["ArtifactVersion"] = "v2.0"
        (self.root / "facts/gates/GATE-003.json").write_text(json.dumps(gate, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(r.initialize_record(self.repo, self.req)["Iteration"], "V002")
        old = r.git(self.repo, "show", previous + ":docs/requirements/" + self.req + "/开发记录.md").stdout
        self.assertEqual(old, before_record)
        self.assertIn("V002", (self.root / "开发记录.md").read_text(encoding="utf-8"))

    def test_direct_b1_entry_honors_takeover(self):
        self.enable(); self.register(); self.frozen()
        r.takeover(self.repo, self.req, "开发者自行处理")
        with self.assertRaises(ValueError): local.project(self.repo, self.req)

    def test_prior_dated_g2_cannot_be_imported_as_a_new_iteration_gate(self):
        self.enable(); self.register(); self.frozen()
        p = self.root / "facts/gates/GATE-002.json"
        event = json.loads(p.read_text(encoding="utf-8"))
        event["CreatedAt"] = "2026-01-01T00:00:00+00:00"
        p.write_text(json.dumps(event), encoding="utf-8")
        with self.assertRaises(ValueError): r.require_governed(self.repo, self.req, execution=True)
        self.assertFalse((self.root / "开发记录.md").exists())

    def test_iteration_seriality_and_immutable_history_links(self):
        self.enable(); self.register(); self.frozen()
        r.initialize_record(self.repo, self.req)
        with self.assertRaises(ValueError):
            r.begin_iteration(self.repo, self.req, "next", "new request")
        revision = self.commit()
        r.close_iteration(self.repo, self.req, "明确终止本轮", "TERMINATED", "保留本轮记录", revision)
        r.begin_iteration(self.repo, self.req, "下一次导出优化", "开始下一迭代")
        state = r.records(self.repo, self.req)
        self.assertEqual(len(state["Cycles"]), 2)
        self.assertIsNone(state["Current"]["Iteration"])
        rows = view.overview(self.repo)["Entries"]
        self.assertEqual(rows[0]["DetailCommit"], revision)
        self.assertEqual(rows[0]["Iteration"], "V001")
        self.assertEqual(rows[1]["Stage"], "NEXT_ITERATION_PLANNING")
        with self.assertRaises(ValueError):
            r.initialize_record(self.repo, self.req)  # Old G2 cannot start V002.

    def test_governed_delivery_cannot_skip_existing_archive(self):
        self.enable(); self.register()
        revision = self.commit()
        with self.assertRaises((ValueError, OSError)):
            r.close_iteration(self.repo, self.req, "我认为完成了", "DELIVERY_RECORDED", "尚未归档", revision)
        self.assertFalse(r.records(self.repo, self.req)["Current"]["Closed"])

    def test_chain_corruption_or_orphan_stops_automatic_governance(self):
        self.enable(); self.register()
        fake = self.root / "tracking/events" / ("000002-" + "0"*64 + ".json")
        fake.write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError): r.require_governed(self.repo, self.req)
        self.assertEqual(status.query_status(self.repo, self.req)["CurrentStage"], "UNDETERMINED")
        self.assertTrue(view.overview(self.repo)["Problems"])

    def test_sensitive_input_unsafe_paths_and_symbolic_sources_are_rejected(self):
        self.enable()
        with self.assertRaises(ValueError): r.register(self.repo, "unsafe", "password=synthetic-private-value", self.req)
        self.assertFalse(self.root.exists())
        with self.assertRaises(ValueError): r.path(self.repo, "../outside")
        target = self.repo / r.BATCH
        with mock.patch.object(Path, "is_symlink", lambda p: p == target), self.assertRaises(ValueError):
            r.batch(self.repo)

    def test_runtime_launcher_stops_takeover_but_allows_status_and_resume(self):
        self.enable(); self.register(); r.takeover(self.repo, self.req, "退出治理")
        runner = self.repo / ".governance/run.py"
        env = {**os.environ, "PYTHONUTF8": "1"}
        blocked = subprocess.run([sys.executable, "-B", str(runner), "governance_local_tests", "--repo", str(self.repo), "--requirement-id", self.req, "--run"], capture_output=True, env=env)
        self.assertEqual(blocked.returncode, 2)
        result = subprocess.run([sys.executable, "-B", str(runner), "governance_status", "--repo", str(self.repo), "--requirement-id", self.req], capture_output=True, text=True, encoding="utf-8", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["CurrentStage"], "MANUAL_TRACKING")

    def remote_setup(self):
        bare = self.base / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True, capture_output=True)
        self.git("remote", "add", "origin", str(bare))
        self.enable(remote="origin", web_base="https://gitlab.example.invalid/team/project/-/blob")
        self.commit()
        main = self.git("branch", "--show-current")
        self.git("push", "-q", "origin", main)
        self.git("checkout", "-qb", "feature/draft")
        self.register()
        view.refresh(self.repo, fetch=False)
        revision = self.commit()
        self.git("push", "-q", "origin", "feature/draft")
        self.git("checkout", "-q", main)
        return main, revision

    def test_uploaded_unmerged_branch_is_visible_without_checkout_or_execution(self):
        branch, revision = self.remote_setup()
        before = self.git("status", "--porcelain=v1")
        result = view.overview(self.repo)
        self.assertEqual(len(result["Entries"]), 1, result)
        row = result["Entries"][0]
        self.assertEqual(row["SourceCommit"], revision)
        self.assertEqual(row["Publication"], "UPLOADED")
        self.assertIn(revision, row["DetailUrl"])
        self.assertEqual(row["EvidenceStatus"], "UPLOADED_OBSERVATION_SOURCE_PINS_MATCH")
        self.assertEqual(branch, self.git("branch", "--show-current"))
        self.assertEqual(before, self.git("status", "--porcelain=v1"))
        self.assertFalse(self.root.exists())

    def test_incomparable_uploaded_changes_are_conflicts_not_latest_timestamp_wins(self):
        main, revision = self.remote_setup()
        self.git("checkout", "-qb", "feature/second", revision)
        r.takeover(self.repo, self.req, "此分支由开发者接管")
        view.refresh(self.repo, fetch=False)
        self.commit(); self.git("push", "-q", "origin", "feature/second")
        self.git("checkout", "-q", "feature/draft")
        (self.root / "需求基线.md").write_text("# Changed draft\n", encoding="utf-8")
        view.refresh(self.repo, fetch=False)
        self.commit(); self.git("push", "-q", "origin", "feature/draft")
        self.git("checkout", "-q", main)
        row = view.overview(self.repo)["Entries"][0]
        self.assertEqual(row["Stage"], "CROSS_BRANCH_CONFLICT")
        self.assertFalse(row["QualityPassed"])

    def test_remote_modified_sources_invalidate_uploaded_projection(self):
        main, _ = self.remote_setup()
        self.git("checkout", "-q", "feature/draft")
        (self.root / "需求基线.md").write_text("# changed without publishing observation\n", encoding="utf-8")
        self.commit(); self.git("push", "-q", "origin", "feature/draft")
        self.git("checkout", "-q", main)
        self.assertEqual(view.overview(self.repo)["Entries"][0]["Stage"], "STALE_OR_INVALID_UPLOADED_OBSERVATION")

    def test_failed_fetch_reports_cached_data_and_never_pushes(self):
        self.remote_setup()
        head = self.git("rev-parse", "HEAD")
        with mock.patch.object(view, "fetch_uploaded", return_value="FETCH_FAILED_USING_CACHED_REFS"):
            report = view.refresh(self.repo)
        self.assertEqual(report["RemoteKnowledge"], "FETCH_FAILED_USING_CACHED_REFS")
        self.assertEqual(self.git("rev-parse", "HEAD"), head)

    def test_fetch_uses_ignored_bare_cache_without_writing_project_git_refs(self):
        main, revision = self.remote_setup()
        before = self.git("for-each-ref", "--format=%(refname) %(objectname)")
        report = view.refresh(self.repo)
        self.assertEqual(report["RemoteKnowledge"], "FETCHED")
        cache, _ = view.remote_cache(self.repo, "origin")
        self.assertTrue(cache.is_relative_to(self.repo / ".governance/local"))
        self.assertEqual(self.git("for-each-ref", "--format=%(refname) %(objectname)"), before)
        self.assertEqual(report["Entries"][0]["SourceCommit"], revision)
        self.assertEqual(self.git("branch", "--show-current"), main)

    def test_registration_and_uploaded_observation_survive_autocrlf_clone(self):
        self.git("config", "core.autocrlf", "true")
        self.enable(); self.register(); view.refresh(self.repo, fetch=False)
        self.commit()
        clone = self.base / "clone"
        subprocess.run(["git", "-c", "core.autocrlf=true", "clone", "-q", str(self.repo), str(clone)], capture_output=True, check=True)
        self.assertEqual(r.records(clone, self.req)["Current"]["Ordinal"], 1)
        report = view.overview(clone)
        self.assertEqual(report["Entries"][0]["Stage"], "WAITING_FOR_G1")


if __name__ == "__main__":
    unittest.main()
