"""Synthetic v2.1 projection facts and v3.0 planning-contract checks."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("governance_tools", Path(__file__).parents[1] / "governance_tools.py")
g = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(g)


class BusinessSourceTests(unittest.TestCase):
    def test_current_template_acceptance_column_only(self):
        text = (
            "| `AcceptanceSourceId` | 业务验收原文 | `SourceKind` | 明确依据 / `SourceRef` |\n"
            "|---|---|---|---|\n"
            "| BAS-001 | 用户可以导出当前内容。 | USER_REQUIREMENT | 人工确认引用 |\n"
            "\n| 无关列 | 描述 |\n|---|---|\n| 技术细节 | 必须全量测试 |\n"
        )
        units = g.markdown_business_units(text, "4-业务结果与用户明确边界")
        self.assertIn("用户可以导出当前内容。", units)
        for excluded in ("BAS-001", "业务验收原文", "USER_REQUIREMENT", "人工确认引用", "技术细节", "必须全量测试"):
            self.assertNotIn(excluded, units)

    def test_legacy_boundary_table_stays_supported(self):
        text = "| 用户明确的业务边界 / 约束 | 来源 |\n|---|---|\n| 保留现有保存行为。 | 用户确认 |\n"
        units = g.markdown_business_units(text, "4-业务结果与用户明确边界")
        self.assertIn("保留现有保存行为。", units)
        self.assertNotIn("用户确认", units)

    def test_malformed_current_table_does_not_supply_acceptance(self):
        header = "| AcceptanceSourceId | 业务验收原文 | SourceKind | 明确依据 / SourceRef |\n|---|---|---|---|\n"
        for row in ("| BAS-001 | 不完整结果 |\n", "| NOT-BAS | 非验收行 | USER_REQUIREMENT | 引用 |\n"):
            self.assertFalse(g.markdown_business_units(header + row, "4-业务结果与用户明确边界"))


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "REQ-20260915-001"
        self.root.mkdir()
        (self.root / "开发上下文包.md").write_text("事实协议版本：v2.1\n", encoding="utf-8")
        self.put("CAND-001", "CANDIDATE_STATE", {}, "candidates")
        self.item = dict(ValidationId="V-01", GateClass="BLOCKING", GateTarget="CANDIDATE_QUALITY", AffectedWorkPackages=["WP-01"], Disposition="EXECUTED", Result="PASS", StartedAt="2026-09-15T01:00:00+08:00", CompletedAt="2026-09-15T01:00:01+08:00", DurationMs=1000, SourceQAEventRef=None, EvidenceRefs=[])
        self.qa = dict(RedactionState="SAFE", SensitiveEvidenceIssues=[], QARound=1, Status="PASS", CandidateDigestVerified=True, StartedAt="2026-09-15T01:00:00+08:00", CompletedAt="2026-09-15T01:00:01+08:00", DurationMs=1000, EffectiveValidationSet=[self.item], NotExecuted=[], BlockingItems=[], ObservedFailures=[], DispositionRef=None, CountDisposition="ACTIVE")

    def put(self, ident, kind, payload, directory="qa", **updates):
        event = dict(SchemaVersion="v1.1", RequirementId=self.root.name, ContextPackVersion="v1.0", RepairLineageRef="RL-001", EventId=ident, EventType=kind, CreatedAt="2026-09-15T01:00:02+08:00", Actor="Synthetic test", CandidateRef="CAND-001", Payload=payload, EvidenceRefs=[], SupersedesEventRef=None, RedactionState="SAFE")
        event.update(updates)
        path = self.root / "facts" / directory / (ident + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
        return path

    def test_happy_and_readonly_deterministic(self):
        self.put("QA-001", "QA_RESULT", self.qa)
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        output = g.render(self.root, "qa-index")
        self.assertIn("EXECUTED: V-01", output)
        self.assertIn("1000 ms", output)
        self.assertEqual(output, g.render(self.root, "qa-index"))
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.assertTrue(all(len(line.split("|")) == 10 for line in output.splitlines()))

    def test_v3_context_document_and_initial_null_lineage(self):
        for version in ("v3.0", "v3.0.1"):
            with self.subTest(version=version):
                (self.root / "开发上下文包.md").write_text(f"事实协议版本：{version}\n", encoding="utf-8")
                self.put("CAND-001", "CANDIDATE_STATE", {}, "candidates", RepairLineageRef=None)
                self.put("QA-001", "QA_RESULT", self.qa, RepairLineageRef=None)
                self.assertIn("PASS", g.render(self.root, "qa-index"))
                self.put("QA-001", "QA_RESULT", self.qa, RepairLineageRef="RL-002")
                with self.assertRaisesRegex(g.Invalid, "RepairLineageRef binding"):
                    g.render(self.root, "qa-index")

    def test_document_version_does_not_relax_machine_protocol(self):
        for context in ("事实协议版本：v9.0\n", "事实协议版本：v3.0\nFactProtocolVersion: v2.1\n"):
            (self.root / "开发上下文包.md").write_text(context, encoding="utf-8")
            self.put("QA-001", "QA_RESULT", self.qa)
            with self.assertRaisesRegex(g.Invalid, "FactProtocolVersion"):
                g.render(self.root, "qa-index")
        (self.root / "开发上下文包.md").write_text("事实协议版本：v3.0\n", encoding="utf-8")
        self.put("QA-001", "QA_RESULT", dict(self.qa, FactProtocolVersion="v9.0"))
        with self.assertRaisesRegex(g.Invalid, "FactProtocolVersion"):
            g.render(self.root, "qa-index")

    def test_notexecuted_advisory_and_blocked(self):
        self.put("TR-001", "TEST_READINESS", {}, "readiness")
        item = dict(ValidationId="V-02", GateClass="ADVISORY", Reason="Service unavailable", ReadinessEventRef="TR-001")
        self.qa["NotExecuted"] = [item]
        self.put("QA-001", "QA_RESULT", self.qa)
        self.assertIn("未执行（ADVISORY）: V-02", g.render(self.root, "qa-index"))
        item.update(GateClass="BLOCKING", BlockerId="B-01")
        self.qa.update(Status="BLOCKED", BlockingItems=[dict(BlockerId="B-01", AffectedValidationIds=["V-02"])])
        self.put("QA-001", "QA_RESULT", self.qa)
        output = g.render(self.root, "qa-index")
        self.assertIn("BLOCKED", output)
        self.assertIn("阻塞: V-02", output)

    def test_invalid_workpackage_reports_field_without_partial_output(self):
        for value in ([], {}, None, 1, ""):
            with self.subTest(value=value):
                self.put("WR-001", "WRITER_HANDOFF", dict(WriterRound=1, WorkPackageId=value, WriterSlot="Writer 1", SensitiveEvidenceDetected=False, Changes=[]), "writers", CandidateRef=None)
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = g.main(["render", "--requirement-dir", str(self.root), "--kind", "changes"])
                self.assertEqual(code, 1)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("facts/writers/WR-001.json", stderr.getvalue())
                self.assertIn("WorkPackageId", stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())

    def resumed_round(self):
        (self.root / "开发上下文包.md").write_text("事实协议版本：v3.0.1\n", encoding="utf-8")
        self.put("TR-001", "TEST_READINESS", {}, "readiness")
        blocked = copy.deepcopy(self.qa)
        blocked.update(Status="BLOCKED", ValidationPlanRef="plan-v1", EffectiveValidationSet=[],
                       NotExecuted=[dict(ValidationId="V-01", GateClass="BLOCKING", Reason="Unavailable", ReadinessEventRef="TR-001", BlockerId="B-01")],
                       BlockingItems=[dict(BlockerId="B-01", AffectedValidationIds=["V-01"])])
        self.put("QA-001", "QA_RESULT", blocked)
        self.put("TR-002", "TEST_READINESS", dict(ValidationPlanRef="plan-v1", Entries=[dict(ValidationId="V-01", Readiness="READY")]), "readiness",
                 CreatedAt="2026-09-15T01:00:03+08:00", EvidenceRefs=["human-recovery:service-ready"])
        resumed = copy.deepcopy(self.qa)
        resumed.update(ValidationPlanRef="plan-v1", ResumeQAEventRef="QA-001", ResumeReadinessEventRef="TR-002", RevalidationPlanRef="revalidation-v1",
                       StartedAt="2026-09-15T01:00:04+08:00", CompletedAt="2026-09-15T01:00:05+08:00")
        resumed["EffectiveValidationSet"][0].update(StartedAt=resumed["StartedAt"], CompletedAt=resumed["CompletedAt"])
        return resumed

    def test_blocked_round_resumes_without_replacing_history(self):
        resumed = self.resumed_round()
        original = (self.root / "facts/qa/QA-001.json").read_bytes()
        self.put("QA-002", "QA_RESULT", resumed, CreatedAt="2026-09-15T01:00:06+08:00")
        output = g.render(self.root, "qa-index")
        self.assertIn("QA-001 | 1", output)
        self.assertIn("QA-002 | 1", output)
        self.assertIn("BLOCKED", output)
        self.assertEqual(original, (self.root / "facts/qa/QA-001.json").read_bytes())

    def test_resume_rejects_changed_plan_round_or_missing_release(self):
        for change in (dict(ValidationPlanRef="plan-v2"), dict(QARound=2), dict(ResumeQAEventRef=None), dict(ResumeReadinessEventRef="TR-001")):
            with self.subTest(change=change):
                resumed = self.resumed_round()
                resumed.update(change)
                self.put("QA-002", "QA_RESULT", resumed, CreatedAt="2026-09-15T01:00:06+08:00")
                with self.assertRaises(g.Invalid):
                    g.render(self.root, "qa-index")

    def test_resume_rejects_no_actual_blocker_release(self):
        resumed = self.resumed_round()
        self.put("TR-002", "TEST_READINESS", dict(ValidationPlanRef="plan-v1", Entries=[dict(ValidationId="V-01", Readiness="NOT_READY")]), "readiness",
                 CreatedAt="2026-09-15T01:00:03+08:00", EvidenceRefs=["human-message:continue"])
        self.put("QA-002", "QA_RESULT", resumed, CreatedAt="2026-09-15T01:00:06+08:00")
        with self.assertRaisesRegex(g.Invalid, "resume released blocker"):
            g.render(self.root, "qa-index")

    def test_missing_target_event_rejected(self):
        for kind in ("qa-index", "review-index", "changes"):
            with self.subTest(kind=kind), self.assertRaises(g.Invalid):
                g.render(self.root, kind)

    def test_reuse_and_crosscandidate_rejected(self):
        self.put("QA-001", "QA_RESULT", self.qa)
        second = copy.deepcopy(self.qa)
        second["QARound"] = 2
        second["EffectiveValidationSet"][0].update(Disposition="REUSED", SourceQAEventRef="QA-001", StartedAt=None, CompletedAt=None, DurationMs=None)
        self.put("QA-002", "QA_RESULT", second)
        self.assertIn("REUSED: V-01", g.render(self.root, "qa-index"))
        self.put("CAND-002", "CANDIDATE_STATE", {}, "candidates", CandidateRef="CAND-002")
        self.put("QA-002", "QA_RESULT", second, CandidateRef="CAND-002")
        with self.assertRaises(g.Invalid):
            g.render(self.root, "qa-index")

    def test_review_and_changes(self):
        review = {k: self.qa[k] for k in ("StartedAt", "CompletedAt", "DurationMs", "CandidateDigestVerified", "DispositionRef", "CountDisposition")}
        review.update(ReviewerRound=1, ReviewScope="STATIC_CANDIDATE", Decision="APPROVE", Findings=[], EvidenceIssues=[])
        self.put("RV-001", "REVIEW_RESULT", review, "reviews")
        self.assertIn("APPROVE", g.render(self.root, "review-index"))
        change = dict(ActionId="A-01", Operation="MODIFY", Path="src/example.py", CodeUnit="function", Summary="a | b\n<script>", Reason="AC-01", EvidenceRefs=["evidence/test.txt"])
        self.put("WR-001", "WRITER_HANDOFF", dict(WriterRound=1, WorkPackageId="WP-01", WriterSlot="Writer 1", SensitiveEvidenceDetected=False, Changes=[change]), "writers", CandidateRef=None)
        output = g.render(self.root, "changes")
        self.assertIn("&#124;", output)
        self.assertIn("&lt;script&gt;", output)
        self.assertIn("evidence/test.txt", output)

        for kind, expected in (("review-index", "APPROVE"), ("changes", "evidence/test.txt")):
            with self.subTest(kind=kind):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = g.main(["render", "--requirement-dir", str(self.root), "--kind", kind])
                self.assertEqual(code, 0)
                self.assertIn(expected, stdout.getvalue())

    def test_invalid_time_version_correction_and_sensitivity(self):
        for field, value in (("SchemaVersion", "v2.1"), ("RedactionState", "REDACTION_REQUIRED"), ("SupersedesEventRef", "QA-000"), ("EventType", "FACT_CORRECTION")):
            with self.subTest(field=field):
                self.put("QA-001", "QA_RESULT", self.qa, **{field: value})
                with self.assertRaises(g.Invalid):
                    g.render(self.root, "qa-index")
        self.qa["DurationMs"] = 8000
        self.put("QA-001", "QA_RESULT", self.qa)
        with self.assertRaises(g.Invalid):
            g.render(self.root, "qa-index")
        self.qa["DurationMs"] = 1000
        self.qa["note"] = "password=synthetic-secret"
        self.put("QA-001", "QA_RESULT", self.qa)
        stderr, stdout = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            code = g.main(["render", "--requirement-dir", str(self.root), "--kind", "qa-index"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("synthetic-secret", stderr.getvalue())

    def test_old_context_and_missing_consumed_field(self):
        self.put("QA-001", "QA_RESULT", self.qa)
        (self.root / "开发上下文包.md").write_text("事实协议版本：v2.0\n", encoding="utf-8")
        with self.assertRaises(g.Invalid):
            g.render(self.root, "qa-index")
        (self.root / "开发上下文包.md").write_text("事实协议版本：v2.1\n", encoding="utf-8")
        del self.qa["QARound"]
        self.put("QA-001", "QA_RESULT", self.qa)
        with self.assertRaises(g.Invalid):
            g.render(self.root, "qa-index")

    def test_path_and_cli(self):
        with self.assertRaises(g.Invalid):
            g.safe_path(self.root, self.root / ".." / "outside")
        self.put("QA-001", "QA_RESULT", self.qa)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(g.main(["render", "--requirement-dir", str(self.root), "--kind", "qa-index"]), 0)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            g.main(["render"])
        self.assertEqual(caught.exception.code, 2)

    def test_normal_route_and_foreign_reference(self):
        self.put("QA-001", "QA_RESULT", self.qa)
        self.put("RT-001", "ROUTING_DECISION", {"TriggerEventRefs": ["QA-001"]}, "routes")
        self.assertIn("facts/routes/RT-001.json", g.render(self.root, "qa-index"))
        events = g.load_events(self.root)
        with self.assertRaises(g.Invalid):
            g.resolve(events, "docs/requirements/REQ-20260914-099/facts/candidates/CAND-001.json", "CANDIDATE_STATE", events["QA-001"])

    def test_fail_change_and_late_disposition(self):
        self.item["Result"] = "FAIL"
        self.qa.update(Status="FAIL", ObservedFailures=[{"AffectedWorkPackage": "WP-01"}])
        self.put("RT-001", "ROUTING_DECISION", {"TriggerEventRefs": []}, "routes")
        self.qa.update(DispositionRef="RT-001", CountDisposition="IGNORED_DUE_TO_PRIOR_DISPOSITION")
        self.put("QA-001", "QA_RESULT", self.qa)
        qa_output = g.render(self.root, "qa-index")
        self.assertIn("FAIL", qa_output)
        self.assertIn("RT-001", qa_output)

        review = {k: self.qa[k] for k in ("StartedAt", "CompletedAt", "DurationMs", "CandidateDigestVerified")}
        review.update(ReviewerRound=1, ReviewScope="STATIC_CANDIDATE", Decision="CHANGE", Findings=[{"FindingId": "F-01"}], EvidenceIssues=[], DispositionRef=None, CountDisposition="ACTIVE")
        self.put("RV-001", "REVIEW_RESULT", review, "reviews")
        self.assertIn("CHANGE", g.render(self.root, "review-index"))

        self.qa.update(DispositionRef=None, CountDisposition="IGNORED_DUE_TO_PRIOR_DISPOSITION")
        self.put("QA-001", "QA_RESULT", self.qa)
        with self.assertRaises(g.Invalid):
            g.render(self.root, "qa-index")

    def test_advisory_failure_cannot_pass_and_g1_null_context(self):
        self.put("GATE-001", "GATE_DECISION", {}, "gates", ContextPackVersion=None, RepairLineageRef=None, CandidateRef=None)
        self.put("QA-001", "QA_RESULT", self.qa)
        self.assertIn("PASS", g.render(self.root, "qa-index"))
        self.item.update(GateClass="ADVISORY", Result="FAIL")
        self.put("QA-001", "QA_RESULT", self.qa)
        with self.assertRaises(g.Invalid):
            g.render(self.root, "qa-index")


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        (self.repo / "AGENTS.md").write_text("# Entry\n", encoding="utf-8")
        for folder, names in {
            "docs/Agent治理": ("Codex运行时拓扑", "多智能体协同开发治理流程", "治理配置与验收控制规范", "治理运行时事实协议", "开发溯源归档规范", "需求基线模板", "开发上下文包模板", "开发记录模板", "临时质量验证记录模板", "只读评审记录模板"),
            "docs/开发规范": ("项目开发总则", "测试规范"),
        }.items():
            directory = self.repo / folder
            directory.mkdir(parents=True)
            for name in names:
                (directory / (name + ".md")).write_text("# " + name + "\n", encoding="utf-8")
        roles = self.repo / ".codex/agents"
        roles.mkdir(parents=True)
        for name in ("writer", "qa", "reviewer", "orchestrator", "development-trace"):
            (roles / (name + ".toml")).write_text(f'name = "{name}"\n', encoding="utf-8")
        contracts = self.repo / "docs/Agent治理/contracts"
        contracts.mkdir()
        for name in ("control-profile.schema.json", "acceptance-plan.schema.json", "replay-expectations.schema.json"):
            (contracts / name).write_text('{"$schema":"https://json-schema.org/draft/2020-12/schema"}', encoding="utf-8")

    def test_inventory_and_additional_document(self):
        self.assertIn("13 Markdown", g.check(self.repo))
        extra = self.repo / "docs/Agent治理/附录.md"
        extra.write_text("# 附录\n", encoding="utf-8")
        self.assertIn("14 Markdown", g.check(self.repo))
        (self.repo / "docs/Agent治理/需求基线模板.md").unlink()
        with self.assertRaises(g.Invalid):
            g.check(self.repo)

    def test_schema_inventory_and_syntax(self):
        schema = self.repo / "docs/Agent治理/contracts/control-profile.schema.json"
        schema.unlink()
        with self.assertRaises(g.Invalid):
            g.check(self.repo)
        schema.write_text("{", encoding="utf-8")
        with self.assertRaises(g.Invalid):
            g.check(self.repo)

    def test_links_anchors_tables_and_rules(self):
        entry = self.repo / "AGENTS.md"
        entry.write_text("# Entry\n\n[section](#entry)\n\n| RuleId | Text |\n|---|---|\n| `GEN-001` | rule |\n\nUse `GEN-001`.\n", encoding="utf-8")
        self.assertIn("mechanical checks only", g.check(self.repo))
        for invalid in ("[missing](missing.md)", "[missing](#absent)", "`GEN-999`", "| a | b |\n|---|---|\n| only one |"):
            with self.subTest(invalid=invalid):
                entry.write_text("# Entry\n\n" + invalid, encoding="utf-8")
                with self.assertRaises(g.Invalid):
                    g.check(self.repo)

    def test_toml_error_and_io_exit(self):
        role = self.repo / ".codex/agents/writer.toml"
        role.write_text('name = "unterminated', encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(g.main(["check", "--repo", str(self.repo)]), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("writer.toml", stderr.getvalue())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(g.main(["render", "--requirement-dir", str(self.repo / "REQ-20260915-099"), "--kind", "changes"]), 1)


class V3PlanValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.repo = root
        (root / "AGENTS.md").write_text("# test repository\n", encoding="utf-8")
        requirement_dir = root / "docs/requirements/REQ-20260915-099"
        self.requirement_dir = requirement_dir
        (requirement_dir / "facts/gates").mkdir(parents=True)
        (requirement_dir / "需求基线.md").write_text(
            "# 基线\n\n- **产物版本：** v1.0\n\n## 1. 原始需求\n\n"
            "> 用户可以导出当前内容。\n\n补充约束：\n\n> 系统保留现有保存行为。\n\n"
            "> 1. 用户可以下载完整文件。\n\n## 4. 业务结果与用户明确边界\n\n"
            "- **唯一业务结果：** 导出内容必须格式化，并使用 UTF-8 编码。\n\n"
            "| 用户明确的业务边界 / 约束 | 明确依据 |\n|---|---|\n"
            "| 导出结果可供用户读取。 | 用户确认 |\n",
            encoding="utf-8",
        )
        (requirement_dir / "开发上下文包.md").write_text("# 开发上下文包\n", encoding="utf-8")
        (requirement_dir / "开发记录.md").write_text("# 开发记录\n", encoding="utf-8")
        standards = root / "docs/开发规范"
        standards.mkdir(parents=True)
        (standards / "项目开发总则.md").write_text("# Rules\n\n| RuleId | Rule |\n|---|---|\n| `GEN-012` | Test |\n\n正文仅提及不存在的 `GEN-999`。\n", encoding="utf-8")
        contracts = root / "docs/Agent治理/contracts"
        contracts.mkdir(parents=True)
        source_contracts = Path(__file__).resolve().parents[2] / "docs/Agent治理/contracts"
        for name in ("control-profile.schema.json", "acceptance-plan.schema.json", "replay-expectations.schema.json"):
            (contracts / name).write_bytes((source_contracts / name).read_bytes())
        self.gate = {
            "SchemaVersion": "v1.1", "RequirementId": "REQ-20260915-099",
            "ContextPackVersion": None, "RepairLineageRef": None,
            "EventId": "GATE-001", "EventType": "GATE_DECISION",
            "CreatedAt": "2026-09-15T01:00:00+08:00", "Actor": "User / Governance Runtime",
            "CandidateRef": None,
            "Payload": {"Gate": "G1", "Decision": "CONFIRMED", "DecisionMode": "HUMAN",
                        "ArtifactRef": "docs/requirements/REQ-20260915-099/需求基线.md", "ArtifactVersion": "v1.0",
                        "BasisRefs": ["current-user-message:2026-09-15T01:00:00+08:00"],
                        "ApprovalRef": "current-user-message:2026-09-15T01:00:00+08:00",
                        "BlockingItems": []},
            "EvidenceRefs": ["current user confirmation"], "SupersedesEventRef": None,
            "RedactionState": "SAFE",
        }
        (requirement_dir / "facts/gates/GATE-001.json").write_text(
            json.dumps(self.gate, ensure_ascii=False), encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Governance Test"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "governance@test.invalid"], cwd=root, check=True)
        subprocess.run(["git", "add", "AGENTS.md", "docs"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "approved baseline fixture"], cwd=root, check=True)
        self.provenance_revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.profile_path = requirement_dir / "control-profile.json"
        self.plan_path = requirement_dir / "acceptance-plan.json"
        self.profile = {
            "SchemaVersion": "v3.0", "GovernanceVersion": "v3.0",
            "DecisionRulesVersion": "v3.0", "RequirementId": "REQ-20260915-099",
            "ProfileVersion": "v1.0", "SelectedProfile": "LEAN", "MinimumProfile": "LEAN",
            "PreviousProfileRef": None,
            "PreviousProfileRevision": None,
            "Classification": {
                "HardTriggers": [
                    {"Code": code, "Matched": False, "EvidenceRefs": ["docs/requirements/REQ-20260915-099/需求基线.md#基线"]}
                    for code in sorted(g.HARD_TRIGGERS)
                ],
                "UpgradeTriggers": [
                    {"Code": code, "Matched": False, "EvidenceRefs": ["docs/requirements/REQ-20260915-099/开发上下文包.md#开发上下文包"]}
                    for code in sorted(g.UPGRADE_TRIGGERS)
                ],
                "LeanQualifications": [
                    {"Code": code, "Satisfied": True, "EvidenceRefs": ["docs/requirements/REQ-20260915-099/开发上下文包.md#开发上下文包"]}
                    for code in sorted(g.LEAN_QUALIFICATIONS)
                ],
                "Unknowns": [], "DecisionRefs": ["docs/requirements/REQ-20260915-099/需求基线.md#基线"],
            },
            "BusinessAcceptanceSources": [{
                "AcceptanceSourceId": "BAS-001", "SourceKind": "USER_REQUIREMENT",
                "SourceRef": "docs/requirements/REQ-20260915-099/需求基线.md#1-原始需求",
                "AcceptanceText": "用户可以导出当前内容。",
                "ApprovalRef": "docs/requirements/REQ-20260915-099/facts/gates/GATE-001.json",
                "SourceArtifactVersion": "v1.0",
                "SourceDigest": g.content_digest((requirement_dir / "需求基线.md").read_text(encoding="utf-8")),
                "ApprovalDigest": g.content_digest((requirement_dir / "facts/gates/GATE-001.json").read_text(encoding="utf-8")),
                "ProvenanceRevision": None,
            }],
            "ExecutionBudget": {"PlanningMinutes": 5, "ImplementationMinutes": 5, "ImplementationAndControlsMinutes": 20, "BlockingControlCount": 2, "AtomicRiskTargetCount": 2, "BlockingControlsMinutes": 5, "AdvisoryMinutes": 10, "WorkPackageCount": 1},
            "PlatformScope": {"LocalDevelopment": ["WINDOWS", "MACOS"], "DeliveryValidation": "LINUX_PRODUCTION", "DeliveryValidationStatus": "NOT_AVAILABLE", "CodexDesktopMultiAgent": True},
            "RecordRefs": ["docs/requirements/REQ-20260915-099/需求基线.md", "control-profile.json", "acceptance-plan.json", "docs/requirements/REQ-20260915-099/开发上下文包.md", "docs/requirements/REQ-20260915-099/开发记录.md"],
        }
        self.plan = {
            "SchemaVersion": "v3.0", "GovernanceVersion": "v3.0", "RequirementId": "REQ-20260915-099",
            "PlanVersion": "v1.0", "ControlProfileRef": "control-profile.json",
            "PreviousPlanRef": None, "PreviousPlanRevision": None,
            "Controls": [
                {"ControlId": "BA-001", "Class": "BA", "Statement": "用户可以导出当前内容。", "SourceRefs": [], "AcceptanceSourceRef": "BAS-001", "Triggered": True, "Blocking": True, "Owner": "HUMAN_ACCEPTANCE", "MethodKind": "NONE", "Method": None, "ExecutionPlatform": "NONE", "EstimatedMinutes": 0, "RiskTargetId": None},
                {"ControlId": "RB-001", "Class": "RB", "Statement": "保持既有保存行为。", "SourceRefs": ["docs/requirements/REQ-20260915-099/开发上下文包.md#开发上下文包"], "AcceptanceSourceRef": None, "Triggered": True, "Blocking": True, "Owner": "WRITER", "MethodKind": "DETERMINISTIC_ASSERTION", "Method": "定向回归断言", "ExecutionPlatform": "WINDOWS", "EstimatedMinutes": 2, "RiskTargetId": "RISK-SAVE-BEHAVIOR"},
                {"ControlId": "QG-001", "Class": "QG", "Statement": "定向测试通过。", "SourceRefs": ["RULE:GEN-012"], "AcceptanceSourceRef": None, "Triggered": True, "Blocking": True, "Owner": "WRITER", "MethodKind": "COMMAND", "Method": "python -m unittest", "ExecutionPlatform": "WINDOWS", "EstimatedMinutes": 3, "RiskTargetId": "RISK-TARGETED-TEST"},
                {"ControlId": "AD-001", "Class": "AD", "Statement": "建议扩展浏览器检查。", "SourceRefs": ["RULE:GEN-012"], "AcceptanceSourceRef": None, "Triggered": True, "Blocking": False, "Owner": "WRITER", "MethodKind": "ADVISORY", "Method": "具名浏览器会话", "ExecutionPlatform": "WINDOWS", "EstimatedMinutes": 10, "RiskTargetId": "RISK-BROWSER-CHECK"},
            ],
        }
        self.write()

    def write(self):
        self.profile_path.write_text(json.dumps(self.profile, ensure_ascii=False), encoding="utf-8")
        self.plan_path.write_text(json.dumps(self.plan, ensure_ascii=False), encoding="utf-8")

    def amend_gate(self, gate=None, relative="facts/gates/GATE-001.json"):
        old_path = self.requirement_dir / "facts/gates/GATE-001.json"
        target = self.requirement_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target != old_path and old_path.exists():
            old_path.unlink()
        target.write_text(json.dumps(self.gate if gate is None else gate, ensure_ascii=False), encoding="utf-8")
        subprocess.run(["git", "add", "-A", "docs/requirements/REQ-20260915-099"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "--amend", "--no-edit", "-q"], cwd=self.repo, check=True)
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, check=True, capture_output=True, text=True,
        ).stdout.strip()
        source = self.profile["BusinessAcceptanceSources"][0]
        source["ProvenanceRevision"] = revision
        source["ApprovalRef"] = f"docs/requirements/REQ-20260915-099/{relative}"
        source["SourceDigest"] = g.content_digest((self.requirement_dir / "需求基线.md").read_text(encoding="utf-8"))
        source["ApprovalDigest"] = g.content_digest(target.read_text(encoding="utf-8"))
        self.write()

    def add_independent_record_refs(self, profile=None):
        profile = self.profile if profile is None else profile
        for name in ("临时质量验证记录.md", "只读评审记录.md"):
            ref = f"docs/requirements/REQ-20260915-099/{name}"
            if ref not in profile["RecordRefs"]:
                profile["RecordRefs"].append(ref)

    def test_valid_lean_and_readonly_cli(self):
        before = (self.profile_path.read_bytes(), self.plan_path.read_bytes())
        result = g.validate_plan(self.profile_path, self.plan_path)
        self.assertEqual((result["Mode"], result["G2Eligible"]), ("LIVE", True))
        self.assertEqual(result["RequiredG2DecisionMode"], "HUMAN")
        self.assertEqual(g.validate_g2(self.profile_path, self.plan_path)["ResultType"], "G2_ELIGIBILITY")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(g.main(["validate-plan", "--control-profile", str(self.profile_path), "--acceptance-plan", str(self.plan_path)]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["G2Eligible"], True)
        self.assertEqual(before, (self.profile_path.read_bytes(), self.plan_path.read_bytes()))

    def test_g2_decision_mode_is_derived_from_triggered_assertions(self):
        self.assertEqual(
            g.validate_plan(self.profile_path, self.plan_path)["RequiredG2DecisionMode"],
            "HUMAN",
        )
        self.plan["Controls"][1].update(MethodKind="COMMAND", Method="python -m unittest targeted_regression")
        self.write()
        self.assertEqual(
            g.validate_plan(self.profile_path, self.plan_path)["RequiredG2DecisionMode"],
            "AUTOMATIC_ALLOWED",
        )

    def test_deterministic_profile_priority_and_floor(self):
        hard = next(item for item in self.profile["Classification"]["HardTriggers"] if item["Code"] == "PRODUCTION_RELEASE")
        hard["Matched"] = True
        self.write()
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)
        self.profile["SelectedProfile"] = "CONTROLLED"
        self.add_independent_record_refs()
        self.plan["Controls"][1].update(Owner="REVIEWER", MethodKind="STATIC_REVIEW")
        self.plan["Controls"][2]["Owner"] = "QA"
        self.write()
        g.validate_plan(self.profile_path, self.plan_path)

        hard["Matched"] = False
        self.profile.update(SelectedProfile="STANDARD", MinimumProfile="STANDARD")
        self.write()
        g.validate_plan(self.profile_path, self.plan_path)

    def test_unknown_and_incomplete_classification_rejected(self):
        self.profile["Classification"]["Unknowns"] = [{"Code": "scope", "CouldBeControlled": True, "EvidenceRefs": ["docs/requirements/REQ-20260915-099/需求基线.md#基线"]}]
        self.write()
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)
        self.profile["SelectedProfile"] = "CONTROLLED"
        self.add_independent_record_refs()
        self.plan["Controls"][1].update(Owner="REVIEWER", MethodKind="STATIC_REVIEW")
        self.plan["Controls"][2]["Owner"] = "QA"
        self.write()
        g.validate_plan(self.profile_path, self.plan_path)
        self.profile["Classification"]["HardTriggers"].pop()
        self.write()
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)

    def test_ba_expansion_and_technical_fields_rejected(self):
        ba = self.plan["Controls"][0]
        for field, value in (("Statement", "用户可以导出当前内容，并必须使用 Blob。"), ("SourceRefs", ["RULE:UI-001"]), ("Method", "vitest")):
            with self.subTest(field=field):
                original = ba[field]
                ba[field] = value
                self.write()
                with self.assertRaises(g.Invalid):
                    g.validate_plan(self.profile_path, self.plan_path)
                ba[field] = original

    def test_ba_requires_complete_markdown_business_unit(self):
        source = self.profile["BusinessAcceptanceSources"][0]
        ba = self.plan["Controls"][0]
        valid = (
            ("docs/requirements/REQ-20260915-099/需求基线.md#1-原始需求", "用户可以导出当前内容。"),
            ("docs/requirements/REQ-20260915-099/需求基线.md#1-原始需求", "系统保留现有保存行为。"),
            ("docs/requirements/REQ-20260915-099/需求基线.md#1-原始需求", "用户可以下载完整文件。"),
            ("docs/requirements/REQ-20260915-099/需求基线.md#4-业务结果与用户明确边界", "导出结果可供用户读取。"),
            ("docs/requirements/REQ-20260915-099/需求基线.md#4-业务结果与用户明确边界", "导出内容必须格式化"),
            ("docs/requirements/REQ-20260915-099/需求基线.md#4-业务结果与用户明确边界", "并使用 UTF-8 编码。"),
        )
        for source_ref, text in valid:
            with self.subTest(valid=text):
                source["SourceRef"] = source_ref
                source["AcceptanceText"] = ba["Statement"] = text
                self.write()
                g.validate_plan(self.profile_path, self.plan_path)
        source["SourceRef"] = "docs/requirements/REQ-20260915-099/需求基线.md#1-原始需求"
        for text in ("用户", "可以", "导出"):
            with self.subTest(invalid=text):
                source["AcceptanceText"] = ba["Statement"] = text
                self.write()
                with self.assertRaises(g.Invalid):
                    g.validate_plan(self.profile_path, self.plan_path)

    def test_ba_anchor_semantics_reject_labels_headers_and_evidence_cells(self):
        source = self.profile["BusinessAcceptanceSources"][0]
        ba = self.plan["Controls"][0]
        invalid = (
            ("docs/requirements/REQ-20260915-099/需求基线.md#1-原始需求", "补充约束："),
            ("docs/requirements/REQ-20260915-099/需求基线.md#4-业务结果与用户明确边界", "用户明确的业务边界 / 约束"),
            ("docs/requirements/REQ-20260915-099/需求基线.md#4-业务结果与用户明确边界", "用户确认"),
        )
        for source_ref, text in invalid:
            with self.subTest(text=text):
                source.update(SourceRef=source_ref, AcceptanceText=text)
                ba["Statement"] = text
                self.write()
                with self.assertRaises(g.Invalid):
                    g.validate_plan(self.profile_path, self.plan_path)

    def test_current_bas_template_through_complete_plan_validation(self):
        path = self.requirement_dir / "需求基线.md"
        text = path.read_text(encoding="utf-8")
        text = text.replace("| 用户明确的业务边界 / 约束 | 明确依据 |\n|---|---|\n| 导出结果可供用户读取。 | 用户确认 |",
                            "| `AcceptanceSourceId` | 业务验收原文 | `SourceKind` | 明确依据 / `SourceRef` |\n|---|---|---|---|\n| BAS-001 | 导出结果可供用户读取。 | USER_REQUIREMENT | 用户确认 |")
        path.write_text(text, encoding="utf-8")
        source = self.profile["BusinessAcceptanceSources"][0]
        source.update(SourceRef="docs/requirements/REQ-20260915-099/需求基线.md#4-业务结果与用户明确边界", AcceptanceText="导出结果可供用户读取。")
        self.plan["Controls"][0]["Statement"] = source["AcceptanceText"]
        source["SourceDigest"] = g.content_digest(text)
        self.write()
        self.assertTrue(g.validate_plan(self.profile_path, self.plan_path)["Valid"])
        source["AcceptanceText"] = self.plan["Controls"][0]["Statement"] = "USER_REQUIREMENT"
        self.write()
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)

    def test_developer_id_through_complete_plan_validation(self):
        old = "REQ-20260915-099"
        new = "REQ-DEV-20260917-01"
        root = self.requirement_dir.with_name(new)
        self.requirement_dir.rename(root)
        for path in root.rglob("*"):
            if path.is_file():
                path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
        self.requirement_dir = root
        self.profile_path, self.plan_path = root / "control-profile.json", root / "acceptance-plan.json"
        self.profile = json.loads(json.dumps(self.profile).replace(old, new))
        self.plan = json.loads(json.dumps(self.plan).replace(old, new))
        source = self.profile["BusinessAcceptanceSources"][0]
        source["SourceDigest"] = g.content_digest((root / "需求基线.md").read_text(encoding="utf-8"))
        source["ApprovalDigest"] = g.content_digest((root / "facts/gates/GATE-001.json").read_text(encoding="utf-8"))
        self.write()
        self.assertEqual(g.validate_plan(self.profile_path, self.plan_path)["RequirementId"], new)

    def test_g1_approval_requires_complete_fact_event(self):
        for missing in ("SchemaVersion", "CreatedAt", "Actor", "EvidenceRefs"):
            with self.subTest(missing=missing):
                gate = copy.deepcopy(self.gate)
                gate.pop(missing)
                self.amend_gate(gate)
                with self.assertRaises(g.Invalid):
                    g.validate_plan(self.profile_path, self.plan_path)

    def test_g1_event_id_filename_and_location_are_bound(self):
        gate = copy.deepcopy(self.gate)
        gate["EventId"] = "GATE-999"
        self.amend_gate(gate)
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)

        self.gate["EventId"] = "GATE-OUTSIDE"
        self.amend_gate(self.gate, "facts/GATE-OUTSIDE.json")
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)

    def test_g1_human_approval_reference_must_be_locatable(self):
        gate = copy.deepcopy(self.gate)
        gate["Payload"]["ApprovalRef"] = "user:test"
        self.amend_gate(gate)
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)

    def test_g1_payload_is_exact_and_confirmed_has_no_blockers(self):
        for mutation in ("extra", "missing_basis", "blocking"):
            with self.subTest(mutation=mutation):
                gate = copy.deepcopy(self.gate)
                if mutation == "extra":
                    gate["Payload"]["Unspecified"] = True
                elif mutation == "missing_basis":
                    gate["Payload"]["BasisRefs"] = []
                else:
                    gate["Payload"]["BlockingItems"] = ["unresolved"]
                self.amend_gate(gate)
                with self.assertRaises(g.Invalid):
                    g.validate_plan(self.profile_path, self.plan_path)

    def test_ba_source_is_bound_to_approved_git_revision_and_version(self):
        baseline = self.requirement_dir / "需求基线.md"
        baseline.write_text(baseline.read_text(encoding="utf-8") + "\n必须使用 Blob 和 Vitest。\n", encoding="utf-8")
        source = self.profile["BusinessAcceptanceSources"][0]
        source["AcceptanceText"] = "必须使用 Blob 和 Vitest。"
        self.plan["Controls"][0]["Statement"] = source["AcceptanceText"]
        self.write()
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)
        source["AcceptanceText"] = "用户可以导出当前内容。"
        self.plan["Controls"][0]["Statement"] = source["AcceptanceText"]
        source["SourceArtifactVersion"] = "v2.0"
        self.write()
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)

    def test_ba_cannot_use_non_business_baseline_section(self):
        source = self.profile["BusinessAcceptanceSources"][0]
        source.update(SourceRef="docs/requirements/REQ-20260915-099/需求基线.md", AcceptanceText="产物版本")
        self.plan["Controls"][0]["Statement"] = "产物版本"
        self.write()
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)

    def test_post_g1_committed_baseline_change_cannot_reuse_approval(self):
        baseline = self.requirement_dir / "需求基线.md"
        baseline.write_text(baseline.read_text(encoding="utf-8") + "\n必须使用 Blob 和 Vitest。\n", encoding="utf-8")
        subprocess.run(["git", "add", "docs/requirements/REQ-20260915-099/需求基线.md"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "post-G1 baseline mutation fixture"], cwd=self.repo, check=True)
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, check=True, capture_output=True, text=True,
        ).stdout.strip()
        source = self.profile["BusinessAcceptanceSources"][0]
        source.update(AcceptanceText="必须使用 Blob 和 Vitest。", ProvenanceRevision=revision)
        self.plan["Controls"][0]["Statement"] = source["AcceptanceText"]
        self.write()
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)

    def test_evidence_references_are_resolved(self):
        item = self.profile["Classification"]["HardTriggers"][0]
        for value in ("missing.md#x", "docs/requirements/REQ-20260915-099/需求基线.md#missing"):
            with self.subTest(value=value):
                item["EvidenceRefs"] = [value]
                self.write()
                with self.assertRaises(g.Invalid):
                    g.validate_plan(self.profile_path, self.plan_path)

    def test_formal_business_contract_requires_human_approval_structure(self):
        root = self.repo
        contract_dir = root / "docs/requirements/REQ-20260915-099/contracts"
        contract_dir.mkdir(parents=True)
        contract = contract_dir / "business.md"
        contract.write_text("# Contract\n\n- ArtifactVersion: v1.0\n\n允许用户下载业务数据。\n", encoding="utf-8")
        approval = contract_dir / "approval.json"
        approval.write_text(json.dumps({
            "RequirementId": "REQ-20260915-099", "ApprovalType": "BUSINESS_CONTRACT_APPROVAL",
            "ApprovalStatus": "APPROVED", "DecisionMode": "HUMAN", "ApprovedBy": "Product Owner",
            "ApprovedAt": "2026-09-15T01:00:00+08:00", "ApprovalRef": "decision:test",
            "ContractRef": "docs/requirements/REQ-20260915-099/contracts/business.md",
            "ContractVersion": "v1.0",
        }, ensure_ascii=False), encoding="utf-8")
        subprocess.run(["git", "add", "docs/requirements/REQ-20260915-099/contracts"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "approved business contract fixture"], cwd=root, check=True)
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True,
        ).stdout.strip()
        source = self.profile["BusinessAcceptanceSources"][0]
        source.update(SourceKind="FORMAL_BUSINESS_CONTRACT",
                      SourceRef="docs/requirements/REQ-20260915-099/contracts/business.md#contract",
                      AcceptanceText="允许用户下载业务数据。",
                      ApprovalRef="docs/requirements/REQ-20260915-099/contracts/approval.json",
                      SourceArtifactVersion="v1.0",
                      SourceDigest=g.content_digest(contract.read_text(encoding="utf-8")),
                      ApprovalDigest=g.content_digest(approval.read_text(encoding="utf-8")),
                      ProvenanceRevision=revision)
        self.plan["Controls"][0]["Statement"] = source["AcceptanceText"]
        self.write()
        g.validate_plan(self.profile_path, self.plan_path)
        approval_payload = json.loads(approval.read_text(encoding="utf-8"))
        approval_payload.pop("ApprovedBy")
        approval.write_text(json.dumps(approval_payload, ensure_ascii=False), encoding="utf-8")
        subprocess.run(["git", "add", "docs/requirements/REQ-20260915-099/contracts"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "invalid approval fixture"], cwd=root, check=True)
        source["ProvenanceRevision"] = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True,
        ).stdout.strip()
        source["ApprovalDigest"] = g.content_digest(approval.read_text(encoding="utf-8"))
        self.write()
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)

    def test_platform_role_and_version_bindings(self):
        base_profile, base_plan = copy.deepcopy(self.profile), copy.deepcopy(self.plan)
        mutations = ("local", "ci", "reviewer_method", "lean_ad_owner", "plan_version", "orphan_version")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.profile, self.plan = copy.deepcopy(base_profile), copy.deepcopy(base_plan)
                if mutation == "local":
                    self.profile["PlatformScope"]["LocalDevelopment"] = ["MACOS"]
                elif mutation == "ci":
                    self.plan["Controls"][2].update(Owner="CI", ExecutionPlatform="WINDOWS")
                elif mutation == "reviewer_method":
                    self.profile.update(SelectedProfile="STANDARD", MinimumProfile="STANDARD")
                    self.add_independent_record_refs()
                    self.plan["Controls"][1].update(Owner="REVIEWER", MethodKind="COMMAND")
                    self.plan["Controls"][2]["Owner"] = "QA"
                elif mutation == "lean_ad_owner":
                    self.plan["Controls"][3]["Owner"] = "QA"
                elif mutation == "plan_version":
                    self.plan["PlanVersion"] = "v2.0"
                else:
                    self.profile["ProfileVersion"] = "v2.0"
                    self.plan["PlanVersion"] = "v2.0"
                self.write()
                with self.assertRaises(g.Invalid):
                    g.validate_plan(self.profile_path, self.plan_path)

    def test_control_source_protocols_must_resolve(self):
        rb = self.plan["Controls"][1]
        for value in ("RULE:GEN-999", "PROFILE:made-up", "PROFILE:CONTROLLED", "RISK:not-real", "USER:anything"):
            with self.subTest(value=value):
                rb["SourceRefs"] = [value]
                self.write()
                with self.assertRaises(g.Invalid):
                    g.validate_plan(self.profile_path, self.plan_path)

    def test_ba_source_once_and_class_invariants(self):
        self.plan["Controls"].append(copy.deepcopy(self.plan["Controls"][0]))
        self.plan["Controls"][-1]["ControlId"] = "BA-002"
        self.write()
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)
        self.plan["Controls"].pop()
        duplicate_source = copy.deepcopy(self.profile["BusinessAcceptanceSources"][0])
        duplicate_source["AcceptanceSourceId"] = "BAS-002"
        self.profile["BusinessAcceptanceSources"].append(duplicate_source)
        duplicate_ba = copy.deepcopy(self.plan["Controls"][0])
        duplicate_ba.update(ControlId="BA-002", AcceptanceSourceRef="BAS-002")
        self.plan["Controls"].append(duplicate_ba)
        self.write()
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)
        self.profile["BusinessAcceptanceSources"].pop()
        self.plan["Controls"].pop()
        self.plan["Controls"][3]["Blocking"] = True
        self.write()
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)

    def test_equivalent_ba_punctuation_whitespace_and_unicode_rejected(self):
        base_profile, base_plan = copy.deepcopy(self.profile), copy.deepcopy(self.plan)
        for equivalent in (" 用户可以导出当前内容 ", "用户可以导出当前内容", "用户可以导出当前内容．", "用户可以  导出当前内容。", "用户\u200b可以导出当前内容。", "\ufeff用户可以导出当前内容。"):
            with self.subTest(equivalent=equivalent):
                self.assertEqual(g.business_identity(equivalent), g.business_identity("用户可以导出当前内容。"))
                profile, plan = copy.deepcopy(base_profile), copy.deepcopy(base_plan)
                duplicate = copy.deepcopy(profile["BusinessAcceptanceSources"][0])
                duplicate.update(AcceptanceSourceId="BAS-002", AcceptanceText=equivalent)
                profile["BusinessAcceptanceSources"].append(duplicate)
                duplicate_ba = copy.deepcopy(plan["Controls"][0])
                duplicate_ba.update(ControlId="BA-002", AcceptanceSourceRef="BAS-002", Statement=equivalent)
                plan["Controls"].append(duplicate_ba)
                self.profile, self.plan = profile, plan
                self.write()
                with self.assertRaises(g.Invalid):
                    g.validate_plan(self.profile_path, self.plan_path)

    def test_business_identity_preserves_visible_semantic_difference(self):
        self.assertNotEqual(
            g.business_identity("用户可以导出当前内容。"),
            g.business_identity("用户不可以导出当前内容。"),
        )
        self.assertNotEqual(
            g.business_identity("支持👩‍💻导出。"),
            g.business_identity("支持👩💻导出。"),
        )
        self.assertNotEqual(
            g.business_identity("支持\u2060导出。"),
            g.business_identity("支持导出。"),
        )
        self.assertNotEqual(
            g.business_identity("a\u200cb"),
            g.business_identity("ab"),
        )

    def test_schema_instances_positive_and_negative(self):
        contracts = self.repo / "docs/Agent治理/contracts"
        profile_schema = json.loads((contracts / "control-profile.schema.json").read_text(encoding="utf-8"))
        plan_schema = json.loads((contracts / "acceptance-plan.schema.json").read_text(encoding="utf-8"))
        replay_schema = json.loads((contracts / "replay-expectations.schema.json").read_text(encoding="utf-8"))
        g.validate_schema_instance(self.profile, profile_schema, "ControlProfile")
        g.validate_schema_instance(self.plan, plan_schema, "AcceptancePlan")
        replay_expectations = {
            "SchemaVersion": "v3.0", "RequirementId": "REQ-20260915-099",
            "RequiredRuleRefs": ["RULE:GEN-012"],
            "ExpectedControlCounts": {"BA": 1, "RB": 1, "QG": 1, "AD": 1},
            "ExpectedBlockingControlsMinutes": 5, "ExpectedAdvisoryMinutes": 10,
            "ReportRequiredTokens": ["GEN-012"],
        }
        g.validate_schema_instance(replay_expectations, replay_schema, "ReplayExpectations")
        invalid_profile = copy.deepcopy(self.profile)
        invalid_profile["ExecutionBudget"]["PlanningMinutes"] = -1
        with self.assertRaises(g.Invalid):
            g.validate_schema_instance(invalid_profile, profile_schema, "ControlProfile")
        invalid_plan = copy.deepcopy(self.plan)
        invalid_plan["Controls"][3].update(Triggered=False, EstimatedMinutes=1, RiskTargetId="RISK-INVALID-AD")
        with self.assertRaises(g.Invalid):
            g.validate_schema_instance(invalid_plan, plan_schema, "AcceptancePlan")
        for control_index in (1, 2):
            with self.subTest(schema_execution_platform=self.plan["Controls"][control_index]["Class"]):
                invalid_platform = copy.deepcopy(self.plan)
                invalid_platform["Controls"][control_index]["ExecutionPlatform"] = "NONE"
                with self.assertRaises(g.Invalid):
                    g.validate_schema_instance(invalid_platform, plan_schema, "AcceptancePlan")
        invalid_replay = copy.deepcopy(replay_expectations)
        invalid_replay["ExpectedControlCounts"]["RB"] = "1"
        with self.assertRaises(g.Invalid):
            g.validate_schema_instance(invalid_replay, replay_schema, "ReplayExpectations")

    def test_uncommitted_digest_bound_sources_are_valid_and_tamper_evident(self):
        source = self.profile["BusinessAcceptanceSources"][0]
        self.assertIsNone(source["ProvenanceRevision"])
        baseline_path = self.requirement_dir / "需求基线.md"
        baseline_path.write_text(baseline_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        source["SourceDigest"] = g.content_digest(baseline_path.read_text(encoding="utf-8"))
        self.gate["EvidenceRefs"].append("uncommitted structured approval fact")
        gate_path = self.requirement_dir / "facts/gates/GATE-001.json"
        gate_path.write_text(json.dumps(self.gate, ensure_ascii=False), encoding="utf-8")
        source["ApprovalDigest"] = g.content_digest(gate_path.read_text(encoding="utf-8"))
        self.write()
        status = subprocess.run(
            ["git", "status", "--porcelain", "docs/requirements/REQ-20260915-099"],
            cwd=self.repo, check=True, capture_output=True, text=True,
        ).stdout
        self.assertTrue(status.strip())
        g.validate_plan(self.profile_path, self.plan_path)
        gate_path.write_text(gate_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)

    def test_baseline_gate_and_profile_cannot_collude_on_false_artifact_version(self):
        source = self.profile["BusinessAcceptanceSources"][0]
        source["SourceArtifactVersion"] = "v9.0"
        self.gate["Payload"]["ArtifactVersion"] = "v9.0"
        gate_path = self.requirement_dir / "facts/gates/GATE-001.json"
        gate_path.write_text(json.dumps(self.gate, ensure_ascii=False), encoding="utf-8")
        source["ApprovalDigest"] = g.content_digest(gate_path.read_text(encoding="utf-8"))
        self.write()
        with self.assertRaises(g.Invalid) as caught:
            g.validate_plan(self.profile_path, self.plan_path)
        self.assertIn("ArtifactVersion", str(caught.exception))

    def test_fifty_zero_minute_blocking_rb_cannot_pass_as_lean(self):
        rb = self.plan["Controls"][1]
        for number in range(2, 52):
            item = copy.deepcopy(rb)
            item.update(ControlId=f"RB-{number:03d}", EstimatedMinutes=0, RiskTargetId=f"RISK-ZERO-{number:03d}")
            self.plan["Controls"].append(item)
        self.profile["ExecutionBudget"].update(
            BlockingControlCount=52, AtomicRiskTargetCount=52, BlockingControlsMinutes=3,
        )
        self.write()
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)

    def test_split_or_merged_controls_cannot_evade_atomic_limits(self):
        duplicate = copy.deepcopy(self.plan["Controls"][1])
        duplicate.update(ControlId="RB-002", RiskTargetId="RISK-SAME-STATEMENT-NEW-ID")
        self.plan["Controls"].append(duplicate)
        self.profile["ExecutionBudget"].update(BlockingControlCount=3, AtomicRiskTargetCount=3, BlockingControlsMinutes=7)
        self.write()
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)
        self.plan["Controls"].pop()
        self.profile["ExecutionBudget"].update(BlockingControlCount=2, AtomicRiskTargetCount=2, BlockingControlsMinutes=5)
        for index, method in ((2, "python -m unittest && python -m compileall ."), (1, "断言保存行为；断言发布行为")):
            with self.subTest(method=method):
                original = self.plan["Controls"][index]["Method"]
                self.plan["Controls"][index]["Method"] = method
                self.write()
                with self.assertRaises(g.Invalid):
                    g.validate_plan(self.profile_path, self.plan_path)
                self.plan["Controls"][index]["Method"] = original

    def test_synonymous_statement_cannot_reuse_normalized_blocking_command(self):
        original = self.plan["Controls"][1]
        original.update(MethodKind="COMMAND", Method="python -m unittest save_regression")
        duplicate = copy.deepcopy(original)
        duplicate.update(
            ControlId="RB-002",
            Statement="保存路径行为维持不变。",
            Method="  python   -m unittest save_regression  ",
            RiskTargetId="RISK-SAVE-PATH-NEW-ID",
        )
        self.plan["Controls"].append(duplicate)
        self.profile["ExecutionBudget"].update(
            BlockingControlCount=3, AtomicRiskTargetCount=3, BlockingControlsMinutes=7,
        )
        self.write()
        with self.assertRaises(g.Invalid) as caught:
            g.validate_plan(self.profile_path, self.plan_path)
        self.assertIn("duplicate blocking command method", str(caught.exception))

    def test_lean_owner_count_budget_and_cross_binding_rejected(self):
        base_profile, base_plan = copy.deepcopy(self.profile), copy.deepcopy(self.plan)
        for mutation in ("owner", "count", "budget", "requirement"):
            with self.subTest(mutation=mutation):
                profile, plan = copy.deepcopy(base_profile), copy.deepcopy(base_plan)
                if mutation == "owner":
                    plan["Controls"][2]["Owner"] = "QA"
                elif mutation == "count":
                    profile["ExecutionBudget"]["BlockingControlCount"] = 3
                elif mutation == "budget":
                    plan["Controls"][2]["EstimatedMinutes"] = 20
                else:
                    plan["RequirementId"] = "REQ-20260915-098"
                self.profile, self.plan = profile, plan
                self.write()
                with self.assertRaises(g.Invalid):
                    g.validate_plan(self.profile_path, self.plan_path)

    def test_unknown_fields_duplicate_keys_and_sensitive_content_rejected(self):
        self.plan["unexpected"] = True
        self.write()
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)
        self.plan.pop("unexpected")
        self.profile_path.write_text('{"SchemaVersion":"v3.0","SchemaVersion":"v3.0"}', encoding="utf-8")
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)
        self.write()
        self.plan["Controls"][1]["Statement"] = "password=synthetic-secret"
        self.write()
        with self.assertRaises(g.Invalid) as caught:
            g.validate_plan(self.profile_path, self.plan_path)
        self.assertNotIn("synthetic-secret", str(caught.exception))

    def test_provenance_reference_and_downgrade_rejected(self):
        self.profile["BusinessAcceptanceSources"][0]["SourceRef"] += "-missing"
        self.write()
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)

    def test_tracked_profile_cannot_select_fake_predecessor_or_downgrade(self):
        lean_profile, lean_plan = copy.deepcopy(self.profile), copy.deepcopy(self.plan)
        hard = next(item for item in self.profile["Classification"]["HardTriggers"] if item["Code"] == "PRODUCTION_RELEASE")
        hard["Matched"] = True
        self.profile.update(SelectedProfile="CONTROLLED", MinimumProfile="CONTROLLED")
        self.add_independent_record_refs()
        self.plan["Controls"][1].update(Owner="REVIEWER", MethodKind="STATIC_REVIEW")
        self.plan["Controls"][2]["Owner"] = "QA"
        self.write()
        subprocess.run(["git", "add", "control-profile.json", "acceptance-plan.json"], cwd=self.profile_path.parent, check=True)
        subprocess.run(["git", "commit", "-qm", "effective controlled profile fixture"], cwd=self.profile_path.parent, check=True)
        previous_revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.profile_path.parent, check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.profile, self.plan = lean_profile, lean_plan
        self.profile.update(ProfileVersion="v2.0", PreviousProfileRef="control-profile.json", PreviousProfileRevision=previous_revision)
        self.plan.update(PlanVersion="v2.0", PreviousPlanRef="acceptance-plan.json", PreviousPlanRevision=previous_revision)
        self.write()
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)

    def test_new_plan_version_cannot_remove_triggered_blocking_control(self):
        self.write()
        subprocess.run(["git", "add", "control-profile.json", "acceptance-plan.json"], cwd=self.requirement_dir, check=True)
        subprocess.run(["git", "commit", "-qm", "effective lean plan fixture"], cwd=self.repo, check=True)
        previous_revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.profile.update(ProfileVersion="v2.0", PreviousProfileRef="control-profile.json", PreviousProfileRevision=previous_revision)
        self.plan.update(PlanVersion="v2.0", PreviousPlanRef="acceptance-plan.json", PreviousPlanRevision=previous_revision)
        self.plan["Controls"] = [item for item in self.plan["Controls"] if item["ControlId"] != "RB-001"]
        self.write()
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)

    def test_new_plan_version_cannot_weaken_blocking_method(self):
        self.write()
        subprocess.run(["git", "add", "control-profile.json", "acceptance-plan.json"], cwd=self.requirement_dir, check=True)
        subprocess.run(["git", "commit", "-qm", "effective lean method fixture"], cwd=self.repo, check=True)
        previous_revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.profile.update(ProfileVersion="v2.0", PreviousProfileRef="control-profile.json", PreviousProfileRevision=previous_revision)
        self.plan.update(PlanVersion="v2.0", PreviousPlanRef="acceptance-plan.json", PreviousPlanRevision=previous_revision)
        self.plan["Controls"][2]["Method"] = "command-that-always-exits-zero"
        self.write()
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)

    def test_untriggered_control_and_reference_binding(self):
        rb = self.plan["Controls"][1]
        rb.update(Triggered=False, Blocking=False, MethodKind="NONE", Method=None, ExecutionPlatform="NONE", EstimatedMinutes=0, RiskTargetId=None)
        self.profile["ExecutionBudget"].update(BlockingControlCount=1, AtomicRiskTargetCount=1, BlockingControlsMinutes=3)
        self.write()
        g.validate_plan(self.profile_path, self.plan_path)
        self.plan["ControlProfileRef"] = "missing.json"
        self.write()
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)
        self.plan["ControlProfileRef"] = "control-profile.json"
        self.profile["RecordRefs"][-1] = "docs/requirements/REQ-20260915-099/missing/开发记录.md"
        self.write()
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)
        self.profile["RecordRefs"][-1] = "docs/requirements/REQ-20260915-098/开发记录.md"
        self.write()
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.profile_path, self.plan_path)


if __name__ == "__main__":
    unittest.main()
