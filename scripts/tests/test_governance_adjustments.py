"""Version binding and frozen-plan adjustment tests using isolated Git fixtures."""
import copy
import json
from pathlib import Path
import unittest
import test_governance_tools as fixtures

g = fixtures.g


class AdjustmentTests(unittest.TestCase):
    def setUp(self):
        f = fixtures.V3PlanValidationTests(methodName="test_valid_lean_and_readonly_cli")
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.f = f
        for key in ("SchemaVersion", "GovernanceVersion", "DecisionRulesVersion"):
            f.profile[key] = "v3.0.1"
        for key in ("SchemaVersion", "GovernanceVersion"):
            f.plan[key] = "v3.0.1"
        bindings = {"BusinessRevision": f.provenance_revision}
        for key, name in (("Governance", "治理版本说明/v3.0.1治理修订说明.md"), ("Testing", "开发规范/测试规范.md")):
            path = f.repo / "docs" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Fixture\n版本：v3.0.1\n", encoding="utf-8")
            bindings[key] = dict(Version="v3.0.1", SourceRef=f"docs/{name}", SourceDigest=g.content_digest(path.read_text(encoding="utf-8")), Revision=None)
        f.profile["BaselineBindings"] = bindings
        f.profile["SelectedProfile"] = "STANDARD"
        next(item for item in f.profile["Classification"]["UpgradeTriggers"] if item["Code"] == "INDEPENDENT_QUALITY_JUDGMENT")["Matched"] = True
        f.add_independent_record_refs()
        f.plan["Controls"][1].update(Owner="REVIEWER", MethodKind="STATIC_REVIEW")
        f.plan["Controls"][2]["Owner"] = "QA"
        f.write()
        self.candidate = self.fact("CAND-001", "CANDIDATE_STATE", {}, "candidates", 2)
        gate = self.fact("GATE-002", "GATE_DECISION", dict(Gate="G2", Decision="FROZEN", ArtifactRef="docs/requirements/REQ-20260915-099/开发上下文包.md", ArtifactVersion="v1.0"), "gates", 1)
        self.origin = self.fact("QA-001", "QA_RESULT", dict(Status="BLOCKED", CountDisposition="ACTIVE", ObservedFailures=[], BlockingItems=[dict(AffectedValidationIds=["V-01"])], NotExecuted=[dict(ValidationId="V-01", ControlId="QG-001")]), "qa", 3)
        coverage = f.requirement_dir / "coverage.md"
        coverage.write_text("# Equivalence\nSame risk and targeted regression; fixture only.\n", encoding="utf-8")
        self.path = f.requirement_dir / "adjustments/ADJ-001.json"
        self.path.parent.mkdir()
        prefix = "docs/requirements/REQ-20260915-099/"
        self.data = dict(GovernanceVersion="v3.0.1", RequirementId=f.profile["RequirementId"],
            OriginalPlanRef=prefix + "acceptance-plan.json", OriginalPlanDigest=self.digest(f.plan_path),
            ControlProfileRef=prefix + "control-profile.json", ControlProfileDigest=self.digest(f.profile_path),
            CandidateRef=prefix + "facts/candidates/CAND-001.json", CandidateEventDigest=self.digest(self.candidate),
            G2Ref=prefix + "facts/gates/GATE-002.json", G2Digest=self.digest(gate), OriginQARef=prefix + "facts/qa/QA-001.json", OriginQADigest=self.digest(self.origin),
            Reason="BASELINE_UNATTRIBUTABLE", EvidenceRefs=[prefix + "coverage.md"], EvidenceDigests={prefix + "coverage.md": self.digest(coverage)},
            Replacements=[dict(ControlId="QG-001", OriginalValidationIds=["V-01"], Statement="相关风险的定向回归通过。", Method="python -m unittest targeted", MethodKind="COMMAND", EstimatedMinutes=3, CoverageEvidenceRefs=[prefix + "coverage.md"])],
            ReviewerRef=prefix + "facts/reviews/RV-002.json", ReviewerDigest="", ApprovalRef=prefix + "facts/recoveries/REC-001.json", ApprovalDigest="")
        self.approve()

    def digest(self, path):
        return g.content_digest(path.read_text(encoding="utf-8"))

    def fact(self, ident, kind, payload, folder, minute):
        value = copy.deepcopy(self.f.gate)
        value.update(EventId=ident, EventType=kind, ContextPackVersion="v1.0", CandidateRef="CAND-001", Payload=payload, CreatedAt=f"2026-09-15T01:{minute:02d}:00+08:00")
        path = self.f.requirement_dir / f"facts/{folder}/{ident}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def approve(self):
        digest = g.adjustment_digest(self.data)
        reviewer = self.fact("RV-002", "REVIEW_RESULT", dict(Decision="APPROVE", CandidateDigestVerified=True, Findings=[], EvidenceIssues=[], AdjustmentDigest=digest), "reviews", 4)
        approval = self.fact("REC-001", "HUMAN_RECOVERY", dict(Decision="APPROVED", ApprovalRef="current-user-message:2026-09-15T01:05:00+08:00", AdjustmentDigest=digest), "recoveries", 5)
        self.data.update(ReviewerDigest=self.digest(reviewer), ApprovalDigest=self.digest(approval))
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.data), encoding="utf-8")

    def test_adjustment_is_readonly_and_does_not_claim_quality_pass(self):
        before = {p: p.read_bytes() for p in self.f.requirement_dir.rglob("*") if p.is_file()}
        result = g.validate_adjustment(self.path)
        self.assertTrue(result["Valid"])
        self.assertFalse(result["QualityPassed"])
        self.assertEqual(result["EffectiveControls"][2]["Method"], "python -m unittest targeted")
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_each_version_binding_is_independently_checked(self):
        original = copy.deepcopy(self.f.profile)
        for target in ("Governance", "Testing", "BusinessRevision"):
            with self.subTest(target=target):
                self.f.profile = copy.deepcopy(original)
                if target == "BusinessRevision":
                    self.f.profile["BaselineBindings"][target] = "0" * 40
                else:
                    self.f.profile["BaselineBindings"][target]["SourceDigest"] = "sha256:" + "0" * 64
                self.f.write()
                with self.assertRaises(g.Invalid):
                    g.validate_plan(self.f.profile_path, self.f.plan_path)

    def test_approval_cannot_be_reused_after_adjustment_changes(self):
        self.data["Replacements"][0]["Method"] = "python changed.py"
        self.write()
        with self.assertRaisesRegex(g.Invalid, "approval content binding"):
            g.validate_adjustment(self.path)

    def test_ba_and_unknown_validation_cannot_be_substituted(self):
        for key, value in (("ControlId", "BA-001"), ("OriginalValidationIds", ["V-UNKNOWN"]), ("EstimatedMinutes", 0)):
            with self.subTest(key=key):
                original = copy.deepcopy(self.data["Replacements"])
                self.data["Replacements"][0][key] = value
                self.approve()
                with self.assertRaises(g.Invalid):
                    g.validate_adjustment(self.path)
                self.data["Replacements"] = original

    def test_candidate_failure_cannot_be_reclassified_as_environment(self):
        value = json.loads(self.origin.read_text())
        value["Payload"].update(Status="FAIL", ObservedFailures=["candidate regression"])
        self.origin.write_text(json.dumps(value), encoding="utf-8")
        self.data["OriginQADigest"] = self.digest(self.origin)
        self.approve()
        with self.assertRaisesRegex(g.Invalid, "cannot bypass candidate FAIL"):
            g.validate_adjustment(self.path)

    def test_observed_unattributed_suite_failure_can_use_controlled_adjustment(self):
        value = json.loads(self.origin.read_text())
        evidence = "docs/requirements/REQ-20260915-099/coverage.md"
        value["Payload"]["ObservedFailures"] = [dict(Attribution="UNATTRIBUTED", ValidationIds=["V-01"], EvidenceRefs=[evidence])]
        self.origin.write_text(json.dumps(value), encoding="utf-8")
        self.data["OriginQADigest"] = self.digest(self.origin)
        self.approve()
        self.assertTrue(g.validate_adjustment(self.path)["Valid"])
        value["Payload"]["ObservedFailures"][0]["Attribution"] = "CANDIDATE"
        self.origin.write_text(json.dumps(value), encoding="utf-8")
        self.data["OriginQADigest"] = self.digest(self.origin)
        self.approve()
        with self.assertRaisesRegex(g.Invalid, "cannot bypass candidate FAIL"):
            g.validate_adjustment(self.path)

    def test_original_plan_tampering_is_detected(self):
        self.f.plan["Controls"][2]["Method"] = "new command"
        self.f.write()
        with self.assertRaisesRegex(g.Invalid, "original plan digest"):
            g.validate_adjustment(self.path)

    def test_coverage_evidence_tampering_is_detected(self):
        (self.f.requirement_dir / "coverage.md").write_text("We removed the risk coverage", encoding="utf-8")
        with self.assertRaisesRegex(g.Invalid, "evidence digest"):
            g.validate_adjustment(self.path)

    def test_new_version_requires_bindings_and_matching_plan(self):
        self.f.profile.pop("BaselineBindings")
        self.f.write()
        with self.assertRaises(g.Invalid):
            g.validate_plan(self.f.profile_path, self.f.plan_path)

    def quality(self):
        common = dict(AdjustmentRef=self.path.relative_to(self.f.repo).as_posix(), AdjustmentDigest=g.adjustment_digest(self.data),
                      CandidateDigestVerified=True, CountDisposition="ACTIVE", DispositionRef=None,
                      StartedAt="2026-09-15T01:06:00+08:00", CompletedAt="2026-09-15T01:07:00+08:00", DurationMs=60000)
        item = dict(ControlId="QG-001", ValidationId="V-01", GateClass="BLOCKING", CommandOrMethod="python -m unittest targeted", Result="PASS", Disposition="EXECUTED",
                    StartedAt=common["StartedAt"], CompletedAt=common["CompletedAt"], DurationMs=60000, EvidenceRefs=["fixture test log"])
        qa = dict(common, QARound=2, Status="PASS", RedactionState="SAFE", SensitiveEvidenceIssues=[], BlockingItems=[], ObservedFailures=[],
                  ValidationPlanRef=self.data["OriginalPlanRef"], EffectiveValidationSet=[item], NotExecuted=[])
        review = dict(common, ReviewerRound=3, Decision="APPROVE", ReviewScope="STATIC_CANDIDATE", Findings=[], EvidenceIssues=[])
        return self.fact("QA-002", "QA_RESULT", qa, "qa", 8), self.fact("RV-003", "REVIEW_RESULT", review, "reviews", 9)

    def test_quality_join_requires_actual_results_and_leaves_human_acceptance_open(self):
        qa, review = self.quality()
        result = g.validate_adjusted_quality(self.path, qa, review)
        self.assertTrue(result["ReadyForHumanAcceptance"])
        self.assertFalse(result["HumanAccepted"])
        value = json.loads(qa.read_text())
        value["Payload"]["EffectiveValidationSet"] = []
        qa.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(g.Invalid, "complete effective control coverage"):
            g.validate_adjusted_quality(self.path, qa, review)

    def test_quality_rejects_old_method_and_cross_candidate_results(self):
        for mutation in ("method", "candidate", "approval", "review", "round", "gate"):
            with self.subTest(mutation=mutation):
                qa, review = self.quality()
                path = review if mutation == "review" else qa
                value = json.loads(path.read_text())
                if mutation == "method":
                    value["Payload"]["EffectiveValidationSet"][0]["CommandOrMethod"] = "python -m unittest"
                elif mutation == "candidate":
                    value["CandidateRef"] = "CAND-002"
                elif mutation == "approval":
                    value["Payload"]["AdjustmentDigest"] = "sha256:" + "0" * 64
                elif mutation == "round":
                    value["Payload"]["QARound"] = 0
                elif mutation == "gate":
                    value["Payload"]["EffectiveValidationSet"][0]["GateClass"] = "ADVISORY"
                else:
                    value["Payload"]["Decision"] = "CHANGE"
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(g.Invalid):
                    g.validate_adjusted_quality(self.path, qa, review)

    def test_quality_rejects_stale_pass_after_later_failure(self):
        qa, review = self.quality()
        self.fact("QA-003", "QA_RESULT", dict(Status="FAIL", CountDisposition="ACTIVE"), "qa", 10)
        with self.assertRaisesRegex(g.Invalid, "stale selected"):
            g.validate_adjusted_quality(self.path, qa, review)


if __name__ == "__main__":
    unittest.main()
