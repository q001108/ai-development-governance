"""Bounded local test preparation; project commands run only with explicit execution.

This helper produces execution observations, never G2, TEST_READINESS or QA facts.
The existing runtime remains responsible for candidate checks, role dispatch and recovery.
"""

import argparse
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid

sys.dont_write_bytecode = True
import governance_environment as environment
import governance_tools as g


CONFIG_NAME = "governance.local-tests.json"
ID = r"[A-Z][A-Z0-9_]{0,47}"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path):
    g.require(path.stat().st_size <= 256 * 1024, "local test configuration size")
    return g.load_contract(path, "LocalTests")


def plain(value):
    return value.strip().strip("`").strip()


def table(text, section, width):
    """Only the existing numbered table is accepted; never infer prose or examples."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"(?ms)^```[^\n]*\n.*?^```[^\n]*$", "", text)
    matches = list(re.finditer(r"(?m)^#### " + re.escape(section) + r"\b[^\n]*$", text))
    g.require(len(matches) == 1, "unique validation table section")
    body = text[matches[0].end():]
    body = re.split(r"(?m)^#{1,4} ", body, maxsplit=1)[0]
    rows = []
    for line in body.splitlines():
        if line.strip().startswith("|"):
            cells = [plain(cell) for cell in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
            g.require(len(cells) == width, "validation table width")
            if re.fullmatch(r"V-\d+", cells[0]):
                rows.append(cells)
    g.require(len({row[0] for row in rows}) == len(rows), "unique validation IDs")
    return {row[0]: row for row in rows}


def metadata(text, label):
    matches = re.findall(r"(?m)^-\s*\*\*" + re.escape(label) + r"[：:]\*\*\s*([^\n]+)", text)
    g.require(len(matches) == 1, "context metadata")
    return plain(matches[0])


def argv(value):
    g.require(isinstance(value, list) and 1 <= len(value) <= 64, "argument array")
    for part in value:
        environment.reference(part, "argument")
        g.require(len(part) <= 4096, "argument length")
    # Explicit argument arrays avoid shell parsing; project-approved wrappers own their semantics.
    g.require(value[0] == "{python}" or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", value[0]), "executable name")
    g.require(value[0].lower() not in {"cmd", "cmd.exe", "sh", "bash", "wsl", "sudo"}, "shell executable")
    g.sensitive(value)
    return value


def timeout(value):
    g.require(type(value) is int and 1 <= value <= 300, "timeout (1-300 seconds)")
    return value


def executable(name):
    if name == "{python}":
        return sys.executable
    suffixes = [""]
    if os.name == "nt" and not Path(name).suffix:
        suffixes += [suffix for suffix in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
                     if re.fullmatch(r"\.[A-Za-z0-9]+", suffix)]
    for directory in os.get_exec_path():
        if not directory or not Path(directory).is_absolute() or directory.replace("\\", "/").startswith("//"):
            continue
        for suffix in suffixes:
            path = Path(directory) / (name + suffix)
            if path.is_file() and (os.name == "nt" or os.access(path, os.X_OK)):
                return str(path)
    return None


def configuration(repo):
    candidates = []
    for name in ("package.json", "pyproject.toml", CONFIG_NAME):
        path = environment.relative_path(repo, name)
        if not path.is_file():
            continue
        g.require(path.stat().st_size <= 256 * 1024, "local project metadata size")
        if name == "package.json":
            metadata_value = read_json(path)
            section = metadata_value.get("governance", {})
            g.require(isinstance(section, dict), "package governance configuration")
            data = section.get("localTests")
        elif name == "pyproject.toml":
            import tomllib
            metadata_value = tomllib.loads(path.read_text(encoding="utf-8-sig"))
            tool = metadata_value.get("tool", {})
            g.require(isinstance(tool, dict), "Python tool configuration")
            section = tool.get("governance", {})
            g.require(isinstance(section, dict), "Python governance configuration")
            data = section.get("local-tests")
        else:
            data = read_json(path)
        if data is not None:
            candidates.append((path, data))
    g.require(len(candidates) <= 1, "single local test configuration source")
    if not candidates:
        return None, {"LocalTestProtocolVersion": "v1.0", "Commands": {}, "Resources": {}}
    path, data = candidates[0]
    g.require(isinstance(data, dict), "local project configuration")
    g.sensitive(data)
    base_keys = {"LocalTestProtocolVersion", "Commands", "Resources"}
    g.require(base_keys <= set(data) <= base_keys | {"PassThrough", "Automatic"}, "local project configuration fields")
    g.require(type(data.get("Automatic", False)) is bool, "automatic invocation setting")
    g.unique_strings(data.get("PassThrough", []), "explicit local environment names")
    g.require(all(re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name) and not name.startswith(("GIT_", "B1_", "PYTHON"))
                  for name in data.get("PassThrough", [])), "explicit environment name")
    g.require(data["LocalTestProtocolVersion"] == "v1.0", "local test protocol")
    g.require(isinstance(data["Commands"], dict) and isinstance(data["Resources"], dict), "local definitions")
    g.require(len(data["Commands"]) <= 128 and len(data["Resources"]) <= 32, "local definitions limit")
    for method, command in data["Commands"].items():
        environment.reference(method, "existing test method")
        g.exact_object(command, "command", ("Argv", "TimeoutSeconds"))
        argv(command["Argv"])
        timeout(command["TimeoutSeconds"])
    for ident, resource in data["Resources"].items():
        g.require(re.fullmatch(ID, ident), "local resource ID")
        kind = resource.get("Kind") if isinstance(resource, dict) else None
        keys = {"Kind", "Requires", "Check", "TimeoutSeconds"}
        if kind == "SERVICE":
            keys |= {"Start", "Port", "AllowPortOverride"}
        elif kind == "FIXTURE":
            keys.add("Prepare")
        else:
            g.require(kind == "CHECK", "local resource kind")
        g.exact_object(resource, "resource", keys)
        g.unique_strings(resource["Requires"], "resource dependencies")
        g.require(set(resource["Requires"]) <= set(data["Resources"]), "known resource dependencies")
        argv(resource["Check"])
        timeout(resource["TimeoutSeconds"])
        if kind == "SERVICE":
            argv(resource["Start"])
            g.require(type(resource["Port"]) is int and 0 <= resource["Port"] <= 65535, "local service port")
            g.require(type(resource["AllowPortOverride"]) is bool, "port override")
        if kind == "FIXTURE":
            argv(resource["Prepare"])
    visited = set()
    def visit(ident, ancestors):
        g.require(ident not in ancestors, "acyclic local resources")
        if ident in visited:
            return
        for dependency in data["Resources"][ident]["Requires"]:
            visit(dependency, ancestors | {ident})
        visited.add(ident)
    for ident in data["Resources"]:
        visit(ident, set())
    return path, data


def project(repo, requirement_id, owner=None):
    """Generate the request from existing plan/context sources; no per-task manifest."""
    g.require(g.valid_requirement_id(requirement_id), "RequirementId")
    repo = environment.local_absolute(str(Path(repo).absolute()))
    g.safe_path(repo, repo)
    tracking = repo / "docs/requirements" / requirement_id / "tracking.json"
    if tracking.exists():
        from governance_requirements import require_governed
        require_governed(repo, requirement_id, execution=True)
    g.require(Path(environment.discovery_git(repo, "rev-parse", "--show-toplevel").strip()).resolve() == repo.resolve(), "working tree root")
    # The stable plan validator invokes Git itself; reject redirects before calling it.
    g.require(not any(key.upper().startswith("GIT_") and key.upper() != "GIT_PAGER" for key in os.environ), "inherited Git overrides")
    base = repo / "docs/requirements" / requirement_id
    profile_path = g.safe_path(repo, base / "control-profile.json")
    plan_path = g.safe_path(repo, base / "acceptance-plan.json")
    context_path = g.safe_path(repo, base / "开发上下文包.md")
    initial_sources = {path: digest(path) for path in (profile_path, plan_path, context_path)}
    adaptation = g.safe_path(repo, repo / ".governance/project.json")
    if adaptation.is_file():
        initial_sources[adaptation] = digest(adaptation)
        if read_json(adaptation).get("LocalRuntimeDirectory") is not None:
            ignore = g.safe_path(repo, repo / ".gitignore")
            g.require(ignore.is_file(), "local runtime exclusion")
            initial_sources[ignore] = digest(ignore)
    plan_check = g.validate_plan(profile_path, plan_path)
    profile, plan = read_json(profile_path), read_json(plan_path)
    owner = owner or ("WRITER" if profile["SelectedProfile"] == "LEAN" else "QA")
    g.require(owner in ("WRITER", "QA"), "test owner")
    g.require(profile["SelectedProfile"] != "LEAN" or owner == "WRITER", "LEAN test owner")
    text = context_path.read_text(encoding="utf-8-sig")
    g.require(metadata(text, "产物当前状态") == "FROZEN", "frozen context")
    version = metadata(text, "产物版本")
    g.require(re.fullmatch(r"v[1-9]\d*\.\d+", version), "context version")
    gates = []
    facts = list(g.safe_path(repo, base / "facts").rglob("*.json"))
    g.require(len(facts) <= 1024, "local fact inspection limit")
    for path in facts:
        event = read_json(g.safe_path(repo, path))
        g.require(event.get("EventType") != "FACT_CORRECTION" and event.get("SupersedesEventRef") is None, "unresolved fact correction")
    gate_dir = g.safe_path(repo, base / "facts/gates")
    for path in sorted(gate_dir.glob("*.json")):
        event = read_json(g.safe_path(repo, path))
        g.validate_event_structure(event, requirement_id, path)
        g.require(event["SupersedesEventRef"] is None and event["EventType"] != "FACT_CORRECTION", "unresolved gate correction")
        if event["Payload"].get("Gate") == "G2" and event["ContextPackVersion"] == version:
            gates.append((path, event))
    g.require(len(gates) == 1, "unique current G2")
    gate_path, gate = gates[0]
    payload = gate["Payload"]
    g.require(gate["EventType"] == "GATE_DECISION" and payload.get("Decision") == "FROZEN", "frozen G2")
    g.require(payload.get("DecisionMode") in ("HUMAN", "AUTOMATIC") and bool(payload.get("BasisRefs")), "G2 decision evidence")
    if payload["DecisionMode"] == "HUMAN":
        g.string(payload.get("ApprovalRef"), "G2 approval reference")
    else:
        g.require(plan_check["RequiredG2DecisionMode"] == "AUTOMATIC_ALLOWED" and payload.get("ApprovalRef") is None, "automatic G2 eligibility")
    g.require(payload.get("ArtifactRef") == context_path.relative_to(repo).as_posix() and payload.get("ArtifactVersion") == version, "G2 context binding")
    config_path, config = configuration(repo)
    definitions, executions = table(text, "5.2.1", 5), table(text, "5.2.2", 7)
    g.require(definitions.keys() == executions.keys(), "validation table coverage")
    controls = {control["ControlId"]: control for control in plan["Controls"]}
    selected, covered = [], set()
    for ident, row in definitions.items():
        control_ids = re.findall(r"(?:BA|RB|QG|AD)-\d{3}", row[1])
        g.require(len(control_ids) == 1 and control_ids[0] in controls, "one control per local validation")
        control = controls[control_ids[0]]
        execution = executions[ident]
        if not control["Triggered"] or control["Owner"] != owner:
            continue
        g.require(row[3] == control["Method"], "plan/context method agreement")
        tokens = [plain(part) for part in execution[2].split("/")]
        expected_class = "BLOCKING" if control["Blocking"] else "ADVISORY"
        expected_target = "CANDIDATE_QUALITY" if control["Blocking"] else "NON_QA_FOLLOW_UP"
        g.require(tokens == [expected_class, expected_target, "TRUE"], "plan/context execution agreement")
        g.require(plain(execution[3].split("/")[0]) == owner, "plan/context owner agreement")
        g.require(control["ControlId"] in re.findall(r"(?:RB|QG|AD)-\d{3}", execution[1]), "context control source")
        environment_cell = execution[4].replace("`", "")
        required, unresolved = [], False
        if environment_cell not in ("NONE / N/A / N/A", "NONE"):
            parts = [part.strip() for part in environment_cell.split("/")]
            supported = (len(parts) == 3 and parts[0] in {"BACKEND", "DATABASE", "BROWSER_SESSION", "OS", "LOCAL", "TOOLS", "FIXTURE"}
                         and parts[1] in {"READY", "PLANNED"}
                         and re.fullmatch(r"LOCAL:" + ID + r"(?:\s*,\s*LOCAL:" + ID + r")*", parts[2]))
            unresolved = not supported
            if supported:
                required = list(dict.fromkeys(re.findall(r"LOCAL:(" + ID + r")", parts[2])))
        missing = control["Method"] not in config["Commands"] or not set(required) <= set(config["Resources"])
        platform = {"win32": "WINDOWS", "darwin": "MACOS", "linux": "LINUX"}.get(sys.platform)
        issue = ("UNSUPPORTED_METHOD" if control["MethodKind"] != "COMMAND" else
                 "PLATFORM_NOT_AVAILABLE" if control["ExecutionPlatform"] != platform else
                 "MISSING_PROJECT_ENTRY" if missing else "UNMAPPED_PREREQUISITE" if unresolved else None)
        selected.append({"ValidationId": ident, "ControlId": control["ControlId"], "GateClass": expected_class,
                         "Method": control["Method"], "Requires": required,
                         "InputIssue": issue})
        g.require(control["ControlId"] not in covered, "unique local control mapping")
        covered.add(control["ControlId"])
    expected = {control["ControlId"] for control in controls.values() if control["Triggered"] and control["Owner"] == owner}
    g.require(covered == expected, "complete selected validation set")
    sources = {path.relative_to(repo).as_posix(): digest(path) for path in (profile_path, plan_path, context_path, gate_path, config_path) if path is not None}
    sources.update({path.relative_to(repo).as_posix(): value for path, value in initial_sources.items()})
    g.require(all(digest(path) == value for path, value in initial_sources.items()), "stable planning inputs")
    return {"Repository": repo, "RequirementId": requirement_id, "ContextPackVersion": version,
            "Owner": owner, "Selected": selected, "Config": config, "Sources": sources}


class Process:
    """Run a foreground command in a contained tree; only kill processes we created."""
    def __init__(self, command, repo, child_env):
        self.job = None
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--_worker", json.dumps(command)],
                                        cwd=repo, env=child_env, stdin=subprocess.PIPE,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        creationflags=flags, start_new_session=os.name != "nt", bufsize=0)
        try:
            if os.name == "nt":
                self.job = windows_job(self.process.pid)
            # The worker cannot spawn the real command before containment is established.
            self.process.stdin.write(b"GO\n")
            self.process.stdin.close()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        if self.job is not None:
            kernel, handle = self.job
            stopped = kernel.TerminateJobObject(handle, 1)
            closed = kernel.CloseHandle(handle)  # KILL_ON_JOB_CLOSE also covers abnormal parent exit.
            self.job = None
            if not stopped or not closed:
                raise OSError("Unable to stop owned process job")
        elif os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)


def windows_job(pid):
    from ctypes import wintypes as w

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, args, result in (
        ("CreateJobObjectW", [ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
        ("SetInformationJobObject", [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
        ("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        ("AssignProcessToJobObject", [w.HANDLE, w.HANDLE], w.BOOL),
        ("TerminateJobObject", [w.HANDLE, w.UINT], w.BOOL),
        ("CloseHandle", [w.HANDLE], w.BOOL),
    ):
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise OSError("Unable to create owned process job")
    process = None
    try:
        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise OSError("Unable to configure owned process job")
        process = kernel.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE, required by assignment.
        if not process or not kernel.AssignProcessToJobObject(job, process):
            raise OSError("Unable to contain owned process tree")
        return kernel, job
    except BaseException:
        kernel.CloseHandle(job)
        raise
    finally:
        if process:
            kernel.CloseHandle(process)


def run_command(command, repo, child_env, seconds):
    started = time.monotonic()
    process = None
    try:
        process = Process(command, repo, child_env)
        code = process.process.wait(timeout=seconds)
        return {"Status": "SUCCEEDED" if code == 0 else "NONZERO_EXIT", "ExitCode": code,
                "DurationMs": round((time.monotonic() - started) * 1000)}
    except subprocess.TimeoutExpired:
        return {"Status": "TIMED_OUT", "ExitCode": None, "DurationMs": round((time.monotonic() - started) * 1000)}
    except OSError:
        return {"Status": "PROCESS_UNAVAILABLE", "ExitCode": None, "DurationMs": round((time.monotonic() - started) * 1000)}
    finally:
        if process:
            process.close()


def source_state(repo):
    """A before/after write guard, not an immutable candidate snapshot or B2 fact."""
    names = environment.discovery_git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
    result = {}
    for name in sorted(set(names) - {""}):
        path = environment.relative_path(repo, name)
        if path.is_file():
            result[name] = digest(path)
    return result


def choose_port(preferred, allow_override):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            handle.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            handle.bind(("127.0.0.1", preferred))
        except OSError:
            if not allow_override:
                return None
            handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


def runtime_root(repo):
    """An adopted project can keep local state in its explicitly ignored workspace."""
    repo = Path(repo).absolute()
    adaptation = g.safe_path(repo, repo / ".governance/project.json")
    if adaptation.is_file():
        value = read_json(adaptation)
        directory = value.get("LocalRuntimeDirectory")
        if directory is not None:
            g.require(value.get("ProjectFormatVersion") == "v1.0" and directory == ".governance/local", "project local runtime directory")
            # Never hide business changes by inventing an unreviewed exclusion.
            environment.discovery_git(repo, "check-ignore", "--quiet", "--no-index", directory + "/.ownership-probe")
            g.require(not environment.discovery_git(repo, "ls-files", "--", directory).strip(), "local runtime contains no tracked files")
            path = g.safe_path(repo, repo / directory)
            path.mkdir(parents=True, exist_ok=True)
            return path
    git_dir = environment.local_absolute(environment.discovery_git(repo, "rev-parse", "--absolute-git-dir").strip())
    return g.safe_path(git_dir, git_dir / "governance")


def temporary_run_directory(repo):
    local_root = runtime_root(repo)
    if local_root == Path(repo).absolute() / ".governance/local":
        parent = g.safe_path(repo, local_root / "runs")
        parent.mkdir(parents=True, exist_ok=True)
        path = g.safe_path(repo, parent / ("governance-b1-" + uuid.uuid4().hex))
        # Windows inherits the approved project ACL. Python's 0700 private DACL
        # prevents the native restricted token from reopening its own directory.
        path.mkdir(mode=0o777 if os.name == "nt" else 0o700)
        return path, parent
    path = Path(tempfile.mkdtemp(prefix="governance-b1-"))
    return path, Path(tempfile.gettempdir())


def retain_report(repo, report):
    root = runtime_root(repo)
    directory = g.safe_path(root, root / "local-tests")
    directory.mkdir(parents=True, exist_ok=True)
    path = g.safe_path(root, directory / (report["RunId"] + ".json"))
    report["LocalReportPath"] = str(path)
    g.sensitive(report)
    with path.open("x", encoding="utf-8") as target:
        json.dump(report, target, ensure_ascii=False, sort_keys=True, indent=2)


def execute(request, *, run_tests=False, dispatch=None, validation_ids=None):
    """Keep owned resources alive through dispatch; always release them afterwards.

    A runtime dispatch callback receives the ready subset, run-local environment and observations.
    It must check the candidate and save formal readiness before dispatching independent QA.
    Without a callback, --run executes local commands and never claims QA PASS.
    """
    repo = request["Repository"]
    g.require(request["Owner"] != "QA" or dispatch is not None, "QA requires existing runtime dispatch and candidate checks")
    g.require(not (run_tests and dispatch is not None), "single execution owner")
    g.require(all(digest(environment.relative_path(repo, name)) == value for name, value in request["Sources"].items()), "unchanged execution sources")
    current = project(repo, request["RequirementId"], request["Owner"])
    g.require(all(current[key] == request[key] for key in ("Sources", "Config", "Selected", "ContextPackVersion")), "generated request binding")
    if validation_ids is not None:
        g.unique_strings(validation_ids, "selected resume validation IDs", minimum=1)
        g.require(set(validation_ids) <= {item["ValidationId"] for item in request["Selected"]}, "known resume validation IDs")
    selected = [item for item in request["Selected"] if validation_ids is None or item["ValidationId"] in validation_ids]
    before = source_state(repo)
    report = {"ResultType": "LOCAL_TEST_EXECUTION", "LocalTestProtocolVersion": "v1.0",
              "RunId": uuid.uuid4().hex, "RequirementId": request["RequirementId"], "ContextPackVersion": request["ContextPackVersion"],
              "Owner": request["Owner"], "GatesChanged": False, "QualityPassed": False,
              "CandidateBinding": "RUNTIME_VERIFICATION_REQUIRED", "FormalReadinessWritten": False,
              "StartedAt": datetime.now(timezone.utc).isoformat(),
              "Sources": request["Sources"], "Resources": {}, "Validations": [], "Issues": [], "Cleanup": "PENDING"}
    report["InvocationValidationIds"] = [item["ValidationId"] for item in selected]
    config = request["Config"]
    system_names = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE",
                    "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "LANG", "JAVA_HOME", "MAVEN_HOME", "M2_HOME"}
    child_env = {key: value for key, value in os.environ.items()
                 if key.upper() in system_names or key.startswith("LC_") or key in config.get("PassThrough", [])}
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    child_env["B1_RUN_ID"] = report["RunId"]
    child_env["B1_REQUIREMENT_ID"] = request["RequirementId"]
    child_env["B1_REPOSITORY_ROOT"] = str(repo)
    child_env["B1_CONTEXT_PACK_VERSION"] = request["ContextPackVersion"]
    processes, services, visiting = [], {}, set()
    run_dir, temporary_parent = temporary_run_directory(repo)
    child_env["B1_RUN_DIR"] = str(run_dir)
    def unchanged():
        after = source_state(repo)
        # A runtime callback may append formal readiness/results while this environment is alive.
        prefix = "docs/requirements/" + request["RequirementId"] + "/facts/"
        additions = set(after) - set(before)
        allowed = (prefix + "readiness/", prefix + "qa/") if dispatch is not None else ()
        return (all(after.get(name) == value for name, value in before.items())
                and all(any(name.startswith(part) and name.endswith(".json") for part in allowed) for name in additions))

    def command(parts):
        resolved = executable(parts[0])
        if resolved is None:
            raise FileNotFoundError("Required local tool is missing")
        return [resolved, *parts[1:]]

    def missing_tools(item):
        names, seen = set(), set()
        def inspect(ident):
            if ident in seen:
                return
            seen.add(ident)
            spec = config["Resources"][ident]
            for key in ("Start", "Prepare", "Check"):
                if key in spec:
                    names.add(spec[key][0])
            for dependency in spec["Requires"]:
                inspect(dependency)
        for ident in item["Requires"]:
            inspect(ident)
        names.add(config["Commands"][item["Method"]]["Argv"][0])
        return sorted(name for name in names if executable(name) is None)

    def resource(ident):
        if ident in report["Resources"]:
            return report["Resources"][ident]["Status"] == "READY"
        g.require(ident not in visiting, "acyclic resource dependencies")
        visiting.add(ident)
        spec = config["Resources"][ident]
        result = {"Status": "NOT_READY", "Kind": spec["Kind"], "Checks": []}
        report["Resources"][ident] = result
        if not all(resource(dependency) for dependency in spec["Requires"]):
            result["Reason"] = "DEPENDENCY_NOT_READY"
            visiting.remove(ident)
            return False
        directory = run_dir / ident
        directory.mkdir()
        child_env["B1_" + ident + "_DIR"] = str(directory)
        seconds = spec["TimeoutSeconds"]
        service = None
        try:
            if spec["Kind"] == "SERVICE":
                port = choose_port(spec["Port"], spec["AllowPortOverride"])
                if port is None:
                    result["Reason"] = "PORT_CONFLICT"
                    return False
                result["Port"] = port
                result["PortChanged"] = port != spec["Port"]
                child_env["B1_" + ident + "_PORT"] = str(port)
                service = Process(command(spec["Start"]), repo, child_env)
                processes.append(service)
                services[ident] = service
            elif spec["Kind"] == "FIXTURE":
                preparation = run_command(command(spec["Prepare"]), repo, child_env, seconds)
                result["Preparation"] = preparation
                if preparation["Status"] != "SUCCEEDED":
                    result["Reason"] = "PREPARATION_FAILED"
                    return False
            # Poll readiness only; never restart, reinstall or repeat fixture preparation.
            deadline = time.monotonic() + seconds
            while True:
                if service is not None and service.process.poll() is not None:
                    result["Reason"] = "SERVICE_EXITED"
                    return False
                check = run_command(command(spec["Check"]), repo, child_env, max(0.1, min(2, deadline - time.monotonic())))
                result["Checks"].append(check)
                if not unchanged():
                    result["Reason"] = "REPOSITORY_CHANGED"
                    return False
                if check["Status"] == "SUCCEEDED":
                    result["Status"] = "READY"
                    result["EvidenceKind"] = "EXECUTED_PROJECT_CHECK"
                    return True
                if spec["Kind"] != "SERVICE" or time.monotonic() >= deadline:
                    result["Reason"] = "CHECK_FAILED"
                    return False
                time.sleep(0.1)
        except OSError:
            result["Reason"] = "PROCESS_UNAVAILABLE"
            return False
        finally:
            visiting.remove(ident)

    def recheck(required):
        checked = set()
        def check(ident):
            if ident in checked:
                return report["Resources"][ident]["Status"] == "READY"
            checked.add(ident)
            spec = config["Resources"][ident]
            if not all(check(dependency) for dependency in spec["Requires"]):
                return False
            result = report["Resources"][ident]
            service = services.get(ident)
            if service is not None and service.process.poll() is not None:
                result.update(Status="NOT_READY", Reason="SERVICE_EXITED")
                return False
            observation = run_command(command(spec["Check"]), repo, child_env, min(2, spec["TimeoutSeconds"]))
            result["Checks"].append(observation)
            if observation["Status"] != "SUCCEEDED":
                result.update(Status="NOT_READY", Reason="CHECK_FAILED")
                return False
            return True
        return unchanged() and all(check(ident) for ident in required) and unchanged()

    try:
        for item in selected:
            entry = {"ValidationId": item["ValidationId"], "ControlId": item["ControlId"], "GateClass": item["GateClass"],
                     "Readiness": "NOT_READY", "Execution": "NOT_EXECUTED"}
            report["Validations"].append(entry)
            if item["InputIssue"]:
                entry["Reason"] = item["InputIssue"]
                continue
            missing = missing_tools(item)
            if missing:
                entry.update(Reason="MISSING_TOOL", MissingTools=missing)
                continue
            if not all(resource(ident) for ident in item["Requires"]):
                entry["Reason"] = "PREREQUISITE_NOT_READY"
                continue
            if not recheck(item["Requires"]):
                entry["Reason"] = "SOURCE_OR_SERVICE_CHANGED"
                continue
            entry["Readiness"] = "READY"
        ready = [item for item in selected if any(entry["ValidationId"] == item["ValidationId"] and entry["Readiness"] == "READY" for entry in report["Validations"])]
        if ready and unchanged():
            if dispatch is not None:
                # Do not record callback output: role results belong to the existing runtime.
                dispatch(ready, dict(child_env), json.loads(json.dumps(report)))
                report["DispatchedValidationIds"] = [item["ValidationId"] for item in ready]
            elif run_tests:
                for item in ready:
                    entry = next(entry for entry in report["Validations"] if entry["ValidationId"] == item["ValidationId"])
                    if not recheck(item["Requires"]):
                        entry.update(Readiness="NOT_READY", Reason="SOURCE_OR_SERVICE_CHANGED")
                        continue
                    spec = config["Commands"][item["Method"]]
                    observation = run_command(command(spec["Argv"]), repo, child_env, spec["TimeoutSeconds"])
                    entry.update(Execution="EXECUTED", Observation=observation)
        if not unchanged():
            report["Issues"].append("REPOSITORY_CHANGED")
    except KeyboardInterrupt:
        report["Issues"].append("INTERRUPTED")
    except (OSError, g.Invalid, subprocess.TimeoutExpired):
        report["Issues"].append("EXECUTION_OR_INPUT_ERROR")
    except Exception:
        report["Issues"].append("DISPATCH_ERROR")
        raise
    finally:
        for item in selected:
            if not any(entry["ValidationId"] == item["ValidationId"] for entry in report["Validations"]):
                report["Validations"].append({"ValidationId": item["ValidationId"], "ControlId": item["ControlId"],
                                              "GateClass": item["GateClass"], "Readiness": "NOT_READY",
                                              "Execution": "NOT_EXECUTED", "Reason": "EXECUTION_INTERRUPTED"})
        cleanup_errors = []
        for process in reversed(processes):
            try:
                process.close()
            except (OSError, subprocess.TimeoutExpired):
                cleanup_errors.append("OWNED_PROCESS_CLEANUP_FAILED")
        try:
            # Delete only the exact fresh temporary root, never project or shared directories.
            g.require(run_dir.parent.resolve() == temporary_parent.resolve() and run_dir.name.startswith("governance-b1-"), "owned temporary root")
            g.safe_path(run_dir, run_dir)
            if cleanup_errors:
                cleanup_errors.append("OWNED_DATA_CLEANUP_DEFERRED")
                report["OwnedRunDirectory"] = str(run_dir)
            else:
                shutil.rmtree(run_dir)
        except (OSError, g.Invalid):
            cleanup_errors.append("OWNED_DATA_CLEANUP_FAILED")
            report["OwnedRunDirectory"] = str(run_dir)
        report["Cleanup"] = "FAILED" if cleanup_errors else "COMPLETED"
        report["Issues"].extend(cleanup_errors)
        try:
            if not unchanged():
                if "REPOSITORY_CHANGED" not in report["Issues"]:
                    report["Issues"].append("REPOSITORY_CHANGED")
                for item in report["Validations"]:
                    item.update(Readiness="NOT_READY", Reason="REPOSITORY_CHANGED")
        except (OSError, g.Invalid):
            report["Issues"].append("REPOSITORY_STATE_UNVERIFIABLE")
        ready_count = sum(item["Readiness"] == "READY" for item in report["Validations"])
        report["PreparationSummary"] = ("NO_SELECTED_TESTS" if not report["Validations"] else
                                         "READY" if ready_count == len(report["Validations"]) else
                                         "PARTIALLY_READY" if ready_count else "NOT_READY")
        report["CompletedAt"] = datetime.now(timezone.utc).isoformat()
        report["NextActions"] = [{"ValidationId": item["ValidationId"], "Action": "RESOLVE_" + item["Reason"]}
                                 for item in report["Validations"] if item.get("Reason")]
        report["NextActions"].extend({"Scope": "RUN", "Action": "RESOLVE_" + issue} for issue in report["Issues"])
        try:
            retain_report(repo, report)
        except (OSError, g.Invalid):
            report["Issues"].append("LOCAL_REPORT_WRITE_FAILED")
    return report


def main(argv_input=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.error = lambda _message: parser.exit(2, "Invalid local test arguments.\n")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--requirement-id", required=True)
    parser.add_argument("--owner", choices=("WRITER", "QA"))
    parser.add_argument("--validation-id", action="append", help="runtime-selected subset for recovery; repeat for each ID")
    parser.add_argument("--run", action="store_true", help="execute the selected local test commands after preparation")
    args = parser.parse_args(argv_input)
    if args.validation_id and not args.run:
        parser.error("resume subset requires execution")
    try:
        request = project(args.repo, args.requirement_id, args.owner)
        if not args.run:
            print(json.dumps({"ResultType": "LOCAL_TEST_PREVIEW", "Selected": request["Selected"], "Sources": request["Sources"],
                              "AutomaticEnabled": request["Config"].get("Automatic", False), "Owner": request["Owner"],
                              "CommandsExecuted": False, "GatesChanged": False, "QualityPassed": False}, ensure_ascii=False, sort_keys=True))
            return 1 if any(item["InputIssue"] for item in request["Selected"]) else 0
        report = execute(request, run_tests=True, validation_ids=args.validation_id)
    except (g.Invalid, OSError, UnicodeError, ValueError, subprocess.TimeoutExpired):
        print("Unable to prepare local tests; check bound project inputs and entry points.", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 1 if report["Issues"] or any(item["GateClass"] == "BLOCKING" and (item["Readiness"] != "READY" or item.get("Observation", {}).get("Status") != "SUCCEEDED") for item in report["Validations"]) else 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--_worker":
        if sys.stdin.buffer.readline() != b"GO\n":
            raise SystemExit(2)
        # Parent sets the process job/group before releasing this worker.
        raise SystemExit(subprocess.call(json.loads(sys.argv[2]), stdin=subprocess.DEVNULL))
    raise SystemExit(main())
