"""Read-only local environment diagnostics; never creates governance or readiness facts."""

import argparse
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import sys
import tomllib

sys.dont_write_bytecode = True
import governance_tools as g


def local_absolute(value):
    g.string(value, "local absolute path")
    g.require(not value.replace("\\", "/").startswith("//"), "network path unsupported")
    path = Path(value)
    g.require(path.is_absolute(), "local absolute path")
    g.require(".." not in path.parts, "local absolute path")
    return path


def relative_path(repo, value):
    g.string(value, "repository relative path")
    normalized = value.replace("\\", "/")
    g.require(not normalized.startswith("/") and not PureWindowsPath(value).drive, "repository relative path")
    parts = normalized.split("/")
    g.require(all(part not in ("", ".", "..") and not re.search(r'[\x00-\x1f:*?<>|]', part) for part in parts),
              "repository relative path")
    return g.safe_path(repo, Path(repo).joinpath(*parts))


def identifier(value, field):
    g.require(isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_-]{0,63}", value), field)
    return value


def reference(value, field):
    g.string(value, field)
    g.require(not re.search(r"[\x00-\x1f]", value), field)
    return value


def command_reference(repo, value):
    reference(value, "command reference")
    if not re.fullmatch(r"COMMAND:[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value):
        relative_path(repo, value.split("#", 1)[0])


def entries(value, field, keys, id_key):
    items = g.array(value, field)
    seen = set()
    for item in items:
        g.exact_object(item, field, keys)
        ident = identifier(item[id_key], id_key)
        g.require(ident not in seen, field + " duplicate ID")
        seen.add(ident)
    return items


def manifest_data(repo, manifest, owner):
    path = relative_path(repo, manifest)
    g.require(path.suffix.lower() == ".json", "manifest JSON extension")
    name_parts = re.split(r"[._-]+", path.name.lower())
    g.require(not set(name_parts) & {"env", "secret", "secrets", "credential", "credentials", "password", "token", "tokens"},
              "sensitive manifest filename")
    data = g.load_contract(path, "EnvironmentManifest")
    expected = {"ToolProtocolVersion", "RequirementId", "RepositoryRoot", "Paths", "Tools", "Ports", "Validations"}
    g.require(set(data) in (expected, expected | {"CandidateRef"}), "EnvironmentManifest fields")
    g.require(data["ToolProtocolVersion"] == "v1.0", "ToolProtocolVersion")
    g.require(g.valid_requirement_id(data["RequirementId"]), "manifest RequirementId")
    local_absolute(data["RepositoryRoot"])
    if data.get("CandidateRef") is not None:
        reference(data["CandidateRef"], "CandidateRef declaration")

    paths = entries(data["Paths"], "Paths", ("PathId", "Path", "Kind"), "PathId")
    for item in paths:
        g.require(item["Kind"] in ("FILE", "DIRECTORY"), "path Kind")
        relative_path(repo, item["Path"])
    tools = entries(data["Tools"], "Tools", ("ToolId", "Kind", "Value"), "ToolId")
    for item in tools:
        g.require(item["Kind"] in ("PATH", "REPO_FILE"), "tool Kind")
        if item["Kind"] == "PATH":
            g.require(isinstance(item["Value"], str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", item["Value"]),
                      "PATH executable name only")
        else:
            relative_path(repo, item["Value"])
    ports = entries(data["Ports"], "Ports", ("PortId", "Port"), "PortId")
    for item in ports:
        g.require(type(item["Port"]) is int and 1 <= item["Port"] <= 65535, "Port integer range")
    validations = entries(data["Validations"], "Validations",
                          ("ValidationId", "Triggered", "Owner", "GateClass", "CommandRef", "Prerequisites"), "ValidationId")
    for item in validations:
        g.require(type(item["Triggered"]) is bool, "Triggered boolean")
        g.require(item["Owner"] in ("WRITER", "QA", "REVIEWER"), "Owner")
        g.require(item["GateClass"] in ("BLOCKING", "ADVISORY"), "GateClass")
        command_reference(repo, item["CommandRef"])
        if not item["Triggered"] or item["Owner"] != owner:
            continue
        prerequisites = entries(item["Prerequisites"], "Prerequisites",
                                ("PrerequisiteId", "Readiness", "Responsible", "CompletionCondition", "ReadinessCheck"), "PrerequisiteId")
        for prerequisite in prerequisites:
            g.require(prerequisite["Readiness"] in ("READY", "PLANNED", "UNKNOWN"), "declared Readiness")
            for key in ("Responsible", "CompletionCondition", "ReadinessCheck"):
                g.require(isinstance(prerequisite[key], str) and not re.search(r"[\x00-\x1f]", prerequisite[key]), key)
            if prerequisite["ReadinessCheck"]:
                command_reference(repo, prerequisite["ReadinessCheck"])
    return data


def local_tool_exists(name):
    # Do not let executable discovery visit an explicit UNC PATH entry.
    directories = []
    for value in os.get_exec_path():
        if value and not value.replace("\\", "/").startswith("//") and Path(value).is_absolute():
            directories.append(value)
    return shutil.which(name, path=os.pathsep.join(directories)) is not None


def diagnose(repo, manifest, requirement_id, phase="execution", owner="QA"):
    g.LOCATION.set("EnvironmentDiagnostic")
    g.require(g.valid_requirement_id(requirement_id), "requested RequirementId")
    g.require(phase in ("planning", "execution"), "phase")
    g.require(owner in ("WRITER", "QA", "REVIEWER"), "owner scope")
    repo = local_absolute(str(Path(repo).absolute()) if not str(repo).replace("\\", "/").startswith("//") else str(repo))
    repo = g.safe_path(repo, repo)
    g.require(repo.is_dir(), "repository directory")
    data = manifest_data(repo, manifest, owner)
    report = {
        "ResultType": "ENVIRONMENT_DIAGNOSTIC", "ToolProtocolVersion": "v1.0",
        "DiagnosticOnly": True, "GatesChanged": False, "QualityPassed": False,
        "RequirementId": requirement_id,
        "Phase": phase, "OwnerScope": owner,
        "ServiceOwnership": "UNKNOWN", "CandidateBinding": "UNKNOWN",
        "ReportChecks": [], "BlockingIssues": [],
        "Unverified": [{"Check": "SERVICE_OWNERSHIP", "Status": "UNKNOWN"},
                       {"Check": "CANDIDATE_BINDING", "Status": "UNKNOWN"}],
    }

    def record(check, status, ident=None, blocking=False, unverified=False, **extra):
        item = {"Check": check, "Status": status, **extra}
        if ident is not None:
            item["Id"] = ident
        report["ReportChecks"].append(item)
        if blocking:
            report["BlockingIssues"].append(dict(item))
        if unverified:
            report["Unverified"].append(dict(item))

    record("REQUIREMENT_BINDING", "MATCH" if data["RequirementId"] == requirement_id else "MISMATCH",
           blocking=data["RequirementId"] != requirement_id)
    declared_root = local_absolute(data["RepositoryRoot"])
    root_matches = os.path.normcase(os.path.normpath(str(declared_root))) == os.path.normcase(str(repo.resolve()))
    record("DECLARED_REPOSITORY_ROOT", "MATCH" if root_matches else "MISMATCH", blocking=not root_matches)
    try:
        git_env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                encoding="utf-8", timeout=5, check=False, env=git_env)
        actual = local_absolute(result.stdout.strip()) if result.returncode == 0 else None
        correct_root = actual is not None and actual.resolve() == repo.resolve()
        record("GIT_WORKTREE_ROOT", "MATCH" if correct_root else "MISMATCH", blocking=not correct_root)
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        record("GIT_WORKTREE_ROOT", "UNKNOWN", blocking=True, unverified=True)

    for item in data["Paths"]:
        path = relative_path(repo, item["Path"])
        exists = path.is_file() if item["Kind"] == "FILE" else path.is_dir()
        record("PATH_EXISTS", "EXISTS" if exists else "MISSING", item["PathId"],
               blocking=not exists and phase == "execution", unverified=not exists, ExistsOnly=True)
    for item in data["Tools"]:
        exists = local_tool_exists(item["Value"]) if item["Kind"] == "PATH" else relative_path(repo, item["Value"]).is_file()
        record("TOOL_EXISTS", "EXISTS" if exists else "MISSING", item["ToolId"],
               blocking=not exists and phase == "execution", unverified=not exists, ExistsOnly=True)
    used_ports = set()
    for item in data["Ports"]:
        duplicate = item["Port"] in used_ports
        used_ports.add(item["Port"])
        record("DECLARED_PORT_UNIQUENESS", "MISMATCH" if duplicate else "UNIQUE", item["PortId"], blocking=duplicate)
        record("PORT_AVAILABILITY", "UNKNOWN", item["PortId"], unverified=True)

    for item in data["Validations"]:
        selected = item["Triggered"] and item["Owner"] == owner
        record("VALIDATION_SCOPE", "SELECTED" if selected else "NOT_APPLICABLE", item["ValidationId"])
        if not selected:
            continue
        for prerequisite in item["Prerequisites"]:
            readiness = prerequisite["Readiness"]
            complete = all(prerequisite[key].strip() for key in ("Responsible", "CompletionCondition", "ReadinessCheck"))
            status = {"READY": "DECLARED_READY", "PLANNED": "PLANNED" if complete else "INCOMPLETE_PLAN", "UNKNOWN": "UNKNOWN"}[readiness]
            record("PREREQUISITE_DECLARATION", status, prerequisite["PrerequisiteId"],
                   blocking=phase == "execution" and item["GateClass"] == "BLOCKING" and readiness != "READY",
                   unverified=True, ValidationId=item["ValidationId"], DeclaredReadiness=readiness,
                   EvidenceKind="DECLARED")
    return report


DISCOVERY_FILES = {"package.json": "NODE", "pom.xml": "MAVEN",
                   "pyproject.toml": "PYTHON", "requirements.txt": "PYTHON"}
DISCOVERY_EXCLUDED = {".git", ".codex", ".agents", "node_modules", ".venv", "venv", "vendor", "target", "build", "dist"}


def discovery_git(repo, *args):
    # Only fixed read-only Git queries; discard inherited redirects and tracing.
    git_env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    git_env["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(["git", "-C", str(repo), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=10, check=False, env=git_env)
    g.require(result.returncode == 0, "local Git discovery")
    g.require(len(result.stdout) <= 8 * 1024 * 1024, "Git discovery size limit")
    return result.stdout.decode("utf-8")


def discover(repo, requirement_id=None, scope="."):
    """Inspect known metadata and local tool presence without running project code."""
    g.LOCATION.set("LocalEnvironmentDiscovery")
    if requirement_id is not None:
        g.require(g.valid_requirement_id(requirement_id), "requested RequirementId")
    repo = g.safe_path(local_absolute(str(Path(repo).absolute())), Path(repo).absolute())
    root = local_absolute(discovery_git(repo, "rev-parse", "--show-toplevel").strip())
    g.require(root.resolve() == repo.resolve(), "Git working tree root")
    selected = repo if scope == "." else relative_path(repo, scope)
    g.require(selected.is_dir(), "discovery scope directory")
    report = {
        "ResultType": "LOCAL_ENVIRONMENT_DISCOVERY", "DiscoveryProtocolVersion": "v1.0",
        "DiagnosticOnly": True, "GatesChanged": False, "QualityPassed": False,
        "RequirementId": requirement_id, "RequirementBinding": "UNKNOWN",
        "CandidateBinding": "UNKNOWN", "ServiceOwnership": "UNKNOWN",
        "Coverage": "SUPPORTED_METADATA_ONLY", "Projects": [], "Findings": [],
        "DiagnosticInterpreterVersion": ".".join(str(part) for part in sys.version_info[:3]),
        "Unverified": ["TASK_SCOPE", "DEPENDENCY_INSTALLATION", "TOOL_VERSION_COMPATIBILITY",
                       "LOCAL_SERVICES", "PORT_AVAILABILITY", "TEST_DATA", "CANDIDATE_BINDING"],
    }
    names = discovery_git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
    sources = sorted({name for name in names if name and Path(name).name in DISCOVERY_FILES
                      and not set(Path(name).parts[:-1]) & DISCOVERY_EXCLUDED
                      and (scope == "." or Path(name).is_relative_to(selected.relative_to(repo)))})
    g.require(len(sources) <= 128, "discovery metadata count limit")
    for source in sources:
        # Filenames and metadata may contain private values; never echo raw parser errors.
        g.require(not re.search(r"[\x00-\x1f]", source), "discovery source name")
        g.sensitive(source)
        path = relative_path(repo, source)
        if not path.is_relative_to(selected):
            continue
        if not path.exists():
            # Tracked deletions are not current project metadata.
            continue
        g.require(path.is_file(), "discovery metadata file")
        item = {"Source": source, "Kind": DISCOVERY_FILES[path.name], "Status": "DETECTED",
                "DeclaredVersions": {}, "EntryRefs": [], "Tools": []}
        tool_names = {"NODE": ["node"], "MAVEN": ["java", "javac"], "PYTHON": ["python"]}[item["Kind"]]
        try:
            g.require(path.stat().st_size <= 256 * 1024, "metadata size limit")
            if path.name == "package.json":
                data = g.load_contract(path, "PackageMetadata")
                engines = data.get("engines", {})
                scripts = data.get("scripts", {})
                g.require(isinstance(engines, dict) and isinstance(scripts, dict), "package metadata fields")
                g.require(all(isinstance(value, str) for value in scripts.values()), "script definitions")
                for name in ("node", "npm"):
                    if name in engines:
                        item["DeclaredVersions"][name] = reference(engines[name], "engine declaration")
                # Return references, never command bodies (which can contain credentials).
                for name in sorted(scripts):
                    g.require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_.-]{0,63}", name), "script name")
                    item["EntryRefs"].append(source + "#scripts." + name)
                manager = data.get("packageManager")
                if manager is not None:
                    g.require(isinstance(manager, str), "package manager declaration")
                    match = re.fullmatch(r"(npm|pnpm|yarn)@([0-9][A-Za-z0-9.+-]{0,100})", manager)
                    if match:
                        tool_names.append(match[1])
                        item["PackageManager"] = {"Tool": match[1], "DeclaredVersion": match[2]}
                    else:
                        report["Findings"].append({"Source": source, "Check": "PACKAGE_MANAGER", "Status": "UNKNOWN"})
                else:
                    managers = [tool for filename, tool in (("package-lock.json", "npm"), ("pnpm-lock.yaml", "pnpm"),
                                                           ("yarn.lock", "yarn"))
                                if relative_path(repo, (path.parent / filename).relative_to(repo).as_posix()).is_file()]
                    if len(managers) == 1:
                        tool_names.extend(managers)
                    else:
                        report["Findings"].append({"Source": source, "Check": "PACKAGE_MANAGER", "Status": "UNKNOWN"})
            elif path.name == "pyproject.toml":
                data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
                project = data.get("project", {})
                g.require(isinstance(project, dict), "Python project metadata")
                if "requires-python" in project:
                    item["DeclaredVersions"]["python"] = reference(project["requires-python"], "Python version declaration")
                scripts = project.get("scripts", {})
                g.require(isinstance(scripts, dict), "Python script metadata")
                g.require(all(isinstance(value, str) for value in scripts.values()), "Python script definitions")
                for name in sorted(scripts):
                    g.require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name), "Python script name")
                    item["EntryRefs"].append(source + "#project.scripts." + name)
            elif path.name == "pom.xml":
                # Raw POM presence cannot resolve parents, profiles or the effective model.
                item["Status"] = "MARKER_ONLY"
                wrappers = [name for name in ("mvnw", "mvnw.cmd")
                            if relative_path(repo, (path.parent / name).relative_to(repo).as_posix()).is_file()]
                item["EntryRefs"] = [(path.parent / name).relative_to(repo).as_posix() for name in wrappers]
                item["MavenWrapper"] = "PRESENT" if wrappers else "NOT_FOUND"
                # A wrapper may be unusable offline; presence never implies Maven is ready.
                if not wrappers:
                    tool_names.append("mvn")
                report["Findings"].append({"Source": source, "Check": "EFFECTIVE_MAVEN_MODEL", "Status": "UNKNOWN"})
            else:
                # Do not read requirement URLs or execute installer/build configuration.
                item["Status"] = "MARKER_ONLY"
            g.sensitive(item)
        except (g.Invalid, OSError, UnicodeError, ValueError):
            item = {"Source": source, "Kind": item["Kind"], "Status": "INVALID_OR_UNREADABLE",
                    "DeclaredVersions": {}, "EntryRefs": [], "Tools": []}
            report["Findings"].append({"Source": source, "Check": "PROJECT_METADATA", "Status": "INVALID_OR_UNREADABLE"})
        for name in dict.fromkeys(tool_names):
            exists = local_tool_exists(name)
            item["Tools"].append({"Tool": name, "Status": "EXISTS" if exists else "MISSING", "ExistsOnly": True})
            if not exists:
                report["Findings"].append({"Source": source, "Check": "TOOL_EXISTS", "Tool": name, "Status": "MISSING"})
        report["Projects"].append(item)
    report["SelectionRequired"] = len({str(Path(item["Source"]).parent) for item in report["Projects"]}) > 1
    if not report["Projects"]:
        report["Unverified"].append("NO_SUPPORTED_PROJECT_METADATA")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.error = lambda _message: parser.exit(2, "Invalid environment arguments.\n")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--manifest", help="optional explicit declaration checks; omit for local discovery")
    parser.add_argument("--requirement-id")
    parser.add_argument("--scope", help="repository-relative discovery directory (default: root)")
    parser.add_argument("--phase", choices=("planning", "execution"))
    parser.add_argument("--owner", choices=("WRITER", "QA", "REVIEWER"))
    args = parser.parse_args(argv)
    if args.manifest and (not args.requirement_id or args.scope is not None):
        parser.error("manifest mode requires a requirement and does not use discovery scope")
    if not args.manifest and (args.phase is not None or args.owner is not None):
        parser.error("invalid discovery options")
    try:
        report = (diagnose(args.repo, args.manifest, args.requirement_id, args.phase or "execution", args.owner or "QA")
                  if args.manifest else discover(args.repo, args.requirement_id, args.scope or "."))
    except (g.Invalid, OSError, UnicodeError, ValueError, subprocess.TimeoutExpired):
        print("Unable to inspect environment input; check its structure and local paths.", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 1 if report.get("BlockingIssues") or report.get("Findings") else 0


if __name__ == "__main__":
    raise SystemExit(main())
