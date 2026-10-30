"""One project entry for new work, evidence-based continuation and read-only discovery.

This router selects context, not gate decisions. Requirement operations still run
through their own pinned release, including after a project's default upgrade.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True
import governance_project as project
import governance_requirements as tracking
import governance_tools as g

VERSION = "v1.0"
MAX_REQUIREMENTS = 128


def invoke(repo, tool, *arguments, allowed=(0,)):
    result = subprocess.run(
        [sys.executable, "-B", str(repo / ".governance/run.py"), tool, *arguments],
        cwd=repo, capture_output=True, timeout=120,
        env={**{k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}, "PYTHONUTF8": "1"})
    g.require(result.returncode in allowed and len(result.stdout) <= 4 * 1024 * 1024,
              "bound requirement operation failed; inspect its original release and inputs")
    return json.loads(result.stdout.decode("utf-8"), object_pairs_hook=g.unique_object)


def branch_state(repo):
    result = tracking.git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", allowed=(0, 1))
    branch = result.stdout.decode("utf-8").strip() if result.returncode == 0 else None
    if branch:
        tracking.text(branch, limit=256)
    commit = tracking.git(repo, "rev-parse", "--verify", "HEAD").stdout.decode("ascii").strip()
    return {"Branch": branch, "Commit": commit}


def local_path(repo, name):
    """Checking read-only context must not create the runtime directory."""
    config = project.project_config(repo)
    g.require(config.get("LocalRuntimeDirectory") == ".governance/local", "startup requires the adopted local runtime directory")
    tracking.git(repo, "check-ignore", "--quiet", "--no-index", name)
    g.require(not tracking.git(repo, "ls-files", "--", ".governance/local").stdout.strip(), "startup context must be untracked")
    return project.path_at(repo, name)


def execution_policy(repo, branch, config):
    startup = config.get("Startup", {})
    g.require(isinstance(startup, dict) and set(startup) <= {"ProjectName", "DirectDevelopment"}, "startup configuration")
    name = startup.get("ProjectName", repo.name)
    tracking.text(name, limit=200)
    policy = {"Mode": "GOVERNED", "Branch": branch["Branch"], "Reason": "ordinary project rules", "PolicyRef": None}
    direct = startup.get("DirectDevelopment")
    if direct is None:
        return name, policy
    g.require(isinstance(direct, dict) and {"Branches", "PolicyRef", "Instruction"} <= set(direct)
              and set(direct) <= {"Branches", "PolicyRef", "Instruction", "IsolatedOriginRef"}, "direct-development policy")
    g.unique_strings(direct["Branches"], "explicit direct-development branches")
    g.require(0 < len(direct["Branches"]) <= 32, "bounded explicit branches")
    for item in direct["Branches"]:
        tracking.git(repo, "check-ref-format", "--branch", item)
    g.require(project.path_at(repo, direct["PolicyRef"]).is_file(), "existing project policy source")
    tracking.text(direct["Instruction"])
    applies = branch["Branch"] in direct["Branches"]
    reason = "explicit project branch policy"
    if "IsolatedOriginRef" in direct:
        g.require(direct["IsolatedOriginRef"] == ".governance/local/worktree-origin.json", "isolated origin is local-only")
        origin = local_path(repo, direct["IsolatedOriginRef"])
        if branch["Branch"] is None and origin.is_file():
            value = project.read_json(repo, direct["IsolatedOriginRef"])
            applies = (value.get("Purpose") == "ISOLATED_ADAPTER_TEST"
                       and value.get("BaseCommit") == branch["Commit"]
                       and value.get("SourceRef") in {"refs/heads/" + b for b in direct["Branches"]})
            reason = "exact-commit isolated test creation record"
    if applies:
        policy.update(Mode="MANUAL", Reason=reason, PolicyRef=direct["PolicyRef"], Instruction=direct["Instruction"])
    return name, policy


def registration(repo, lock, ident):
    pin = project.binding(repo, lock, ident)
    state = invoke(repo, "governance_requirements", "inspect", "--repo", str(repo), "--requirement-id", ident)
    g.require(isinstance(state, dict), "current-batch registered requirement")
    return pin, state


def context_name(branch):
    scope = branch["Branch"] or "DETACHED:" + branch["Commit"]
    return ".governance/local/startup/contexts/" + tracking.sha(scope.encode("utf-8")) + ".json"


@contextmanager
def startup_lock(repo):
    target = local_path(repo, ".governance/local/startup/operation.lock")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("xb") as stream:
            stream.write(b"startup operation\n")
    except FileExistsError:
        raise g.Invalid("Startup operation in progress; inspect the local operation lock") from None
    try:
        yield
    finally:
        target.unlink(missing_ok=True)


def register_new(repo, branch, batch, title, instruction, ident, request_key):
    """Write-ahead ID selection makes retries safe even after a partial startup."""
    if ident is not None:
        return invoke(repo, "governance_requirements", "init", "--repo", str(repo), "--title", title,
                      "--instruction", instruction, "--requirement-id", ident)
    g.require(isinstance(request_key, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", request_key), "stable new-request key")
    request_digest = tracking.sha(tracking.canonical({"BatchId": batch["BatchId"], "Worktree": str(repo.resolve()), "Scope": context_name(branch),
                                                    "Title": title, "Instruction": instruction}))
    target = local_path(repo, ".governance/local/startup/requests/" + tracking.sha(request_key.encode("utf-8")) + ".json")
    with startup_lock(repo):
        if target.is_file():
            value = project.read_json(repo, target.relative_to(repo).as_posix())
            g.exact_object(value, "new-request receipt", ("StartupVersion", "RequestDigest", "RequirementId"))
            g.require(value["StartupVersion"] == VERSION and value["RequestDigest"] == request_digest
                      and g.valid_requirement_id(value["RequirementId"]), "new-request key reused with different inputs")
            ident = value["RequirementId"]
        else:
            ident = tracking.reserve_tracking_id(repo)
            tracking.write_new(target, {"StartupVersion": VERSION, "RequestDigest": request_digest, "RequirementId": ident})
        return invoke(repo, "governance_requirements", "init", "--repo", str(repo), "--title", title,
                      "--instruction", instruction, "--requirement-id", ident)


def candidates(repo, lock, batch):
    directory = project.path_at(repo, "docs/requirements")
    folders = sorted(directory.iterdir()) if directory.exists() else []
    selected = [p for p in folders if g.valid_requirement_id(p.name) and p.name not in batch["ExcludedRequirementIds"]
                and project.path_at(repo, "docs/requirements/" + p.name + "/tracking.json").is_file()]
    g.require(len(selected) <= MAX_REQUIREMENTS, "startup requirement discovery limit")
    result = {}
    for folder in selected:
        result[folder.name] = registration(repo, lock, folder.name)
    return result


def association(repo, branch, batch, entries):
    target = local_path(repo, context_name(branch))
    if not target.is_file():
        return None, None
    try:
        value = project.read_json(repo, target.relative_to(repo).as_posix())
        g.exact_object(value, "startup association", ("StartupVersion", "WorktreeDigest", "BatchId", "Branch", "DetachedCommit", "RequirementId", "BindingDigest"))
        identity = hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()
        g.require(value["StartupVersion"] == VERSION and value["WorktreeDigest"] == identity
                  and value["BatchId"] == batch["BatchId"] and value["Branch"] == branch["Branch"]
                  and value["DetachedCommit"] == (branch["Commit"] if branch["Branch"] is None else None), "startup association scope")
        ident = value["RequirementId"]
        g.require(ident in entries and value["BindingDigest"] == tracking.sha(project.read_bytes(repo, "docs/requirements/" + ident + "/plugin-binding.json")), "startup association binding")
        g.require(not entries[ident][1]["Current"]["Closed"], "association iteration remains active")
        return ident, None
    except (ValueError, OSError, KeyError, TypeError):
        return None, "STALE_OR_INVALID_LOCAL_ASSOCIATION"


def remember(repo, branch, batch, ident):
    target = local_path(repo, context_name(branch))
    value = {"StartupVersion": VERSION, "WorktreeDigest": hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest(),
             "BatchId": batch["BatchId"], "Branch": branch["Branch"],
             "DetachedCommit": branch["Commit"] if branch["Branch"] is None else None,
             "RequirementId": ident, "BindingDigest": tracking.sha(project.read_bytes(repo, "docs/requirements/" + ident + "/plugin-binding.json"))}
    with tracking.mutation(repo):
        project.write_atomic(target, tracking.canonical(value) + b"\n")


def next_actions(status, state):
    if state["Current"]["Closed"]:
        return [{"Action": "KEEP_CLOSED_OR_EXPLICITLY_START_NEXT_ITERATION", "Owner": "HUMAN"}]
    if status.get("QueryStatus") != "VERIFIED_SUPPORTED_STATE":
        return status.get("NextActions") or [{"Action": "CHECK_BOUND_SOURCE_FACTS", "Owner": "RUNTIME"}]
    stages = {
        "MANUAL_TRACKING": ("CONTINUE_ORDINARY_DEVELOPMENT_AND_MINIMAL_TRACKING", "MAIN_AGENT"),
        "WAITING_FOR_G1": ("CLARIFY_BASELINE_AND_OBTAIN_EXPLICIT_G1", "MAIN_AGENT_AND_HUMAN"),
        "G1_REJECTED": ("REVISE_BASELINE_FOR_HUMAN_CONFIRMATION", "MAIN_AGENT_AND_HUMAN"),
        "PLANNING": ("PREPARE_PLANNING_FROM_CONFIRMED_BASELINE", "RUNTIME"),
        "FRESH_CANDIDATE_REQUIRED_AFTER_RESUME": ("RECHECK_PLAN_AND_FORM_FRESH_CANDIDATE", "RUNTIME"),
    }
    if status.get("CurrentStage") in stages:
        action, owner = stages[status["CurrentStage"]]
        return [{"Action": action, "Owner": owner}]
    return status.get("NextActions") or [{"Action": "CHECK_BOUND_STAGE_RULES", "Owner": "RUNTIME"}]


def start(repo, intent="continue", requirement_id=None, title=None, instruction=None, *, request_key=None, fetch=True):
    repo = tracking.repository(repo)
    g.require(intent in {"new", "continue", "query"}, "explicit startup intent")
    g.require(requirement_id is None or g.valid_requirement_id(requirement_id), "startup RequirementId")
    g.require(intent == "new" or (title is None and instruction is None and request_key is None), "new requirement text only belongs to new intent")
    lock, config = project.load_lock(repo), project.project_config(repo)
    g.require(lock is not None and config is not None, "adopted project")
    project.verify_release(repo, lock, lock["ActivePackageDigest"])
    batch = tracking.batch(repo)
    branch = branch_state(repo)
    name, policy = execution_policy(repo, branch, config)
    dirty = tracking.git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=normal").stdout
    report = {"ResultType": "GOVERNANCE_START", "StartupVersion": VERSION, "Intent": intent, "ReadOnly": True,
              "Project": name, "Branch": branch["Branch"], "HeadCommit": branch["Commit"],
              "WorkingTreeDirty": bool(dirty), "ProjectPolicy": policy, "Outcome": "INPUT_REQUIRED",
              "RequirementId": None, "Requirement": None, "SelectionBasis": None, "Candidates": [],
              "NextActions": [], "Problems": [], "Writes": [], "FactsWritten": False, "GatesChanged": False,
              "RolesDispatched": False, "TestsStarted": False, "QualityPassed": False, "HumanAccepted": False,
              "ExecutionAuthorized": False}
    if intent == "new":
        tracking.text(title, limit=200); tracking.text(instruction)
        g.require("\n" not in title and "\r" not in title, "single-line requirement title")
        if requirement_id is None:
            developer = tracking.git(repo, "config", "--local", "--get", "governance.developerId", allowed=(0, 1))
            if developer.returncode != 0:
                report["NextActions"] = [{"Action": "CONFIGURE_LOCAL_DEVELOPER_ID_ONCE", "Owner": "HUMAN"}]
                return report
            g.developer_id(developer.stdout.decode("utf-8").strip())
            if request_key is None:
                report["NextActions"] = [{"Action": "PROVIDE_STABLE_NEW_REQUEST_KEY", "Owner": "MAIN_AGENT"}]
                return report
        result = register_new(repo, branch, batch, title, instruction, requirement_id, request_key)
        requirement_id = result["RequirementId"]
        report["SelectionBasis"] = "EXPLICIT_NEW_REQUEST"
        if result["Created"]:
            report["Writes"].append("REQUIREMENT_REGISTRATION")
        pin, state = registration(repo, lock, requirement_id)
    else:
        entries = candidates(repo, lock, batch) if requirement_id is None else {}
        report["Candidates"] = [{"RequirementId": ident, "Title": state["Current"]["Title"], "Mode": state["Mode"],
                                  "Iteration": state["Current"]["Iteration"]}
                                 for ident, (_, state) in entries.items() if not state["Current"]["Closed"]]
        if requirement_id is not None:
            g.require(requirement_id not in batch["ExcludedRequirementIds"], "historical requirement keeps its original entry")
            pin, state = entries[requirement_id] if requirement_id in entries else registration(repo, lock, requirement_id)
            report["SelectionBasis"] = "EXPLICIT_REQUIREMENT_ID"
        else:
            linked, warning = association(repo, branch, batch, entries)
            if warning:
                report["Problems"].append(warning)
            branch_ids = [ident for ident in entries if not entries[ident][1]["Current"]["Closed"] and branch["Branch"]
                          and re.search(r"(?<![A-Za-z0-9])" + re.escape(ident) + r"(?![A-Za-z0-9])", branch["Branch"])]
            if len(branch_ids) > 1 or (linked and branch_ids and linked != branch_ids[0]):
                report["Problems"].append("BRANCH_AND_LOCAL_ASSOCIATION_CONFLICT")
            elif branch_ids or linked:
                requirement_id = branch_ids[0] if branch_ids else linked
                report["SelectionBasis"] = "BRANCH_REQUIREMENT_ID" if branch_ids else "LOCAL_WORKTREE_ASSOCIATION"
                pin, state = entries[requirement_id]
            if requirement_id is None:
                report["Outcome"] = "SELECTION_REQUIRED" if report["Candidates"] else "NO_ACTIVE_REQUIREMENT"
                report["NextActions"] = [{"Action": "SELECT_REQUIREMENT" if report["Candidates"] else "PROVIDE_EXPLICIT_NEW_IMPLEMENTATION_REQUEST", "Owner": "HUMAN"}]
                return report
    g.require(branch_state(repo) == branch, "branch changed during startup; inspect the recorded operation before retrying")
    # An existing explicit resumption wins over a branch's default direct mode.
    if (intent != "query" and not state["Current"]["Closed"] and policy["Mode"] == "MANUAL"
            and state["Mode"] != "MANUAL" and not any(e["Action"] == "RESUME" for e in state["Events"])):
        invoke(repo, "governance_requirements", "exit", "--repo", str(repo), "--requirement-id", requirement_id,
               "--instruction", policy["Instruction"])
        report["Writes"].append("PROJECT_DIRECT_DEVELOPMENT_INSTRUCTION")
        pin, state = registration(repo, lock, requirement_id)
    status = invoke(repo, "governance_status", "--repo", str(repo), "--requirement-id", requirement_id, allowed=(0, 1))
    report["RequirementId"] = requirement_id
    report["Requirement"] = {"RequirementId": requirement_id, "Title": state["Current"]["Title"],
                             "Mode": state["Mode"], "Iteration": state["Current"]["Iteration"],
                             "Closed": state["Current"]["Closed"], "PluginVersion": pin["PluginVersion"],
                             "PackageDigest": pin["PackageDigest"], "CurrentStage": "ITERATION_CLOSED" if state["Current"]["Closed"] else status["CurrentStage"],
                             "ProcessRef": "docs/requirements/" + requirement_id + "/" + (
                                 "开发记录.md" if state["Current"]["Iteration"] else state["Current"]["BaselineRef"])}
    report["Status"] = status
    report["Problems"].extend(status.get("Problems", []))
    report["NextActions"] = next_actions(status, state)
    report["Outcome"] = ("ITERATION_CLOSED" if state["Current"]["Closed"] else
                         "BLOCKED" if status.get("QueryStatus") != "VERIFIED_SUPPORTED_STATE" else "READY")
    if intent != "query" and not state["Current"]["Closed"]:
        remember(repo, branch, batch, requirement_id)
        report["Writes"].append("LOCAL_WORKTREE_ASSOCIATION")
        try:
            args = [] if fetch else ["--no-fetch"]
            overview = invoke(repo, "governance_overview", "refresh", "--repo", str(repo), *args)
            report["Overview"] = {"TableRef": overview["TableRef"], "RemoteKnowledge": overview["RemoteKnowledge"], "Problems": overview["Problems"]}
            report["Writes"].append("OVERVIEW_PROJECTION")
        except (ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired):
            report["Problems"].append("OVERVIEW_REFRESH_PENDING")
    report["ReadOnly"] = not report["Writes"]
    return report


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.error = lambda _m: parser.exit(2, "Invalid startup arguments.\n")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--intent", choices=("new", "continue", "query"), default="continue")
    parser.add_argument("--requirement-id")
    parser.add_argument("--title")
    parser.add_argument("--instruction")
    parser.add_argument("--request-key")
    parser.add_argument("--no-fetch", action="store_true")
    a = parser.parse_args(argv)
    try:
        result = start(a.repo, a.intent, a.requirement_id, a.title, a.instruction, request_key=a.request_key, fetch=not a.no_fetch)
        g.sensitive(result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result["Outcome"] == "BLOCKED" else 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired):
        print("Startup rejected; inspect the project lock, tracking batch, explicit selection and bound facts.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
