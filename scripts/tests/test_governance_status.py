"""Read-only status, incomplete facts and real B2 snapshots in synthetic repositories."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import governance_status as status
import governance_candidates as candidates
import governance_rules as rules
sys.path.pop(0)
import test_governance_local_tests as fixtures


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.LocalTests(methodName="test_project_sources_generate_selection_without_a_task_manifest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.repo = self.case.repo
        self.root = self.case.fixture.requirement_dir
        self.req = self.case.requirement
        self.context = self.case.body.replace("- **产物当前状态：** FROZEN", "- **产物当前状态：** FROZEN\n- **事实协议版本：** v3.0")
        self.context = self.context.replace("#### 5.2.1", "## 2. 工作包与归属边界\n\n| WorkPackageId | 工作 |\n|---|---|\n| WP-01 | local change |\n\n## 5. 验证\n\n#### 5.2.1")
        self.case.context.write_text(self.context, encoding="utf-8")
        self.baseline = candidates.git(self.repo, "rev-parse", "HEAD").decode().strip()
        self.writer_ref = None

    def event(self, ident, kind, payload, folder, *, candidate=None, when="2026-10-28T01:00:02+08:00", lineage=None):
        event = {"SchemaVersion": "v1.1", "RequirementId": self.req, "ContextPackVersion": "v1.0",
                 "RepairLineageRef": lineage, "EventId": ident, "EventType": kind, "CreatedAt": when,
                 "Actor": "Synthetic runtime", "CandidateRef": candidate, "Payload": payload,
                 "EvidenceRefs": [], "SupersedesEventRef": None, "RedactionState": "SAFE"}
        path = self.root / "facts" / folder / (ident + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(event), encoding="utf-8")
        return path

    def query(self):
        return status.query_status(self.repo, self.req)

    def writer(self):
        (self.repo / "AGENTS.md").write_text("# candidate change\n", encoding="utf-8")
        change = {"ActionId": "ACT-01", "Operation": "MODIFY", "Path": "AGENTS.md", "CodeUnit": "entry", "Summary": "local change", "Reason": "scope", "EvidenceRefs": []}
        path = self.event("WR-001", "WRITER_HANDOFF", {"FactProtocolVersion": "v2.1", "Status": "COMPLETED", "WriterRound": 1,
                          "WorkPackageId": "WP-01", "WriterSlot": "Writer 1", "SensitiveEvidenceDetected": False, "Changes": [change],
                          "SelfValidation": [], "NotValidated": [], "Failures": [], "ResidualRisks": [], "BoundaryIssues": [],
                          "BlockingItems": [], "RequiredConfirmations": []}, "writers")
        self.writer_ref = path.relative_to(self.repo).as_posix()
        return path

    def candidate(self):
        if self.writer_ref is None:
            self.writer()
        captured = candidates.capture_snapshot(self.repo, requirement_id=self.req, context_version="v1.0", repair_lineage_ref=None,
                                               baseline_revision=self.baseline, writer_handoff_refs=[self.writer_ref], scope_paths=["AGENTS.md"])
        payload = copy.deepcopy(captured["CandidateFactDraft"]["Payload"])
        payload["DraftOnly"] = False  # Only the existing runtime can register a candidate identity.
        return self.event("CAND-001", "CANDIDATE_STATE", payload, "candidates", candidate="CAND-001", when="2026-10-28T01:00:03+08:00")

    def standard(self):
        fixture = self.case.fixture
        fixture.profile.update(SelectedProfile="STANDARD", MinimumProfile="STANDARD")
        next(item for item in fixture.profile["Classification"]["UpgradeTriggers"] if item["Code"] == "INDEPENDENT_QUALITY_JUDGMENT")["Matched"] = True
        for name in ("临时质量验证记录.md", "只读评审记录.md"):
            fixture.profile["RecordRefs"].append("docs/requirements/" + self.req + "/" + name)
        fixture.plan["Controls"][1].update(Owner="REVIEWER", MethodKind="STATIC_REVIEW", Method="static review")
        fixture.plan["Controls"][2]["Owner"] = "QA"
        self.context = self.context.replace("| WRITER / WP-01 | DATABASE", "| QA / WP-01 | DATABASE")
        self.case.context.write_text(self.context, encoding="utf-8")
        fixture.write()

    def qa(self, *, state="PASS"):
        item = {"ValidationId": "V-02", "ControlId": "QG-001", "GateClass": "BLOCKING", "GateTarget": "CANDIDATE_QUALITY",
                "AffectedWorkPackages": ["WP-01"], "Disposition": "EXECUTED", "Result": "PASS", "CommandOrMethod": "python support.py integration",
                "ActualResult": "synthetic result", "StartedAt": "2026-10-28T01:00:05+08:00", "CompletedAt": "2026-10-28T01:00:06+08:00",
                "DurationMs": 1000, "SourceQAEventRef": None, "EvidenceRefs": []}
        payload = {"FactProtocolVersion": "v2.1", "RedactionState": "SAFE", "SensitiveEvidenceIssues": [], "QARound": 1,
                   "Status": state, "CandidateDigestVerified": True, "StartedAt": item["StartedAt"], "CompletedAt": item["CompletedAt"],
                   "DurationMs": 1000, "EffectiveValidationSet": [item], "NotExecuted": [], "BlockingItems": [], "ObservedFailures": [],
                   "DispositionRef": None, "CountDisposition": "ACTIVE", "ValidationPlanRef": "acceptance-plan.json", "RevalidationPlanRef": None}
        return self.event("QA-001", "QA_RESULT", payload, "qa", candidate="CAND-001", when="2026-10-28T01:00:07+08:00")

    def review(self):
        return self.event("RV-001", "REVIEW_RESULT", {"ReviewerRound": 1, "ReviewScope": "STATIC_CANDIDATE", "Decision": "APPROVE",
                         "CandidateDigestVerified": True, "StartedAt": "2026-10-28T01:00:04+08:00", "CompletedAt": "2026-10-28T01:00:05+08:00",
                         "DurationMs": 1000, "Findings": [], "EvidenceIssues": [], "Basis": ["frozen context"], "DispositionRef": None, "CountDisposition": "ACTIVE"},
                          "reviews", candidate="CAND-001", when="2026-10-28T01:00:07+08:00")

    def test_frozen_plan_reports_missing_implementation(self):
        report = self.query()
        self.assertEqual(report["CurrentStage"], "IMPLEMENTATION_INCOMPLETE", report)
        self.assertFalse(report["QualityPassed"])

    def test_writer_completion_alone_requires_candidate_not_quality_pass(self):
        self.writer()
        report = self.query()
        self.assertEqual(report["CurrentStage"], "CANDIDATE_REQUIRED", report)
        self.assertFalse(report["HumanAccepted"])

    def test_lean_does_not_demand_independent_qa_or_reviewer(self):
        self.candidate()
        report = self.query()
        self.assertEqual(report["CurrentStage"], "LEAN_SELF_VALIDATION_REVIEW", report)
        self.assertFalse(report["Observations"]["IndependentQARequired"])
        self.assertFalse(report["Observations"]["IndependentReviewerRequired"])

    def test_current_worktree_and_snapshot_tampering_are_not_reported_as_valid(self):
        self.candidate()
        (self.repo / "AGENTS.md").write_text("changed after candidate", encoding="utf-8")
        report = self.query()
        self.assertEqual(report["QueryStatus"], "UNDETERMINED")
        self.assertIsNone(report["HumanAccepted"])

    def test_unknown_events_and_corrections_stop_inference(self):
        self.event("NEW-001", "UNKNOWN_EVENT", {}, "misc")
        self.assertEqual(self.query()["QueryStatus"], "UNDETERMINED")
        (self.root / "facts/misc/NEW-001.json").unlink()
        self.event("COR-001", "FACT_CORRECTION", {}, "corrections")
        self.assertEqual(self.query()["QueryStatus"], "UNDETERMINED")

    def test_standard_missing_independent_qa_is_visible(self):
        self.standard()
        self.candidate()
        report = self.query()
        self.assertEqual(report["CurrentStage"], "QA_RESULT_REQUIRED", report)
        self.assertIn("QA_RESULT", report["Missing"])

    def test_qa_pass_without_reviewer_is_not_ready_for_business_acceptance(self):
        self.standard()
        self.candidate()
        self.qa()
        report = self.query()
        self.assertEqual(report["CurrentStage"], "REVIEW_RESULT_REQUIRED", report)
        self.assertFalse(report["QualityPassed"])

    def test_complete_quality_observations_still_require_existing_runtime_join(self):
        self.standard()
        self.candidate()
        self.qa()
        self.review()
        report = self.query()
        self.assertEqual(report["CurrentStage"], "QUALITY_OBSERVATIONS_COMPLETE", report)
        self.assertFalse(report["QualityPassed"])
        self.assertFalse(report["HumanAccepted"])

    def test_incomplete_effective_validation_set_is_rejected(self):
        self.standard()
        self.candidate()
        path = self.qa()
        value = json.loads(path.read_text(encoding="utf-8"))
        value["Payload"]["EffectiveValidationSet"] = []
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(self.query()["QueryStatus"], "UNDETERMINED")

    def test_stale_markdown_cannot_override_current_facts_and_query_writes_nothing(self):
        self.writer()
        (self.root / "开发记录.md").write_text("QA PASS; Human accepted; archived", encoding="utf-8")
        before = {path: path.read_bytes() for path in self.repo.rglob("*") if path.is_file()}
        report = self.query()
        self.assertEqual(report["CurrentStage"], "CANDIDATE_REQUIRED", report)
        self.assertEqual(before, {path: path.read_bytes() for path in self.repo.rglob("*") if path.is_file()})

    def test_unknown_human_acceptance_payload_is_not_treated_as_approval(self):
        self.event("HA-001", "HUMAN_ACCEPTANCE", {"Approved": True}, "approvals", candidate="CAND-001")
        self.assertEqual(self.query()["QueryStatus"], "UNDETERMINED")
        self.assertIsNone(self.query()["HumanAccepted"])

    def test_cli_sanitizes_sensitive_input_errors(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), mock.patch.dict(os.environ, {"GIT_DIR": "password=synthetic-private-value"}):
            result = status.main(["--repo", str(self.repo), "--requirement-id", self.req])
        self.assertEqual(result, 1)
        self.assertNotIn("synthetic-private-value", stdout.getvalue() + stderr.getvalue())

    def test_qa_plan_method_target_and_workpackage_mismatch_are_rejected(self):
        self.standard()
        self.candidate()
        path = self.qa()
        self.review()
        original = json.loads(path.read_text(encoding="utf-8"))
        mutations = [lambda value: value["Payload"].update(ValidationPlanRef="obsolete-plan.json"),
                     lambda value: value["Payload"]["EffectiveValidationSet"][0].update(CommandOrMethod="different command"),
                     lambda value: value["Payload"]["EffectiveValidationSet"][0].update(GateTarget="NON_QA_FOLLOW_UP"),
                     lambda value: value["Payload"]["EffectiveValidationSet"][0].update(AffectedWorkPackages=["WP-99"])]
        for change in mutations:
            value = copy.deepcopy(original)
            change(value)
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(self.query()["QueryStatus"], "UNDETERMINED")

    def test_g2_invalid_automatic_rejection_and_frozen_blockers_are_rejected(self):
        path = self.root / "facts/gates/GATE-002.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        value = copy.deepcopy(original)
        value["Payload"].update(DecisionMode="AUTOMATIC", Decision="REJECTED", ApprovalRef=None)
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(self.query()["QueryStatus"], "UNDETERMINED")
        value = copy.deepcopy(original)
        value["Payload"]["BlockingItems"] = ["unresolved prerequisite"]
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(self.query()["QueryStatus"], "UNDETERMINED")

    def test_same_version_plan_change_after_snapshot_is_detected(self):
        self.candidate()
        self.case.context.write_text(self.case.context.read_text(encoding="utf-8") + "\nextra context\n", encoding="utf-8")
        self.assertEqual(self.query()["QueryStatus"], "UNDETERMINED")

    def test_equivalent_timestamps_do_not_silently_select_a_latest_fact(self):
        with self.assertRaises(status.Uncertain):
            status.latest([{"CreatedAt": "2026-10-28T01:00:02+08:00"}, {"CreatedAt": "2026-10-27T17:00:02Z"}], "WRITER")

    def test_malformed_readiness_entries_return_undetermined_without_traceback(self):
        self.standard()
        self.candidate()
        self.event("TR-001", "TEST_READINESS", {"ValidationPlanRef": "acceptance-plan.json", "Entries": ["invalid entry"]},
                   "readiness", candidate="CAND-001", when="2026-10-28T01:00:04+08:00")
        self.assertEqual(self.query()["QueryStatus"], "UNDETERMINED")

    def test_quality_execution_before_candidate_is_not_accepted(self):
        self.standard()
        self.candidate()
        path = self.qa()
        self.review()
        value = json.loads(path.read_text(encoding="utf-8"))
        for entry in (value["Payload"], value["Payload"]["EffectiveValidationSet"][0]):
            entry.update(StartedAt="2026-10-28T01:00:01+08:00", CompletedAt="2026-10-28T01:00:02+08:00")
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(self.query()["QueryStatus"], "UNDETERMINED")

    def test_late_g2_cannot_justify_prior_execution(self):
        path = self.root / "facts/gates/GATE-002.json"
        gate = json.loads(path.read_text(encoding="utf-8"))
        gate["CreatedAt"] = "2026-10-28T01:00:09+08:00"
        path.write_text(json.dumps(gate), encoding="utf-8")
        self.candidate()
        self.assertEqual(self.query()["QueryStatus"], "UNDETERMINED")

    def test_sensitive_source_filenames_are_not_leaked(self):
        (self.root / "password=synthetic-private-value.md").write_text("ordinary note", encoding="utf-8")
        report = self.query()
        self.assertEqual(report["QueryStatus"], "UNDETERMINED")
        self.assertNotIn("synthetic-private-value", json.dumps(report))

    def test_rule_projection_sources_are_checked_with_candidate_inputs(self):
        source = Path(__file__).resolve().parents[2]
        for name in ("rulepacks", "examples/rules"):
            shutil.copytree(source / name, self.repo / name)
        (self.repo / rules.CORE_SOURCE).write_bytes((source / rules.CORE_SOURCE).read_bytes())
        projection = rules.compose_rules(self.repo, "examples/rules/fullstack-react/project.json", ["web"], ["FE-001"])
        (self.root / "rules-projection.json").write_text(json.dumps(projection), encoding="utf-8")
        self.candidate()
        report = self.query()
        self.assertEqual(report["CurrentStage"], "LEAN_SELF_VALIDATION_REVIEW", report)
        self.assertTrue(report["Observations"]["RuleSourcePinsVerified"])
        self.assertFalse(report["Observations"]["RuleRequirementAuthorizationVerified"])
        path = self.repo / "examples/rules/fullstack-react/web/conventions.md"
        path.write_text(path.read_text(encoding="utf-8") + "\nnew convention\n", encoding="utf-8")
        self.assertEqual(self.query()["QueryStatus"], "UNDETERMINED")


if __name__ == "__main__":
    unittest.main()
