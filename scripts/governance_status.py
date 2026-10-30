"""Read-only status from verified sources; never grants gates or dispatches roles."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True
import governance_environment as environment
import governance_local_tests as local
import governance_tools as g


class Uncertain(ValueError):
    pass


SUPPORTED = {"GATE_DECISION", "WRITER_HANDOFF", "CANDIDATE_STATE", "TEST_READINESS", "QA_RESULT", "REVIEW_RESULT"}
FOLDERS = {"GATE_DECISION": "gates", "WRITER_HANDOFF": "writers", "CANDIDATE_STATE": "candidates",
           "TEST_READINESS": "readiness", "QA_RESULT": "qa", "REVIEW_RESULT": "reviews"}


def require(value, code):
    if not value:
        raise Uncertain(code)


def sources(repo, root):
    paths = [path for path in root.glob("*") if path.suffix in (".json", ".md")]
    paths += list((root / "facts").rglob("*.json"))
    require(len(paths) <= 1024, "SOURCE_LIMIT")
    result = {}
    for path in sorted(paths):
        g.safe_path(repo, path)
        require(path.is_file() and path.stat().st_size <= 1024 * 1024, "SOURCE_LIMIT")
        reference = path.relative_to(repo).as_posix()
        g.sensitive(reference)
        result[reference] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def latest(events, label):
    if not events:
        return None
    ordered = sorted(events, key=lambda event: g.timestamp(event["CreatedAt"]))
    require(len(ordered) < 2 or g.timestamp(ordered[-1]["CreatedAt"]) != g.timestamp(ordered[-2]["CreatedAt"]), "AMBIGUOUS_" + label)
    return ordered[-1]


def planned_workpackages(text):
    body = g.markdown_body(re.sub(r"<!--.*?-->", "", text, flags=re.S))
    section = re.search(r"(?ms)^## 2\. [^\n]*\n(.*?)(?=^## |\Z)", body)
    require(section is not None, "UNSUPPORTED_WORKPACKAGE_TABLE")
    ids = re.findall(r"(?m)^\|\s*`?(WP-[A-Z0-9_-]+)`?\s*\|", section[1])
    require(bool(ids) and len(set(ids)) == len(ids), "AMBIGUOUS_WORKPACKAGES")
    return set(ids)


def validation_map(text, controls, owner):
    definitions, executions = local.table(text, "5.2.1", 5), local.table(text, "5.2.2", 7)
    require(definitions.keys() == executions.keys(), "VALIDATION_TABLE_COVERAGE")
    result, covered = {}, set()
    for ident, definition in definitions.items():
        refs = re.findall(r"(?:RB|QG|AD)-\d{3}", definition[1])
        require(len(refs) == 1 and refs[0] in controls, "UNSUPPORTED_VALIDATION_CONTROL_MAP")
        control = controls[refs[0]]
        if control["Owner"] != owner or not control["Triggered"]:
            continue
        execution = executions[ident]
        gate = "BLOCKING" if control["Blocking"] else "ADVISORY"
        target = "CANDIDATE_QUALITY" if control["Blocking"] else "NON_QA_FOLLOW_UP"
        require(definition[3] == control["Method"], "PLAN_CONTEXT_METHOD_CONFLICT")
        require([local.plain(part) for part in execution[2].split("/")] == [gate, target, "TRUE"], "PLAN_CONTEXT_GATE_CONFLICT")
        require(local.plain(execution[3].split("/")[0]) == owner, "PLAN_CONTEXT_OWNER_CONFLICT")
        affected = re.findall(r"\bWP-[A-Z0-9_-]+\b", execution[3])
        require(bool(affected) and len(set(affected)) == len(affected), "VALIDATION_WORKPACKAGE_MAPPING_MISSING")
        require(refs[0] not in covered, "AMBIGUOUS_VALIDATION_CONTROL_MAP")
        covered.add(refs[0])
        result[ident] = {"ControlId": refs[0], "GateClass": gate, "GateTarget": target,
                         "Method": control["Method"], "AffectedWorkPackages": sorted(affected)}
    expected = {ident for ident, control in controls.items() if control["Triggered"] and control["Owner"] == owner}
    require(covered == expected, "INCOMPLETE_VALIDATION_CONTROL_MAP")
    return result


def find_event(events, ref, kind, source):
    return g.resolve(events, ref, kind, source)


def candidate_identity(event):
    return {event["EventId"], event["_path"], "docs/requirements/" + event["RequirementId"] + "/" + event["_path"]}


def readiness(event, expected, events, candidate, workpackages):
    payload = event["Payload"]
    require(event["CandidateRef"] in candidate_identity(candidate), "READINESS_CANDIDATE_CONFLICT")
    find_event(events, event["CandidateRef"], "CANDIDATE_STATE", event)
    entries = g.array(payload.get("Entries"), "readiness entries")
    require(all(isinstance(item, dict) for item in entries), "INVALID_READINESS_ENTRY")
    require(len({item.get("ValidationId") for item in entries}) == len(entries), "DUPLICATE_READINESS_ENTRY")
    for item in entries:
        ident = item.get("ValidationId")
        require(ident in expected, "UNKNOWN_READINESS_VALIDATION")
        require(item.get("Readiness") in ("READY", "NOT_READY"), "INVALID_READINESS")
        require(item.get("GateClass") == expected[ident]["GateClass"] and item.get("GateTarget") == expected[ident]["GateTarget"], "READINESS_GATE_CONFLICT")
        affected = g.unique_strings(item.get("AffectedWorkPackages"), "readiness workpackages", minimum=1)
        require(set(affected) <= workpackages and sorted(affected) == expected[ident]["AffectedWorkPackages"], "UNKNOWN_READINESS_WORKPACKAGE")
    require(set(item["ValidationId"] for item in entries) == set(expected), "INCOMPLETE_READINESS_SET")
    return entries


def repair_links(events, workpackages):
    result = []
    for review in events:
        if review["EventType"] != "REVIEW_RESULT":
            continue
        for finding in g.array(review["Payload"].get("Findings"), "review findings"):
            require(isinstance(finding, dict), "INVALID_FINDING")
            ident = g.string(finding.get("FindingId"), "FindingId")
            affected = finding.get("AffectedWorkPackages")
            require(affected is None or isinstance(affected, list) and set(affected) <= workpackages, "FINDING_WORKPACKAGE_CONFLICT")
            references = {ref + "#" + ident for ref in candidate_identity(review)}
            changes = []
            for writer in events:
                if writer["EventType"] != "WRITER_HANDOFF" or writer["RepairLineageRef"] != review["RepairLineageRef"] or g.timestamp(writer["CreatedAt"]) <= g.timestamp(review["CreatedAt"]):
                    continue
                for change in writer["Payload"].get("Changes", []):
                    if references & set(change.get("EvidenceRefs", [])):
                        changes.append({"WriterEventRef": writer["_path"], "ActionId": change["ActionId"], "Path": change["Path"],
                                        "SelfValidationEntryCount": len(writer["Payload"].get("SelfValidation", []))})
            result.append({"ReviewEventRef": review["_path"], "FindingId": ident, "AffectedWorkPackages": affected,
                           "ExplicitChanges": changes, "Closure": "RUNTIME_VERIFICATION_REQUIRED" if changes else "NO_EXPLICIT_REPAIR_LINK"})
    return result


def _query_status(repo, requirement_id):
    g.require(g.valid_requirement_id(requirement_id), "RequirementId")
    report = {"ResultType": "GOVERNANCE_STATUS_QUERY", "StatusQueryVersion": "v1.0", "ReadOnly": True,
              "RequirementId": requirement_id, "QueryStatus": "UNDETERMINED", "CurrentStage": "UNDETERMINED",
              "CurrentCandidate": None, "QualityPassed": False, "HumanAccepted": False,
              "FactsWritten": False, "RolesDispatched": False, "Missing": [], "BlockingItems": [],
              "NextActions": [], "Problems": [], "Observations": {}, "Sources": {}}
    try:
        repo = environment.local_absolute(str(Path(repo).absolute()))
        g.safe_path(repo, repo)
        require(Path(environment.discovery_git(repo, "rev-parse", "--show-toplevel").strip()).resolve() == repo.resolve(), "WORKTREE_ROOT_CONFLICT")
        require(not any(key.upper().startswith("GIT_") and key.upper() != "GIT_PAGER" for key in os.environ), "GIT_OVERRIDES")
        root = g.safe_path(repo, repo / "docs/requirements" / requirement_id)
        before = sources(repo, root)
        report["Sources"] = before
        require(all((root / name).is_file() for name in ("control-profile.json", "acceptance-plan.json", "开发上下文包.md")), "PLANNING_INPUTS_INCOMPLETE")
        checked = g.validate_plan(root / "control-profile.json", root / "acceptance-plan.json")
        profile = g.load_contract(root / "control-profile.json", "ControlProfile")
        plan = g.load_contract(root / "acceptance-plan.json", "AcceptancePlan")
        text = (root / "开发上下文包.md").read_text(encoding="utf-8-sig")
        version = g.artifact_version(text)
        report["ContextPackVersion"], report["GovernanceProfile"] = version, profile["SelectedProfile"]
        events = g.load_events(root)
        require(all(event["EventType"] in SUPPORTED for event in events.values()), "UNSUPPORTED_EVENT_TYPE")
        for event in events.values():
            require(Path(event["_path"]).parts[:2] == ("facts", FOLDERS[event["EventType"]]), "EVENT_DIRECTORY_CONFLICT")
            if event["EventType"] == "GATE_DECISION":
                gate_name = event["Payload"].get("Gate")
                require(gate_name in ("G1", "G2"), "UNKNOWN_GATE")
                require(event["Payload"].get("Decision") in (("CONFIRMED", "REJECTED") if gate_name == "G1" else ("FROZEN", "REJECTED")), "INVALID_GATE_DECISION")
        active = [event for event in events.values() if event["ContextPackVersion"] == version]
        projection = root / "rules-projection.json"
        rule_projection = None
        if projection.is_file():
            from governance_rules import verify_composition_sources
            rule_projection = g.load_contract(projection, "RuleProjection")
            rule_check = verify_composition_sources(repo, rule_projection)
            report["Observations"]["RuleSourcePinsVerified"] = rule_check["Valid"]
            report["Observations"]["RuleRequirementAuthorizationVerified"] = False
            report["RuleDependencySources"] = rule_projection["Sources"]
        gates = [event for event in active if event["EventType"] == "GATE_DECISION" and event["Payload"].get("Gate") == "G2"]
        require(len(gates) <= 1, "AMBIGUOUS_G2")

        def stage(name, action, owner="RUNTIME", **details):
            report["QueryStatus"], report["CurrentStage"] = "VERIFIED_SUPPORTED_STATE", name
            report["NextActions"] = [{"Action": action, "Owner": owner, **details}]

        if not gates:
            report["Missing"].append("G2_DECISION")
            stage("WAITING_FOR_G2", "FORM_G2_DECISION", "HUMAN" if checked["RequiredG2DecisionMode"] == "HUMAN" else "RUNTIME")
        else:
            gate = gates[0]
            payload = gate["Payload"]
            require(payload.get("DecisionMode") in ("HUMAN", "AUTOMATIC") and bool(payload.get("BasisRefs")), "G2_DECISION_EVIDENCE_MISSING")
            if payload["DecisionMode"] == "HUMAN":
                g.human_approval_reference(payload.get("ApprovalRef"))
            else:
                require(checked["RequiredG2DecisionMode"] == "AUTOMATIC_ALLOWED" and payload.get("ApprovalRef") is None
                        and payload.get("Decision") == "FROZEN", "G2_AUTOMATIC_CONFLICT")
            require(payload.get("ArtifactVersion") == version and payload.get("ArtifactRef") == "docs/requirements/" + requirement_id + "/开发上下文包.md", "G2_ARTIFACT_CONFLICT")
            require(payload.get("Decision") in ("FROZEN", "REJECTED"), "INVALID_G2_DECISION")
            if payload["Decision"] == "REJECTED":
                require(local.metadata(text, "产物当前状态") == "DRAFT", "REJECTED_G2_CONTEXT_CONFLICT")
                stage("G2_REJECTED", "REVIEW_G2_REJECTION", "HUMAN")
            else:
                require(payload.get("BlockingItems") == [], "FROZEN_G2_HAS_BLOCKERS")
                require(local.metadata(text, "产物当前状态") == "FROZEN", "CONTEXT_NOT_FROZEN")
                require(all(g.timestamp(event["CreatedAt"]) >= g.timestamp(gate["CreatedAt"])
                            for event in active if event["EventType"] != "GATE_DECISION"), "EXECUTION_PRECEDES_G2")
                _execution_state(repo, root, text, plan, profile, events, active, report, stage)
        if rule_projection is not None:
            verify_composition_sources(repo, rule_projection)
        require(sources(repo, root) == before, "SOURCES_CHANGED_DURING_QUERY")
    except Uncertain as error:
        report.update(QueryStatus="UNDETERMINED", CurrentStage="UNDETERMINED", HumanAccepted=None)
        report["Problems"].append(str(error))
        report["NextActions"] = [{"Action": "RESOLVE_INPUT_OR_UNSUPPORTED_FACTS", "Owner": "RUNTIME"}]
    except (g.Invalid, OSError, UnicodeError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired):
        report.update(QueryStatus="UNDETERMINED", CurrentStage="UNDETERMINED", HumanAccepted=None)
        report["Problems"].append("SOURCE_OR_FACT_VALIDATION_FAILED")
        report["NextActions"] = [{"Action": "CHECK_OR_RESOLVE_SOURCE_FACTS", "Owner": "RUNTIME"}]
    if report["QueryStatus"] == "UNDETERMINED":
        report["ObservationsAreUnverified"] = True
    try:
        g.sensitive(report)
    except g.Invalid:
        return {"ResultType": "GOVERNANCE_STATUS_QUERY", "StatusQueryVersion": "v1.0", "ReadOnly": True,
                "RequirementId": requirement_id, "QueryStatus": "UNDETERMINED", "CurrentStage": "UNDETERMINED",
                "QualityPassed": False, "HumanAccepted": None, "FactsWritten": False, "RolesDispatched": False,
                "Problems": ["SENSITIVE_OUTPUT_REJECTED"], "Sources": {}, "Observations": {}}
    return report


def query_status(repo, requirement_id):
    """Tracking is an explicitly adopted execution overlay, not a new gate result."""
    g.require(g.valid_requirement_id(requirement_id), "RequirementId")
    root = Path(repo).absolute() / "docs/requirements" / requirement_id
    if not (root / "tracking.json").exists():
        return _query_status(repo, requirement_id)
    from governance_requirements import execution_state, fact_pins, early_stage
    try:
        state = execution_state(repo, requirement_id)
        label = None
        if state["Closed"]:
            label = "ITERATION_CLOSED"
        elif state["Mode"] == "MANUAL":
            label = "MANUAL_TRACKING"
        elif state["ResumeEvent"]:
            current = {v for n, v in fact_pins(repo, requirement_id).items() if n.startswith("facts/candidates/")}
            previous = set(state["ResumeEvent"]["Payload"]["BeforeCandidates"])
            if previous and (not current or current <= previous):
                label = "FRESH_CANDIDATE_REQUIRED_AFTER_RESUME"
        if label is None and not (root / "control-profile.json").exists():
            label = early_stage(repo, requirement_id)
        if label:
            return {"ResultType": "GOVERNANCE_STATUS_QUERY", "StatusQueryVersion": "v1.0", "ReadOnly": True,
                    "RequirementId": requirement_id, "QueryStatus": "VERIFIED_SUPPORTED_STATE", "CurrentStage": label,
                    "ManagementMode": state["Mode"], "QualityPassed": False, "HumanAccepted": False, "FactsWritten": False,
                    "RolesDispatched": False, "Sources": {}, "Problems": [], "Observations": {}, "NextActions": []}
        return _query_status(repo, requirement_id)
    except (ValueError, OSError, KeyError, TypeError):
        return {"ResultType": "GOVERNANCE_STATUS_QUERY", "StatusQueryVersion": "v1.0", "ReadOnly": True,
                "RequirementId": requirement_id, "QueryStatus": "UNDETERMINED", "CurrentStage": "UNDETERMINED",
                "QualityPassed": False, "HumanAccepted": None, "FactsWritten": False, "RolesDispatched": False,
                "Sources": {}, "Problems": ["INVALID_TRACKING_STATE"], "Observations": {}, "NextActions": []}


def _execution_state(repo, root, text, plan, profile, events, active, report, stage):
    workpackages = planned_workpackages(text)
    require(len(workpackages) == profile["ExecutionBudget"]["WorkPackageCount"], "WORKPACKAGE_COUNT_CONFLICT")
    writers = [event for event in active if event["EventType"] == "WRITER_HANDOFF"]
    if writers:
        g.render(root, "changes")
    require(all(event["Payload"].get("WorkPackageId") in workpackages for event in writers), "UNKNOWN_WRITER_WORKPACKAGE")
    report["Observations"]["RepairLinks"] = repair_links(active, workpackages)
    completed = []
    for ident in sorted(workpackages):
        event = latest([event for event in writers if event["Payload"].get("WorkPackageId") == ident], "WRITER")
        if event is None:
            report["Missing"].append({"Kind": "WRITER_HANDOFF", "WorkPackageId": ident})
            continue
        require(event["Payload"].get("Status") in ("COMPLETED", "BLOCKED"), "UNSUPPORTED_WRITER_STATUS")
        for key in ("SelfValidation", "NotValidated", "Failures", "ResidualRisks", "BoundaryIssues", "BlockingItems", "RequiredConfirmations"):
            g.array(event["Payload"].get(key), "writer " + key)
        if event["Payload"]["Status"] == "BLOCKED":
            blocks = g.array(event["Payload"].get("BlockingItems"), "writer blockers")
            require(bool(blocks), "MISSING_WRITER_BLOCKERS")
            report["BlockingItems"].extend(blocks)
        else:
            require(not event["Payload"]["BlockingItems"], "COMPLETED_WRITER_HAS_BLOCKERS")
            completed.append(event)
    if report["BlockingItems"]:
        stage("IMPLEMENTATION_BLOCKED", "RESOLVE_WRITER_BLOCKERS", "WRITER")
        return
    if report["Missing"]:
        stage("IMPLEMENTATION_INCOMPLETE", "COMPLETE_ASSIGNED_WORKPACKAGES", "WRITER")
        return
    candidate = latest([event for event in active if event["EventType"] == "CANDIDATE_STATE"], "CANDIDATE")
    if candidate is None or any(g.timestamp(writer["CreatedAt"]) > g.timestamp(candidate["CreatedAt"]) for writer in completed):
        report["Missing"].append("CURRENT_CANDIDATE")
        stage("CANDIDATE_REQUIRED", "CAPTURE_VERIFY_AND_REGISTER_CANDIDATE")
        return
    require(candidate["CandidateRef"] in candidate_identity(candidate), "CANDIDATE_IDENTITY_CONFLICT")
    payload = candidate["Payload"]
    require(payload.get("SnapshotFormatVersion") == "v1.0" and payload.get("DraftOnly") is not True
            and not payload.get("LocalOnly") and not payload.get("CannotArchiveAsFact"), "UNSUPPORTED_OR_DRAFT_CANDIDATE")
    snapshot = environment.relative_path(repo, payload.get("CandidateSnapshotRef"))
    from governance_candidates import verify_snapshot
    result = verify_snapshot(snapshot, repo=repo, requirement_id=root.name, context_version=report["ContextPackVersion"],
                             repair_lineage_ref=candidate["RepairLineageRef"], expected_snapshot_digest=payload.get("CandidateSnapshotDigest"),
                             verify_worktree=True)
    require(result.get("Valid") is True and not result.get("LocalOnly") and not result.get("CannotArchiveAsFact"), "CANDIDATE_SNAPSHOT_INVALID")
    require(result.get("GovernanceInputsChecked") is True, "CANDIDATE_GOVERNANCE_INPUTS_UNBOUND")
    require(payload.get("BaseCommitSHA") == json.loads(snapshot.read_text(encoding="utf-8"))["BaseCommitSHA"], "CANDIDATE_BASE_CONFLICT")
    used_writers = [find_event(events, ref, "WRITER_HANDOFF", candidate) for ref in payload.get("WriterHandoffRefs", [])]
    require({event["EventId"] for event in used_writers} == {event["EventId"] for event in completed}, "CANDIDATE_WRITER_COVERAGE")
    report["CurrentCandidate"] = {"CandidateRef": candidate["CandidateRef"], "SnapshotDigest": result["SnapshotDigest"], "SnapshotVerified": True}
    current = [event for event in active if event["CandidateRef"] in candidate_identity(candidate)]
    controls = {item["ControlId"]: item for item in plan["Controls"]}
    if profile["SelectedProfile"] == "LEAN":
        report["Observations"]["IndependentQARequired"] = False
        report["Observations"]["IndependentReviewerRequired"] = False
        report["Observations"]["WriterSelfValidationRefs"] = [event["_path"] for event in completed]
        stage("LEAN_SELF_VALIDATION_REVIEW", "CHECK_FROZEN_WRITER_CONTROLS_BEFORE_BA")
        return
    expected = validation_map(text, controls, "QA")
    observed_qa = [event for event in current if event["EventType"] == "QA_RESULT" and event["Payload"].get("CountDisposition") == "ACTIVE"]
    observed_review = [event for event in current if event["EventType"] == "REVIEW_RESULT" and event["Payload"].get("CountDisposition") == "ACTIVE"]
    for kind, view in (("QA_RESULT", "qa-index"), ("REVIEW_RESULT", "review-index")):
        if any(event["EventType"] == kind for event in events.values()):
            g.render(root, view)
    qa, review = latest(observed_qa, "QA"), latest(observed_review, "REVIEW")
    if qa is not None:
        require(g.timestamp(qa["Payload"].get("StartedAt")) >= g.timestamp(candidate["CreatedAt"]), "QA_EXECUTION_PRECEDES_CANDIDATE")
        g.string(qa["Payload"].get("ValidationPlanRef"), "QA validation plan")
        require(g.reference_path(repo, root / "acceptance-plan.json", qa["Payload"].get("ValidationPlanRef")).resolve()
                == (root / "acceptance-plan.json").resolve(), "QA_PLAN_REFERENCE_CONFLICT")
        require(not qa["Payload"].get("AdjustmentRef") and not qa["Payload"].get("AdjustmentDigest"), "ADJUSTED_QUALITY_REQUIRES_RUNTIME")
        entries = qa["Payload"].get("EffectiveValidationSet", []) + qa["Payload"].get("NotExecuted", [])
        require({item["ValidationId"] for item in entries} == set(expected), "QA_EFFECTIVE_SET_INCOMPLETE")
        for item in entries:
            require(item["GateClass"] == expected[item["ValidationId"]]["GateClass"], "QA_GATE_CLASS_CONFLICT")
            require(item.get("ControlId") == expected[item["ValidationId"]]["ControlId"], "QA_CONTROL_CONFLICT")
            definition = expected[item["ValidationId"]]
            require(item.get("GateTarget") == definition["GateTarget"], "QA_GATE_TARGET_CONFLICT")
            require(isinstance(item.get("AffectedWorkPackages"), list) and sorted(item["AffectedWorkPackages"]) == definition["AffectedWorkPackages"], "QA_WORKPACKAGE_CONFLICT")
            if item in qa["Payload"].get("EffectiveValidationSet", []):
                require(item.get("CommandOrMethod") == definition["Method"], "QA_METHOD_CONFLICT")
        report["Observations"]["QAStatus"] = qa["Payload"]["Status"]
        report["Observations"]["QARound"] = qa["Payload"]["QARound"]
        report["BlockingItems"].extend(qa["Payload"].get("BlockingItems", []))
    if review is not None:
        require(g.timestamp(review["Payload"].get("StartedAt")) >= g.timestamp(candidate["CreatedAt"]), "REVIEW_PRECEDES_CANDIDATE")
        report["Observations"]["ReviewerDecision"] = review["Payload"]["Decision"]
        report["Observations"]["OpenFindings"] = review["Payload"]["Findings"]
    if (qa is not None and qa["Payload"]["Status"] == "FAIL") or (review is not None and review["Payload"]["Decision"] == "CHANGE"):
        stage("QUALITY_CHANGES_RECORDED", "VALIDATE_EXISTING_ROUTING_AND_REPAIR_MAPPING")
    elif qa is not None and qa["Payload"]["Status"] == "BLOCKED":
        stage("TEST_PREREQUISITE_WAIT", "WAIT_FOR_NEW_RELEASE_EVIDENCE")
    elif qa is None:
        ready = latest([event for event in current if event["EventType"] == "TEST_READINESS"], "READINESS")
        if ready is not None:
            g.string(ready["Payload"].get("ValidationPlanRef"), "readiness validation plan")
            require(g.reference_path(repo, root / "acceptance-plan.json", ready["Payload"].get("ValidationPlanRef")).resolve()
                    == (root / "acceptance-plan.json").resolve(), "READINESS_PLAN_REFERENCE_CONFLICT")
            entries = readiness(ready, expected, events, candidate, workpackages)
            report["Observations"]["ReadyValidationIds"] = [item["ValidationId"] for item in entries if item["Readiness"] == "READY"]
            if not report["Observations"]["ReadyValidationIds"] and any(item["GateClass"] == "BLOCKING" for item in entries):
                stage("TEST_PREREQUISITE_WAIT", "WAIT_FOR_NEW_RELEASE_EVIDENCE")
                return
        report["Missing"].append("QA_RESULT")
        stage("QA_RESULT_REQUIRED", "CHECK_READINESS_AND_DISPATCH_QA")
    elif review is None:
        report["Missing"].append("REVIEW_RESULT")
        stage("REVIEW_RESULT_REQUIRED", "COMPLETE_INDEPENDENT_REVIEW")
    else:
        stage("QUALITY_OBSERVATIONS_COMPLETE", "VALIDATE_AND_PERSIST_QUALITY_JOIN")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.error = lambda _message: parser.exit(2, "Invalid status query arguments.\n")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--requirement-id", required=True)
    args = parser.parse_args(argv)
    try:
        report = query_status(args.repo, args.requirement_id)
    except g.Invalid:
        print("Unable to query the selected requirement.", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["QueryStatus"] == "VERIFIED_SUPPORTED_STATE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
