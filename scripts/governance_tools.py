"""Governance helpers (Python 3.12 standard library).

check --repo PATH checks Markdown tables/local links/rule references and role TOML.
render --requirement-dir PATH --kind qa-index|review-index|changes prints a block.
validate-plan checks v3.0/v3.0.1 ControlProfile and AcceptancePlan JSON contracts.
init-developer writes local Git config; reserve-id writes shared local ID reservations.
All other commands are read-only.
Rendering supports persisted SchemaVersion v1.1 events using machine protocol v2.1
under context document versions v2.1, v3.0 and v3.0.1. It checks consumed fields,
not the complete runtime Schema or gate validity.
Corrections require runtime resolution and are deliberately not interpreted here.
"""

import argparse
import copy
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib
import unicodedata
from urllib.parse import unquote, urlsplit


class Invalid(ValueError):
    pass


LOCATION = ContextVar("input_location", default="input")
DEVELOPER_PATTERN = r"[A-Z0-9](?:[A-Z0-9._-]{0,30}[A-Z0-9])?"
REQUIREMENT_PATTERN = rf"REQ-(?:[0-9]{{8}}-[0-9]{{3}}|{DEVELOPER_PATTERN}-[0-9]{{8}}-[0-9]{{2,}})"


def developer_id(value):
    value = string(value, "developer ID").upper()
    require(bool(re.fullmatch(DEVELOPER_PATTERN, value)) and ".." not in value, "developer ID (1-32 ASCII characters, alphanumeric ends)")
    return value


def valid_requirement_id(value):
    if not isinstance(value, str) or not re.fullmatch(REQUIREMENT_PATTERN, value):
        return False
    parts = value.rsplit("-", 2)
    try:
        datetime.strptime(parts[-2], "%Y%m%d")
    except ValueError:
        return False
    if int(parts[-1]) < 1:
        return False
    return parts[0] == "REQ" or ".." not in parts[0][4:]


def local_git(repo, *args, allowed=(0,)):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8")
    require(result.returncode in allowed, "Git command failed (check repository/configuration)")
    return result


def init_developer(repo, value):
    value = developer_id(value)
    existing = local_git(repo, "config", "--local", "--get-all", "governance.developerId", allowed=(0, 1))
    if existing.returncode == 0:
        values = existing.stdout.splitlines()
        require(len(values) == 1 and developer_id(values[0]) == value, "developer ID already initialized; change explicitly with git config --local")
    else:
        local_git(repo, "config", "--local", "--replace-all", "governance.developerId", value)
    return {"DeveloperId": value, "Scope": "LOCAL_REPOSITORY"}


def reserve_requirement(repo):
    """Reserve a name atomically across linked worktrees, without creating a requirement."""
    repo = Path(local_git(repo, "rev-parse", "--show-toplevel").stdout.strip())
    config = local_git(repo, "config", "--local", "--get-all", "governance.developerId", allowed=(0, 1))
    require(config.returncode == 0, "developer ID missing; run init-developer --repo PATH --developer-id YOUR_ID")
    values = config.stdout.splitlines()
    require(len(values) == 1, "developer ID ambiguity")
    ident = developer_id(values[0])
    day = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    prefix = f"REQ-{ident}-{day}-"
    common = Path(local_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    common = safe_path(common, common)
    reservations = safe_path(common, common / "governance-id-reservations")
    reservations.mkdir(exist_ok=True)
    used = {path.name.upper() for path in reservations.iterdir()}
    for field in local_git(repo, "worktree", "list", "--porcelain", "-z").stdout.split("\0"):
        if field.startswith("worktree "):
            worktree = Path(field[9:])
            if not worktree.exists():
                continue
            requirements = safe_path(worktree, worktree / "docs/requirements")
            if requirements.is_dir():
                used.update(path.name.upper() for path in requirements.iterdir())
    # Include locally known branch/tag trees, including remote-tracking refs; never fetch.
    revisions = set(local_git(repo, "for-each-ref", "--format=%(objectname)", "refs/heads", "refs/remotes", "refs/tags").stdout.splitlines())
    for revision in revisions:
        listing = local_git(repo, "ls-tree", "-d", "--name-only", f"{revision}:docs/requirements", allowed=(0, 128))
        if listing.returncode == 0:
            used.update(name.upper() for name in listing.stdout.splitlines())
    sequence = 1
    while sequence <= 999999:
        requirement = f"{prefix}{sequence:02d}"
        sequence += 1
        if requirement in used:
            continue
        reservation = safe_path(common, reservations / requirement)
        try:
            reservation.mkdir()
        except FileExistsError:
            continue
        # A crash consumes the number; never recycle a possibly already handed-out ID.
        return {"RequirementId": requirement, "RequirementDirectory": f"docs/requirements/{requirement}",
                "ReservationScope": "LOCAL_REPOSITORY_AND_LINKED_WORKTREES", "GlobalUniqueness": False}
    raise Invalid("Daily requirement sequence exhausted")


def require(ok, field):
    if not ok:
        raise Invalid(f"{LOCATION.get()}: invalid or unsupported field: {field}")


def string(value, field):
    require(isinstance(value, str) and bool(value.strip()), field)
    return value


def array(value, field):
    require(isinstance(value, list), field)
    return value


def exact_object(value, field, keys):
    require(isinstance(value, dict), field)
    require(set(value) == set(keys), field + " fields")
    return value


def unique_strings(value, field, minimum=0):
    values = array(value, field)
    require(len(values) >= minimum, field)
    for item in values:
        string(item, field)
    require(len(values) == len(set(values)), field + " uniqueness")
    return values


def safe_path(root, path):
    root = Path(root).absolute()
    path = Path(path).absolute()
    require(path.is_relative_to(root), "path boundary")
    for item in (path, *path.parents):
        require(not item.is_symlink() and not item.is_junction(), "symbolic path")
    require(path.resolve().is_relative_to(root.resolve()), "resolved path boundary")
    return path


def timestamp(value):
    string(value, "timestamp")
    require(bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value)), "timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Invalid("Invalid timestamp") from None


def timing(payload, created, bounds=None):
    start, end = timestamp(payload.get("StartedAt")), timestamp(payload.get("CompletedAt"))
    duration = payload.get("DurationMs")
    require(type(duration) is int and duration >= 0, "DurationMs")
    require(start <= end <= created and abs((end-start).total_seconds()*1000-duration) <= 1000, "time interval")
    if bounds:
        require(bounds[0] <= start <= end <= bounds[1], "item time interval")
    return start, end


def cell(value):
    if value is None:
        return "—"
    return html.escape(str(value), quote=True).replace("\\", "&#92;").replace("|", "&#124;").replace("`", "&#96;").replace("[", "&#91;").replace("]", "&#93;").replace("\r\n", "<br>").replace("\n", "<br>").replace("\r", "<br>")


def sensitive(value):
    text = json.dumps(value, ensure_ascii=False)
    require(not re.search(r"-----BEGIN .*PRIVATE KEY|(?:password|api[_-]?key|access[_-]?token|cookie|authorization)\s*[\"']?\s*[:=]\s*[\"']?[^\s\"',}]{4,}|Bearer\s+[A-Za-z0-9._-]+", text, re.I), "sensitive content")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


HARD_TRIGGERS = {
    "SECURITY_AUTH_PRIVACY", "REGULATED_LEGAL_FINANCIAL",
    "DESTRUCTIVE_OR_IRREVERSIBLE", "PRODUCTION_RELEASE",
    "BREAKING_SHARED_CONTRACT", "PROTECTED_DATA_MIGRATION",
    "USER_MANDATED_CONTROLLED",
}
UPGRADE_TRIGGERS = {
    "MULTIPLE_WORK_PACKAGES", "SHARED_CONTRACT_NON_BREAKING",
    "CROSS_MODULE_OR_TEAM", "EXTERNAL_INTEGRATION",
    "INDEPENDENT_QUALITY_JUDGMENT", "LEAN_BUDGET_EXCEEDED",
    "LEAN_QUALIFICATION_FAILED", "UNKNOWN_NON_PROTECTED_FACT",
    "USER_MANDATED_STANDARD",
}
LEAN_QUALIFICATIONS = {
    "SINGLE_WORK_PACKAGE", "LOCAL_REVERSIBLE_CHANGE", "NO_SHARED_OR_PROTECTED_SURFACE",
    "NO_EXTERNAL_DB_CI_OR_PRODUCTION", "LOCAL_DETERMINISTIC_BLOCKING_GATES",
    "AT_MOST_EIGHT_BLOCKING_CONTROLS", "WITHIN_LEAN_TIME_BUDGET",
}
PROFILE_RANK = {"LEAN": 0, "STANDARD": 1, "CONTROLLED": 2}
BUSINESS_IDENTITY_IGNORABLES = {"\u200b", "\ufeff"}


def load_contract(path, label):
    path = Path(path).absolute()
    LOCATION.set(label)
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object)
    except (json.JSONDecodeError, UnicodeError):
        raise Invalid(f"{LOCATION.get()}: invalid JSON encoding or syntax") from None
    require(isinstance(value, dict), label)
    sensitive(value)
    return value


def _json_equal(left, right):
    """Compare JSON values without Python's bool/int equality shortcut."""
    return type(left) is type(right) and left == right


def _schema_type_matches(value, expected):
    return {
        "object": isinstance(value, dict), "array": isinstance(value, list),
        "string": isinstance(value, str), "integer": type(value) is int,
        "number": type(value) in (int, float), "boolean": type(value) is bool,
        "null": value is None,
    }.get(expected, False)


def validate_schema_instance(instance, schema, label="instance"):
    """Validate the Draft 2020-12 subset used by the v3 governance contracts."""
    root = schema

    def visit(value, rule, location):
        require(isinstance(rule, dict), f"{location} schema")
        if "$ref" in rule:
            reference = rule["$ref"]
            require(isinstance(reference, str) and reference.startswith("#/"), f"{location} $ref")
            target = root
            for part in reference[2:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                require(isinstance(target, dict) and part in target, f"{location} $ref")
                target = target[part]
            visit(value, target, location)
        expected = rule.get("type")
        if expected is not None:
            types = expected if isinstance(expected, list) else [expected]
            require(bool(types) and all(isinstance(item, str) for item in types), f"{location} schema type")
            require(any(_schema_type_matches(value, item) for item in types), f"{location} schema type")
        if "const" in rule:
            require(_json_equal(value, rule["const"]), f"{location} schema const")
        if "enum" in rule:
            require(any(_json_equal(value, item) for item in rule["enum"]), f"{location} schema enum")
        if isinstance(value, str):
            if "minLength" in rule:
                require(len(value) >= rule["minLength"], f"{location} schema minLength")
            if "pattern" in rule:
                require(re.search(rule["pattern"], value) is not None, f"{location} schema pattern")
        if type(value) in (int, float) and "minimum" in rule:
            require(value >= rule["minimum"], f"{location} schema minimum")
        if isinstance(value, list):
            if "minItems" in rule:
                require(len(value) >= rule["minItems"], f"{location} schema minItems")
            if "maxItems" in rule:
                require(len(value) <= rule["maxItems"], f"{location} schema maxItems")
            if rule.get("uniqueItems"):
                encoded = [json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for item in value]
                require(len(encoded) == len(set(encoded)), f"{location} schema uniqueItems")
            if "items" in rule:
                for index, item in enumerate(value):
                    visit(item, rule["items"], f"{location}[{index}]")
        if isinstance(value, dict):
            required = rule.get("required", [])
            require(all(key in value for key in required), f"{location} schema required")
            properties = rule.get("properties", {})
            for key, child in properties.items():
                if key in value:
                    visit(value[key], child, f"{location}.{key}")
            if rule.get("additionalProperties") is False:
                require(set(value) <= set(properties), f"{location} schema additionalProperties")
        for child in rule.get("allOf", []):
            visit(value, child, location)
        if "if" in rule:
            try:
                visit(value, rule["if"], location)
                branch = rule.get("then")
            except Invalid:
                branch = rule.get("else")
            if branch is not None:
                visit(value, branch, location)

    previous = LOCATION.get()
    try:
        LOCATION.set(label)
        visit(instance, schema, label)
    finally:
        LOCATION.set(previous)


def validate_contract_schema(instance, repo, schema_name, label):
    schema_path = safe_path(repo, Path(repo) / "docs/Agent治理/contracts" / schema_name)
    require(schema_path.is_file(), f"{label} schema file")
    schema = load_contract(schema_path, f"{label} schema")
    require(schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema", f"{label} schema dialect")
    validate_schema_instance(instance, schema, label)


def repository_root(path):
    for candidate in (Path(path).absolute().parent, *Path(path).absolute().parents):
        if (candidate / "AGENTS.md").is_file():
            return candidate
    raise Invalid(f"{LOCATION.get()}: invalid or unsupported field: repository root")


def reference_path(repo, containing_file, reference):
    """Resolve a repository reference; nested relative paths stay beside the contract."""
    plain = reference.split("#", 1)[0].replace("\\", "/")
    require(bool(plain) and not urlsplit(plain).scheme, "reference")
    candidate = Path(plain)
    parts = candidate.parts
    if candidate.is_absolute() or (parts and parts[0] in ("docs", "scripts", ".codex")) or plain == "AGENTS.md":
        return safe_path(repo, candidate if candidate.is_absolute() else repo / candidate)
    return safe_path(repo, Path(containing_file).parent / candidate)


def validate_file_reference(repo, containing_file, reference, field):
    string(reference, field)
    path = reference_path(repo, containing_file, reference)
    require(path.is_file(), field)
    parts = reference.split("#", 1)
    if len(parts) == 2:
        require(path.suffix.lower() == ".md", field + " anchor target")
        markdown_section(path.read_text(encoding="utf-8-sig"), parts[1])
    return path


def validate_control_source_ref(repo, containing_file, reference, selected_profile):
    string(reference, "SourceRefs")
    if reference.startswith("RULE:"):
        rule = reference[5:]
        require(bool(re.fullmatch(r"[A-Z]{2,8}-\d{3}", rule)), "SourceRefs RuleId")
        documents = (Path(repo) / "docs/开发规范").glob("*.md")
        defined = set()
        for path in documents:
            defined.update(re.findall(r"(?m)^\|\s*`?([A-Z]{2,8}-\d{3})`?\s*\|", path.read_text(encoding="utf-8-sig")))
        require(rule in defined, "SourceRefs RuleId existence")
    elif reference.startswith("PROFILE:"):
        require(reference[8:] == selected_profile, "SourceRefs Profile")
    elif reference.startswith(("RISK:", "USER:")):
        target = reference.split(":", 1)[1]
        require("#" in target, "SourceRefs stable anchor")
        validate_file_reference(repo, containing_file, target, "SourceRefs")
    else:
        validate_file_reference(repo, containing_file, reference, "SourceRefs")


def git_text(repo, revision, repository_path, field):
    require(bool(re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", revision)), field + " revision")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", revision, "HEAD"], cwd=repo,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    require(ancestor.returncode == 0, field + " revision ancestry")
    completed = subprocess.run(
        ["git", "show", f"{revision}:{repository_path}"], cwd=repo,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    require(completed.returncode == 0, field + " revision binding")
    try:
        return completed.stdout.decode("utf-8-sig")
    except UnicodeError:
        raise Invalid(f"{LOCATION.get()}: invalid or unsupported field: {field} encoding") from None


def git_contract(repo, revision, path, field):
    repository_path = Path(path).resolve().relative_to(Path(repo).resolve()).as_posix()
    text = git_text(repo, revision, repository_path, field)
    LOCATION.set(field)
    try:
        value = json.loads(text, object_pairs_hook=unique_object)
    except json.JSONDecodeError:
        raise Invalid(f"{field}: invalid JSON encoding or syntax") from None
    require(isinstance(value, dict), field)
    sensitive(value)
    return value


def head_revision(repo):
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, check=False, text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def head_text(repo, path):
    try:
        relative = Path(path).resolve().relative_to(Path(repo).resolve()).as_posix()
    except ValueError:
        return None
    completed = subprocess.run(
        ["git", "show", f"HEAD:{relative}"], cwd=repo, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, check=False,
    )
    return completed.stdout if completed.returncode == 0 else None


def validate_evaluations(items, field, allowed, result_key, repo, control_path):
    seen = set()
    for index, item in enumerate(array(items, field)):
        LOCATION.set(f"ControlProfile.{field}[{index}]")
        exact_object(item, field, ("Code", result_key, "EvidenceRefs"))
        code = string(item.get("Code"), "Code")
        require(code in allowed and code not in seen, "Code")
        seen.add(code)
        require(type(item.get(result_key)) is bool, result_key)
        refs = unique_strings(item.get("EvidenceRefs"), "EvidenceRefs", 1)
        for ref in refs:
            validate_file_reference(repo, control_path, ref, "EvidenceRefs")
    require(seen == allowed, field + " completeness")
    return items


def validate_control_profile(data, control_path):
    LOCATION.set("ControlProfile")
    current = data.get("GovernanceVersion") == "v3.0.1"
    exact_object(data, "ControlProfile", (
        "SchemaVersion", "GovernanceVersion", "DecisionRulesVersion", "RequirementId",
        "ProfileVersion", "SelectedProfile", "MinimumProfile", "PreviousProfileRef", "PreviousProfileRevision",
        "Classification", "BusinessAcceptanceSources", "ExecutionBudget", "PlatformScope",
        "RecordRefs",
    ) + (("BaselineBindings",) if current else ()))
    for key in ("SchemaVersion", "GovernanceVersion", "DecisionRulesVersion"):
        require(data.get(key) == ("v3.0.1" if current else "v3.0"), key)
    requirement = string(data.get("RequirementId"), "RequirementId")
    require(valid_requirement_id(requirement), "RequirementId")
    require(bool(re.fullmatch(r"v[1-9]\d*\.\d+", string(data.get("ProfileVersion"), "ProfileVersion"))), "ProfileVersion")
    selected, minimum = data.get("SelectedProfile"), data.get("MinimumProfile")
    require(selected in PROFILE_RANK, "SelectedProfile")
    require(minimum in PROFILE_RANK, "MinimumProfile")
    previous_ref = data.get("PreviousProfileRef")
    require(previous_ref is None or bool(string(previous_ref, "PreviousProfileRef")), "PreviousProfileRef")
    previous_revision = data.get("PreviousProfileRevision")
    require(previous_revision is None or bool(re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", string(previous_revision, "PreviousProfileRevision"))), "PreviousProfileRevision")
    require((previous_ref is None) == (previous_revision is None) == (data["ProfileVersion"] == "v1.0"), "initial profile and previous binding")

    repo = repository_root(control_path)
    if current:
        validate_baseline_bindings(data["BaselineBindings"], repo, control_path)

    classification = exact_object(data.get("Classification"), "Classification", (
        "HardTriggers", "UpgradeTriggers", "LeanQualifications", "Unknowns", "DecisionRefs",
    ))
    hard = validate_evaluations(classification["HardTriggers"], "HardTriggers", HARD_TRIGGERS, "Matched", repo, control_path)
    upgrades = validate_evaluations(classification["UpgradeTriggers"], "UpgradeTriggers", UPGRADE_TRIGGERS, "Matched", repo, control_path)
    qualifications = validate_evaluations(classification["LeanQualifications"], "LeanQualifications", LEAN_QUALIFICATIONS, "Satisfied", repo, control_path)
    unknowns = array(classification["Unknowns"], "Unknowns")
    controlled_unknown = False
    for index, item in enumerate(unknowns):
        LOCATION.set(f"ControlProfile.Unknowns[{index}]")
        exact_object(item, "Unknown", ("Code", "CouldBeControlled", "EvidenceRefs"))
        string(item.get("Code"), "Code")
        require(type(item.get("CouldBeControlled")) is bool, "CouldBeControlled")
        controlled_unknown = controlled_unknown or item["CouldBeControlled"]
        refs = unique_strings(item.get("EvidenceRefs"), "EvidenceRefs", 1)
        for ref in refs:
            validate_file_reference(repo, control_path, ref, "EvidenceRefs")
    decision_refs = unique_strings(classification["DecisionRefs"], "DecisionRefs", 1)
    for ref in decision_refs:
        validate_file_reference(repo, control_path, ref, "DecisionRefs")

    calculated = "LEAN"
    if any(item["Matched"] for item in hard) or controlled_unknown:
        calculated = "CONTROLLED"
    elif (any(item["Matched"] for item in upgrades)
          or any(not item["Satisfied"] for item in qualifications) or unknowns):
        calculated = "STANDARD"
    if PROFILE_RANK[minimum] > PROFILE_RANK[calculated]:
        calculated = minimum
    require(selected == calculated, "SelectedProfile deterministic result")

    sources = array(data.get("BusinessAcceptanceSources"), "BusinessAcceptanceSources")
    require(bool(sources), "BusinessAcceptanceSources")
    source_map, source_identities = {}, set()
    for index, source in enumerate(sources):
        LOCATION.set(f"ControlProfile.BusinessAcceptanceSources[{index}]")
        exact_object(source, "BusinessAcceptanceSource", (
            "AcceptanceSourceId", "SourceKind", "SourceRef", "AcceptanceText", "ApprovalRef",
            "SourceArtifactVersion", "SourceDigest", "ApprovalDigest", "ProvenanceRevision",
        ))
        ident = string(source.get("AcceptanceSourceId"), "AcceptanceSourceId")
        require(bool(re.fullmatch(r"BAS-\d{3}", ident)) and ident not in source_map, "AcceptanceSourceId")
        require(source.get("SourceKind") in ("USER_REQUIREMENT", "FORMAL_BUSINESS_CONTRACT"), "SourceKind")
        string(source.get("SourceRef"), "SourceRef")
        string(source.get("AcceptanceText"), "AcceptanceText")
        string(source.get("ApprovalRef"), "ApprovalRef")
        require(bool(re.fullmatch(r"v[1-9]\d*\.\d+", string(source.get("SourceArtifactVersion"), "SourceArtifactVersion"))), "SourceArtifactVersion")
        for digest_field in ("SourceDigest", "ApprovalDigest"):
            require(bool(re.fullmatch(r"sha256:[0-9a-f]{64}", string(source.get(digest_field), digest_field))), digest_field)
        revision = source.get("ProvenanceRevision")
        require(revision is None or bool(re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", string(revision, "ProvenanceRevision"))), "ProvenanceRevision")
        identity = business_identity(source["AcceptanceText"])
        require(identity not in source_identities, "duplicate business acceptance source")
        source_identities.add(identity)
        source_map[ident] = source

    budget = exact_object(data.get("ExecutionBudget"), "ExecutionBudget", (
        "PlanningMinutes", "ImplementationMinutes", "ImplementationAndControlsMinutes",
        "BlockingControlCount", "AtomicRiskTargetCount", "BlockingControlsMinutes",
        "AdvisoryMinutes", "WorkPackageCount",
    ))
    for key in budget:
        require(type(budget[key]) is int and budget[key] >= (1 if key == "WorkPackageCount" else 0), key)
    upgrade_map = {item["Code"]: item["Matched"] for item in upgrades}
    qualification_map = {item["Code"]: item["Satisfied"] for item in qualifications}
    require(upgrade_map["MULTIPLE_WORK_PACKAGES"] == (budget["WorkPackageCount"] >= 2), "MULTIPLE_WORK_PACKAGES consistency")
    require(upgrade_map["LEAN_BUDGET_EXCEEDED"] == (budget["PlanningMinutes"] > 5 or budget["ImplementationAndControlsMinutes"] > 20), "LEAN_BUDGET_EXCEEDED consistency")
    require(upgrade_map["LEAN_QUALIFICATION_FAILED"] == any(not value for value in qualification_map.values()), "LEAN_QUALIFICATION_FAILED consistency")
    require(qualification_map["SINGLE_WORK_PACKAGE"] == (budget["WorkPackageCount"] == 1), "SINGLE_WORK_PACKAGE consistency")
    require(qualification_map["AT_MOST_EIGHT_BLOCKING_CONTROLS"] == (
        budget["BlockingControlCount"] <= 8 and budget["AtomicRiskTargetCount"] <= 8
    ), "AT_MOST_EIGHT_BLOCKING_CONTROLS consistency")
    require(qualification_map["WITHIN_LEAN_TIME_BUDGET"] == (budget["PlanningMinutes"] <= 5 and budget["ImplementationAndControlsMinutes"] <= 20), "WITHIN_LEAN_TIME_BUDGET consistency")
    platform = exact_object(data.get("PlatformScope"), "PlatformScope", (
        "LocalDevelopment", "DeliveryValidation", "DeliveryValidationStatus", "CodexDesktopMultiAgent",
    ))
    local = unique_strings(platform.get("LocalDevelopment"), "LocalDevelopment", 1)
    require(set(local) <= {"WINDOWS", "MACOS", "LINUX"}, "LocalDevelopment")
    delivery = platform.get("DeliveryValidation")
    delivery_status = platform.get("DeliveryValidationStatus")
    require(delivery in {"NOT_APPLICABLE", "PROJECT_DEFINED", "WINDOWS", "MACOS", "LINUX", "LINUX_PRODUCTION", "CROSS_PLATFORM"}, "DeliveryValidation")
    require(delivery_status in {"AVAILABLE", "NOT_AVAILABLE", "NOT_APPLICABLE"}, "DeliveryValidationStatus")
    require((delivery == "NOT_APPLICABLE") == (delivery_status == "NOT_APPLICABLE"), "delivery validation applicability")
    require(type(platform.get("CodexDesktopMultiAgent")) is bool, "CodexDesktopMultiAgent")
    records = unique_strings(data.get("RecordRefs"), "RecordRefs", 5)
    record_names = {Path(ref).name for ref in records}
    require(len(record_names) == len(records), "RecordRefs file-name uniqueness")
    require({"需求基线.md", "control-profile.json", "acceptance-plan.json", "开发上下文包.md", "开发记录.md"} <= record_names, "RecordRefs minimum set")
    if selected == "LEAN":
        require(not ({"临时质量验证记录.md", "只读评审记录.md"} & record_names), "LEAN empty quality records forbidden")
        require(budget["PlanningMinutes"] <= 5, "PlanningMinutes")
        require(budget["ImplementationAndControlsMinutes"] <= 20, "ImplementationAndControlsMinutes")
        require(budget["BlockingControlCount"] <= 8, "BlockingControlCount")
        require(budget["AtomicRiskTargetCount"] <= 8, "AtomicRiskTargetCount")
        require(budget["WorkPackageCount"] == 1, "WorkPackageCount")
    else:
        require({"临时质量验证记录.md", "只读评审记录.md"} <= record_names, "independent quality RecordRefs")
    return source_map


def normalized(text):
    return unicodedata.normalize("NFC", text).strip()


def business_identity(text):
    text = unicodedata.normalize("NFKC", text)
    text = "".join(character for character in text if character not in BUSINESS_IDENTITY_IGNORABLES)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", text)
    return re.sub(r"[，,；;。.!！?？：:]+$", "", text).rstrip()


def command_identity(text):
    """Normalize command spelling only; do not infer command semantics."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def content_digest(text):
    canonical = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_baseline_bindings(bindings, repo, control_path):
    exact_object(bindings, "BaselineBindings", ("Governance", "BusinessRevision", "Testing"))
    revision = string(bindings["BusinessRevision"], "BusinessRevision")
    require(bool(re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", revision)), "BusinessRevision")
    local_git(repo, "cat-file", "-e", revision + "^{commit}")
    for key in ("Governance", "Testing"):
        binding = exact_object(bindings[key], key + " binding", ("Version", "SourceRef", "SourceDigest", "Revision"))
        require(binding["Version"] == "v3.0.1", key + " Version")
        path = validate_file_reference(repo, control_path, binding["SourceRef"], key + " SourceRef")
        expected = "docs/治理版本说明/v3.0.1治理修订说明.md" if key == "Governance" else "docs/开发规范/测试规范.md"
        require(path.resolve() == (repo / expected).resolve(), key + " canonical source")
        require(path.suffix == ".md", key + " Markdown source")
        text = (git_text(repo, binding["Revision"], path.relative_to(repo).as_posix(), key)
                if binding["Revision"] is not None else path.read_text(encoding="utf-8-sig"))
        require(content_digest(text) == binding["SourceDigest"], key + " SourceDigest")
        require(bool(re.search(r"(?m)^版本[：:]\s*v3\.0\.1(?:[；;\s]|$)", text)), key + " document version")
    require(bindings["Governance"]["SourceRef"] != bindings["Testing"]["SourceRef"], "independent governance/testing bindings")


def artifact_version(text):
    versions = set(re.findall(r"(?:产物版本|ArtifactVersion)[^\n]*?\b(v[1-9]\d*\.\d+)\b", text))
    require(len(versions) == 1, "source ArtifactVersion metadata")
    return next(iter(versions))


def markdown_section(text, fragment):
    if not fragment:
        return text
    lines = text.splitlines()
    counts = {}
    start = level = None
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if not match:
            continue
        slug = re.sub(r"[^\w\-\s]", "", match.group(2).lower()).replace(" ", "-")
        number = counts.get(slug, 0)
        counts[slug] = number + 1
        slug = slug + (f"-{number}" if number else "")
        if slug == unquote(fragment):
            start, level = index + 1, len(match.group(1))
            break
    require(start is not None, "SourceRef anchor")
    end = len(lines)
    for index in range(start, len(lines)):
        match = re.match(r"^(#{1,6})\s+", lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    return "\n".join(lines[start:end])


def markdown_business_units(text, fragment=None):
    """Return complete prose/list/table units and explicitly delimited clauses."""
    if fragment == "1-原始需求":
        text = "\n".join(line if re.match(r"^\s*>", line) else "" for line in text.splitlines())
    elif fragment == "4-业务结果与用户明确边界":
        allowed = []
        acceptance_column = None
        for raw_line in text.splitlines():
            line = re.sub(r"^(?:\s*>\s*)+", "", raw_line).strip()
            match = re.match(r"^-\s+\*\*唯一业务结果：\*\*\s*(.+)$", line)
            if match:
                allowed.append(match.group(1))
                continue
            if "|" not in line:
                acceptance_column = None
                continue
            cells = [item.strip() for item in line.strip("|").split("|")]
            if cells and cells[0] == "用户明确的业务边界 / 约束":
                acceptance_column = 0
                continue
            if [item.replace("`", "") for item in cells] == ["AcceptanceSourceId", "业务验收原文", "SourceKind", "明确依据 / SourceRef"]:
                acceptance_column = 1
                continue
            if acceptance_column is not None and cells and not all(re.fullmatch(r":?-{3,}:?", item) for item in cells):
                if acceptance_column == 1:
                    # Only acceptance text is a business source; IDs and evidence are not.
                    if len(cells) != 4 or not re.fullmatch(r"BAS-\d{3}", cells[0].strip("`")):
                        acceptance_column = None
                        continue
                allowed.append(cells[acceptance_column])
        text = "\n\n".join(allowed)
    units = set()
    paragraph = []

    def add(value):
        value = re.sub(r"\s+", " ", normalized(value))
        if not value:
            return
        units.add(value)
        units.add(value.rstrip("\uff0c,\uff1b;\u3002\uff01!\uff1f?").rstrip())
        for clause in re.split(r"(?<=[\uff0c,\uff1b;\u3002\uff01!\uff1f?])", value):
            clause = clause.strip()
            if clause:
                units.add(clause)
                units.add(clause.rstrip("\uff0c,\uff1b;\u3002\uff01!\uff1f?").rstrip())

    def flush():
        if paragraph:
            add(" ".join(paragraph))
            paragraph.clear()

    for raw_line in text.splitlines():
        line = re.sub(r"^(?:\s*>\s*)+", "", raw_line).strip()
        if not line:
            flush()
            continue
        if re.match(r"^#{1,6}\s+", line):
            flush()
            continue
        if "|" in line:
            flush()
            cells = [item.strip() for item in line.strip("|").split("|")]
            if cells and all(re.fullmatch(r":?-{3,}:?", item) for item in cells):
                continue
            for item in cells:
                add(item)
            continue
        match = re.match(r"^(?:[-+*]|\d+[.)])\s+(.+)$", line)
        if match:
            flush()
            add(match.group(1))
            continue
        paragraph.append(line)
    flush()
    units.discard("")
    return units


EVENT_FIELDS = (
    "SchemaVersion", "RequirementId", "ContextPackVersion", "RepairLineageRef",
    "EventId", "EventType", "CreatedAt", "Actor", "CandidateRef", "Payload",
    "EvidenceRefs", "SupersedesEventRef", "RedactionState",
)


def validate_event_structure(event, requirement, relative_path=None):
    exact_object(event, "event", EVENT_FIELDS)
    require(event["SchemaVersion"] == "v1.1", "SchemaVersion")
    require(event["RequirementId"] == requirement, "RequirementId")
    require(event["RedactionState"] in ("SAFE", "REDACTED"), "RedactionState")
    require(isinstance(event["Payload"], dict), "Payload")
    require(event["Payload"].get("FactProtocolVersion", "v2.1") == "v2.1", "FactProtocolVersion")
    for key in ("Actor", "EventType"):
        string(event[key], key)
    evidence = array(event["EvidenceRefs"], "EvidenceRefs")
    for ref in evidence:
        string(ref, "EvidenceRefs")
    timestamp(event["CreatedAt"])
    sensitive(event)
    ident = string(event["EventId"], "EventId")
    if relative_path is not None:
        require(Path(relative_path).suffix == ".json" and Path(relative_path).stem == ident, "EventId filename binding")
    return ident


def human_approval_reference(value):
    value = string(value, "ApprovalRef human evidence")
    prefix = "current-user-message:"
    require(value.startswith(prefix), "ApprovalRef human evidence location")
    timestamp(value[len(prefix):])
    return value


def validate_source_provenance(profile, control_path):
    repo = repository_root(control_path)
    requirement = profile["RequirementId"]
    expected_baselines = {
        f"docs/requirements/{requirement}/需求基线.md",
        f"docs/requirements/{requirement}/REQUIREMENT_BASELINE.md",
    }
    for index, source in enumerate(profile["BusinessAcceptanceSources"]):
        LOCATION.set(f"ControlProfile.BusinessAcceptanceSources[{index}]")
        source_parts = source["SourceRef"].split("#", 1)
        source_ref = source_parts[0].replace("\\", "/")
        source_path = safe_path(repo, repo / source_ref)
        require(source_path.is_file(), "SourceRef")
        revision = source["ProvenanceRevision"]
        source_text = (git_text(repo, revision, source_ref, "SourceRef") if revision
                       else source_path.read_text(encoding="utf-8-sig"))
        require(content_digest(source_text) == source["SourceDigest"], "SourceDigest binding")
        require(artifact_version(source_text) == source["SourceArtifactVersion"], "source ArtifactVersion binding")
        source_section = markdown_section(source_text, source_parts[1] if len(source_parts) == 2 else "")
        source_fragment = unquote(source_parts[1]) if len(source_parts) == 2 else None
        acceptance_text = business_identity(source["AcceptanceText"])
        source_units = {business_identity(item) for item in markdown_business_units(source_section, source_fragment)}
        require(acceptance_text in source_units, "AcceptanceText complete business unit provenance")
        approval_ref = source["ApprovalRef"].split("#", 1)[0].replace("\\", "/")
        approval_path = safe_path(repo, repo / approval_ref)
        require(approval_path.is_file(), "ApprovalRef")
        approval_text = (git_text(repo, revision, approval_ref, "ApprovalRef") if revision
                         else approval_path.read_text(encoding="utf-8-sig"))
        require(content_digest(approval_text) == source["ApprovalDigest"], "ApprovalDigest binding")
        try:
            approval = json.loads(approval_text, object_pairs_hook=unique_object)
        except json.JSONDecodeError:
            raise Invalid(f"{LOCATION.get()}: invalid JSON encoding or syntax") from None
        require(isinstance(approval, dict), "ApprovalRef")
        sensitive(approval)
        if source["SourceKind"] == "USER_REQUIREMENT":
            require(source_ref in expected_baselines, "SourceRef requirement binding")
            require(len(source_parts) == 2 and unquote(source_parts[1]) in ("1-原始需求", "4-业务结果与用户明确边界"), "SourceRef business section")
            expected_approval_root = f"docs/requirements/{requirement}/facts/gates/"
            require(approval_ref.startswith(expected_approval_root) and "/" not in approval_ref[len(expected_approval_root):], "ApprovalRef G1 facts/gates location")
            validate_event_structure(approval, requirement, approval_ref)
            require(approval["ContextPackVersion"] is None, "ApprovalRef ContextPackVersion")
            require(approval["RepairLineageRef"] is None, "ApprovalRef RepairLineageRef")
            require(approval["CandidateRef"] is None, "ApprovalRef CandidateRef")
            require(approval["SupersedesEventRef"] is None, "ApprovalRef SupersedesEventRef")
            require(bool(approval["EvidenceRefs"]), "ApprovalRef EvidenceRefs")
            payload = approval.get("Payload")
            exact_object(payload, "ApprovalRef Payload", (
                "Gate", "ArtifactRef", "ArtifactVersion", "Decision", "DecisionMode",
                "BasisRefs", "ApprovalRef", "BlockingItems",
            ))
            require(approval.get("EventType") == "GATE_DECISION", "ApprovalRef EventType")
            require(isinstance(payload, dict) and payload.get("Gate") == "G1" and payload.get("Decision") == "CONFIRMED" and payload.get("DecisionMode") == "HUMAN", "ApprovalRef G1 CONFIRMED")
            require(payload.get("ArtifactRef", "").replace("\\", "/") == source_ref, "ApprovalRef ArtifactRef")
            require(payload.get("ArtifactVersion") == source["SourceArtifactVersion"], "ApprovalRef ArtifactVersion")
            unique_strings(payload.get("BasisRefs"), "ApprovalRef BasisRefs", minimum=1)
            require(array(payload.get("BlockingItems"), "ApprovalRef BlockingItems") == [], "ApprovalRef BlockingItems CONFIRMED")
            human_approval_reference(payload.get("ApprovalRef"))
        else:
            allowed_contract_roots = (
                "docs/business-contracts/",
                f"docs/requirements/{requirement}/contracts/",
            )
            require(source_ref.startswith(allowed_contract_roots), "formal contract source location")
            require(approval.get("RequirementId") == requirement, "formal contract RequirementId")
            require(approval.get("ApprovalType") == "BUSINESS_CONTRACT_APPROVAL", "formal contract ApprovalType")
            require(approval.get("ApprovalStatus") == "APPROVED", "formal contract approval")
            require(approval.get("DecisionMode") == "HUMAN", "formal contract DecisionMode")
            string(approval.get("ApprovedBy"), "formal contract ApprovedBy")
            timestamp(approval.get("ApprovedAt"))
            string(approval.get("ApprovalRef"), "formal contract human evidence")
            require(approval.get("ContractRef", "").split("#", 1)[0].replace("\\", "/") == source_ref, "formal contract binding")
            require(approval.get("ContractVersion") == source["SourceArtifactVersion"], "formal contract version binding")


def validate_profile_chain(profile, control_path, repo, seen=None):
    seen = set() if seen is None else seen
    if profile["PreviousProfileRef"] is None:
        return
    previous_path = reference_path(repo, control_path, profile["PreviousProfileRef"])
    previous_relative = previous_path.resolve().relative_to(Path(repo).resolve()).as_posix()
    identity = (profile["PreviousProfileRevision"].lower(), previous_relative)
    require(identity not in seen, "PreviousProfileRef cycle")
    seen.add(identity)
    previous_text = git_text(repo, profile["PreviousProfileRevision"], previous_relative, "PreviousProfileRef")
    LOCATION.set("PreviousProfileRef")
    try:
        previous = json.loads(previous_text, object_pairs_hook=unique_object)
    except json.JSONDecodeError:
        raise Invalid("PreviousProfileRef: invalid JSON encoding or syntax") from None
    require(isinstance(previous, dict), "PreviousProfileRef")
    sensitive(previous)
    validate_contract_schema(previous, repo, "control-profile.schema.json", "PreviousProfile")
    validate_control_profile(previous, previous_path)
    validate_source_provenance(previous, previous_path)
    require(previous.get("RequirementId") == profile["RequirementId"], "PreviousProfileRef RequirementId")
    require(PROFILE_RANK[profile["SelectedProfile"]] >= PROFILE_RANK[previous["SelectedProfile"]], "profile downgrade forbidden in v3.0 phase 1")
    require(PROFILE_RANK[profile["MinimumProfile"]] >= PROFILE_RANK[previous["MinimumProfile"]], "MinimumProfile downgrade forbidden")
    previous_hard = {item["Code"] for item in previous["Classification"]["HardTriggers"] if item["Matched"]}
    current_hard = {item["Code"] for item in profile["Classification"]["HardTriggers"] if item["Matched"]}
    require(previous_hard <= current_hard, "matched hard trigger removal forbidden in phase 1")
    previous_sources = {item["AcceptanceSourceId"]: item for item in previous["BusinessAcceptanceSources"]}
    current_sources = {item["AcceptanceSourceId"]: item for item in profile["BusinessAcceptanceSources"]}
    require(all(current_sources.get(ident) == source for ident, source in previous_sources.items()), "business acceptance source removal or change forbidden")
    current_version = tuple(map(int, profile["ProfileVersion"][1:].split(".")))
    previous_version = tuple(map(int, previous["ProfileVersion"][1:].split(".")))
    require(current_version > previous_version, "ProfileVersion progression")
    validate_profile_chain(previous, previous_path, repo, seen)


def validate_acceptance_plan(data, profile, sources, acceptance_path):
    LOCATION.set("AcceptancePlan")
    exact_object(data, "AcceptancePlan", (
        "SchemaVersion", "GovernanceVersion", "RequirementId", "PlanVersion",
        "ControlProfileRef", "PreviousPlanRef", "PreviousPlanRevision", "Controls",
    ))
    require(data.get("SchemaVersion") == data.get("GovernanceVersion") == profile["GovernanceVersion"], "version")
    require(data.get("RequirementId") == profile.get("RequirementId"), "RequirementId binding")
    require(bool(re.fullmatch(r"v[1-9]\d*\.\d+", string(data.get("PlanVersion"), "PlanVersion"))), "PlanVersion")
    require(data["PlanVersion"] == profile["ProfileVersion"], "PlanVersion/ProfileVersion binding")
    string(data.get("ControlProfileRef"), "ControlProfileRef")
    previous_ref, previous_revision = data.get("PreviousPlanRef"), data.get("PreviousPlanRevision")
    require(previous_ref is None or bool(string(previous_ref, "PreviousPlanRef")), "PreviousPlanRef")
    require(previous_revision is None or bool(re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", string(previous_revision, "PreviousPlanRevision"))), "PreviousPlanRevision")
    require((previous_ref is None) == (previous_revision is None) == (data["PlanVersion"] == "v1.0"), "initial plan and previous binding")
    require(previous_revision == profile["PreviousProfileRevision"], "plan/profile previous revision binding")
    controls = array(data.get("Controls"), "Controls")
    require(bool(controls), "Controls")
    identifiers, consumed_ba, control_identities, command_identities = set(), set(), set(), set()
    blocking_controls = 0
    blocking_minutes = 0
    advisory_minutes = 0
    risk_targets = set()
    repo = repository_root(acceptance_path)
    local_platforms = set(profile["PlatformScope"]["LocalDevelopment"])
    for index, item in enumerate(controls):
        LOCATION.set(f"AcceptancePlan.Controls[{index}]")
        exact_object(item, "Control", (
            "ControlId", "Class", "Statement", "SourceRefs", "AcceptanceSourceRef",
            "Triggered", "Blocking", "Owner", "MethodKind", "Method", "ExecutionPlatform",
            "EstimatedMinutes", "RiskTargetId",
        ))
        ident = string(item.get("ControlId"), "ControlId")
        kind = item.get("Class")
        require(kind in ("BA", "RB", "QG", "AD"), "Class")
        require(bool(re.fullmatch(kind + r"-\d{3}", ident)) and ident not in identifiers, "ControlId")
        identifiers.add(ident)
        statement = string(item.get("Statement"), "Statement")
        if kind != "BA":
            control_identity = (kind, business_identity(statement))
            require(control_identity not in control_identities, "duplicate control semantic identity")
            control_identities.add(control_identity)
        refs = unique_strings(item.get("SourceRefs"), "SourceRefs")
        require(type(item.get("Triggered")) is bool and type(item.get("Blocking")) is bool, "Triggered/Blocking")
        require(item.get("Owner") in ("WRITER", "QA", "REVIEWER", "HUMAN_ACCEPTANCE"), "Owner")
        require(item.get("MethodKind") in ("NONE", "COMMAND", "DETERMINISTIC_ASSERTION", "STATIC_REVIEW", "ADVISORY"), "MethodKind")
        require(item.get("ExecutionPlatform") in ("NONE", "WINDOWS", "MACOS", "LINUX"), "ExecutionPlatform")
        execution_platform = item["ExecutionPlatform"]
        require(execution_platform == "NONE" or execution_platform in local_platforms, "local platform scope")
        minutes = item.get("EstimatedMinutes")
        require(type(minutes) is int and minutes >= 0, "EstimatedMinutes")
        risk_target = item.get("RiskTargetId")
        require(risk_target is None or bool(re.fullmatch(r"RISK-[A-Z0-9][A-Z0-9_-]*", string(risk_target, "RiskTargetId"))), "RiskTargetId")
        if kind == "BA":
            source_ref = item.get("AcceptanceSourceRef")
            require(source_ref in sources and source_ref not in consumed_ba, "AcceptanceSourceRef")
            consumed_ba.add(source_ref)
            require(business_identity(statement) == business_identity(sources[source_ref]["AcceptanceText"]), "BA source text equality")
            require(not refs, "BA SourceRefs forbidden")
            require(item["Triggered"] and item["Blocking"], "BA blocking invariant")
            require(item["Owner"] == "HUMAN_ACCEPTANCE", "BA Owner")
            require(item.get("MethodKind") == "NONE" and item.get("Method") is None and item.get("ExecutionPlatform") == "NONE" and minutes == 0 and risk_target is None, "BA technical fields forbidden")
        elif kind in ("RB", "QG"):
            require(item.get("AcceptanceSourceRef") is None, "AcceptanceSourceRef")
            require(bool(refs), "SourceRefs")
            for ref in refs:
                validate_control_source_ref(repo, acceptance_path, ref, profile["SelectedProfile"])
            require(item["Blocking"] == item["Triggered"], "blocking trigger invariant")
            allowed_owners = ("WRITER", "QA", "REVIEWER") if kind == "RB" else ("WRITER", "QA")
            require(item["Owner"] in allowed_owners, kind + " Owner")
            if item["Triggered"]:
                allowed_methods = ("STATIC_REVIEW",) if item["Owner"] == "REVIEWER" else ("COMMAND", "DETERMINISTIC_ASSERTION")
                require(item["MethodKind"] in allowed_methods, "MethodKind")
                method = string(item.get("Method"), "Method")
                require(item["ExecutionPlatform"] != "NONE", "ExecutionPlatform")
                require(minutes > 0, "triggered blocking EstimatedMinutes")
                require(risk_target is not None and risk_target not in risk_targets, "atomic RiskTargetId uniqueness")
                risk_targets.add(risk_target)
                if item["MethodKind"] == "COMMAND":
                    command_key = (kind, command_identity(method))
                    require(command_key not in command_identities, "duplicate blocking command method")
                    command_identities.add(command_key)
                if profile["SelectedProfile"] == "LEAN" and item["MethodKind"] in ("COMMAND", "DETERMINISTIC_ASSERTION"):
                    require(not re.search(r"(?:&&|\|\||[;；\r\n])", method), "LEAN atomic method delimiter")
            else:
                require(item["Owner"] == "WRITER" and item["MethodKind"] == "NONE" and item.get("Method") is None and execution_platform == "NONE" and minutes == 0 and risk_target is None, "untriggered control")
            if item["Triggered"]:
                blocking_controls += 1
                blocking_minutes += minutes
        else:
            require(item.get("AcceptanceSourceRef") is None, "AcceptanceSourceRef")
            require(bool(refs), "SourceRefs")
            for ref in refs:
                validate_control_source_ref(repo, acceptance_path, ref, profile["SelectedProfile"])
            require(not item["Blocking"], "AD non-blocking invariant")
            require(item["MethodKind"] == "ADVISORY", "AD MethodKind")
            require(item.get("Method") is None or bool(string(item.get("Method"), "Method")), "Method")
            if item["Triggered"]:
                require(minutes > 0 and risk_target is not None, "triggered AD cost and RiskTargetId")
                advisory_minutes += minutes
            else:
                require(minutes == 0 and risk_target is None, "untriggered AD cost and RiskTargetId")
    require(consumed_ba == set(sources), "BA source coverage")
    budget = profile["ExecutionBudget"]
    require(blocking_controls == budget["BlockingControlCount"], "BlockingControlCount binding")
    require(len(risk_targets) == budget["AtomicRiskTargetCount"], "AtomicRiskTargetCount binding")
    require(blocking_minutes == budget["BlockingControlsMinutes"], "BlockingControlsMinutes binding")
    require(advisory_minutes == budget["AdvisoryMinutes"], "AdvisoryMinutes binding")
    if profile["SelectedProfile"] == "LEAN":
        require(all(item["Owner"] == "WRITER" for item in controls if item["Class"] in ("RB", "QG") and item["Triggered"]), "LEAN control Owner")
        require(all(item["Owner"] == "WRITER" for item in controls if item["Class"] == "AD" and item["Triggered"]), "LEAN AD Owner")
        require(blocking_controls <= 8 and len(risk_targets) <= 8, "LEAN blocking control and risk-target limit")
        require(blocking_minutes + budget["ImplementationMinutes"] <= budget["ImplementationAndControlsMinutes"], "LEAN control budget")
    elif profile["SelectedProfile"] == "STANDARD":
        owners = {item["Owner"] for item in controls if item["Triggered"] and item["Blocking"] and item["Class"] in ("RB", "QG")}
        require({"QA", "REVIEWER"} <= owners, "STANDARD QA and Reviewer phase-1 routing")
    else:
        owners = {item["Owner"] for item in controls if item["Triggered"] and item["Blocking"] and item["Class"] in ("RB", "QG")}
        require({"QA", "REVIEWER"} <= owners, "CONTROLLED independent QA and Reviewer")


def validate_plan_chain(plan, acceptance_path, profile, control_path, repo, seen=None):
    seen = set() if seen is None else seen
    if plan["PreviousPlanRef"] is None:
        return
    revision = plan["PreviousPlanRevision"]
    previous_plan_path = reference_path(repo, acceptance_path, plan["PreviousPlanRef"])
    previous_profile_path = reference_path(repo, control_path, profile["PreviousProfileRef"])
    identity = (revision.lower(), previous_plan_path.resolve())
    require(identity not in seen, "PreviousPlanRef cycle")
    seen.add(identity)
    previous_plan = git_contract(repo, revision, previous_plan_path, "PreviousPlanRef")
    previous_profile = git_contract(repo, revision, previous_profile_path, "PreviousProfileRef")
    validate_contract_schema(previous_plan, repo, "acceptance-plan.schema.json", "PreviousPlan")
    validate_contract_schema(previous_profile, repo, "control-profile.schema.json", "PreviousProfile")
    previous_sources = validate_control_profile(previous_profile, previous_profile_path)
    validate_source_provenance(previous_profile, previous_profile_path)
    validate_acceptance_plan(previous_plan, previous_profile, previous_sources, previous_plan_path)
    require(reference_path(repo, previous_plan_path, previous_plan["ControlProfileRef"]).resolve() == previous_profile_path.resolve(), "previous plan/profile path binding")
    current_version = tuple(map(int, plan["PlanVersion"][1:].split(".")))
    previous_version = tuple(map(int, previous_plan["PlanVersion"][1:].split(".")))
    require(current_version > previous_version, "PlanVersion progression")
    current_controls = {item["ControlId"]: item for item in plan["Controls"]}
    for previous_control in previous_plan["Controls"]:
        if previous_control["Class"] not in ("BA", "RB", "QG") or not previous_control["Triggered"] or not previous_control["Blocking"]:
            continue
        current = current_controls.get(previous_control["ControlId"])
        require(current is not None, "triggered blocking control removal forbidden")
        require(current == previous_control, "triggered blocking control change forbidden in phase 1")
    validate_plan_chain(previous_plan, previous_plan_path, previous_profile, previous_profile_path, repo, seen)


def validate_replay_consistency(profile, plan, acceptance_path):
    LOCATION.set("ReplayConsistency")
    repo = repository_root(acceptance_path)
    expectation_path = Path(acceptance_path).parent / "replay-expectations.json"
    require(expectation_path.is_file(), "replay expectations")
    expectations = load_contract(expectation_path, "ReplayExpectations")
    validate_contract_schema(expectations, repo, "replay-expectations.schema.json", "ReplayExpectations")
    require(expectations["RequirementId"] == profile["RequirementId"] == plan["RequirementId"], "replay RequirementId binding")
    required_rules = set(expectations["RequiredRuleRefs"])
    control_rules = {
        ref for item in plan["Controls"] if item["Triggered"] and item["Class"] in ("RB", "QG")
        for ref in item["SourceRefs"] if ref.startswith("RULE:")
    }
    require(required_rules <= control_rules, "required replay rule controls")
    report_path = acceptance_path.parent / "回放报告.md"
    require(report_path.is_file(), "replay report")
    report = report_path.read_text(encoding="utf-8-sig")
    counts = {kind: sum(item["Class"] == kind for item in plan["Controls"]) for kind in ("BA", "RB", "QG", "AD")}
    require(counts == expectations["ExpectedControlCounts"], "replay expected control counts")
    count_marker = "BA/RB/QG/AD = " + "/".join(str(counts[kind]) for kind in ("BA", "RB", "QG", "AD"))
    require(count_marker in report, "replay report control counts")
    budget = profile["ExecutionBudget"]
    require(budget["BlockingControlsMinutes"] == expectations["ExpectedBlockingControlsMinutes"], "replay expected blocking cost")
    require(budget["AdvisoryMinutes"] == expectations["ExpectedAdvisoryMinutes"], "replay expected advisory cost")
    cost_marker = f"阻断控制预算：{budget['BlockingControlsMinutes']} 分钟；AD 预算：{budget['AdvisoryMinutes']} 分钟"
    require(cost_marker in report, "replay report control costs")
    require(all(token in report for token in expectations["ReportRequiredTokens"]), "replay report required tokens")


def validate_plan(control_path, acceptance_path, mode="live"):
    require(mode in ("live", "replay"), "validation mode")
    control_path, acceptance_path = Path(control_path).absolute(), Path(acceptance_path).absolute()
    repo = repository_root(control_path)
    profile = load_contract(control_path, "ControlProfile")
    validate_contract_schema(profile, repo, "control-profile.schema.json", "ControlProfile")
    sources = validate_control_profile(profile, control_path)
    validate_source_provenance(profile, control_path)
    plan = load_contract(acceptance_path, "AcceptancePlan")
    validate_contract_schema(plan, repo, "acceptance-plan.schema.json", "AcceptancePlan")
    LOCATION.set("GovernancePlan")
    requirement_dir = (repo / "docs/requirements" / profile["RequirementId"]).resolve()
    if mode == "live":
        require(control_path.resolve() == requirement_dir / "control-profile.json", "live ControlProfile canonical path")
        require(acceptance_path.resolve() == requirement_dir / "acceptance-plan.json", "live AcceptancePlan canonical path")
    else:
        replay_dir = (repo / "docs/Agent治理/replays" / f"{profile['RequirementId']}-v3").resolve()
        require(control_path.resolve() == replay_dir / "control-profile.json", "replay ControlProfile canonical path")
        require(acceptance_path.resolve() == replay_dir / "acceptance-plan.json", "replay AcceptancePlan canonical path")
    validate_acceptance_plan(plan, profile, sources, acceptance_path)
    if mode == "replay":
        validate_replay_consistency(profile, plan, acceptance_path)
    tracked_profile = head_text(repo, control_path)
    if tracked_profile is not None:
        try:
            head_profile = json.loads(tracked_profile.decode("utf-8-sig"), object_pairs_hook=unique_object)
        except (json.JSONDecodeError, UnicodeError):
            raise Invalid("HEAD ControlProfile: invalid JSON encoding or syntax") from None
        if head_profile != profile:
            require(profile["PreviousProfileRevision"] == head_revision(repo), "in-place profile change must bind HEAD")
            require(reference_path(repo, control_path, profile["PreviousProfileRef"]).resolve() == control_path.resolve(), "in-place profile PreviousProfileRef")
    validate_profile_chain(profile, control_path, repo)
    tracked_plan = head_text(repo, acceptance_path)
    if tracked_plan is not None:
        try:
            head_plan = json.loads(tracked_plan.decode("utf-8-sig"), object_pairs_hook=unique_object)
        except (json.JSONDecodeError, UnicodeError):
            raise Invalid("HEAD AcceptancePlan: invalid JSON encoding or syntax") from None
        if head_plan != plan:
            require(plan["PreviousPlanRevision"] == head_revision(repo), "in-place plan change must bind HEAD")
            require(reference_path(repo, acceptance_path, plan["PreviousPlanRef"]).resolve() == acceptance_path.resolve(), "in-place plan PreviousPlanRef")
    validate_plan_chain(plan, acceptance_path, profile, control_path, repo)
    declared = reference_path(repo, acceptance_path, plan["ControlProfileRef"])
    require(safe_path(repo, declared).resolve() == control_path.resolve(), "ControlProfileRef binding")
    pre_g2_names = {"需求基线.md", "REQUIREMENT_BASELINE.md", "control-profile.json", "acceptance-plan.json", "开发上下文包.md", "CONTEXT_PACK.md"}
    resolved_records = {}
    for ref in profile["RecordRefs"]:
        record = reference_path(repo, control_path, ref)
        resolved_records[Path(ref).name] = record.resolve()
        if Path(ref).name in pre_g2_names:
            require(safe_path(repo, record).is_file(), "RecordRefs pre-G2 existence")
    fixed_requirement_records = {"需求基线.md", "开发上下文包.md", "开发记录.md"}
    if profile["SelectedProfile"] != "LEAN":
        fixed_requirement_records |= {"临时质量验证记录.md", "只读评审记录.md"}
    for name in fixed_requirement_records:
        require(resolved_records.get(name) == (requirement_dir / name).resolve(), "RecordRefs RequirementId binding")
    require(resolved_records.get("control-profile.json") == control_path.resolve(), "RecordRefs ControlProfile binding")
    require(resolved_records.get("acceptance-plan.json") == acceptance_path.resolve(), "RecordRefs AcceptancePlan binding")
    return {
        "ResultType": "GOVERNANCE_PLAN_VALIDATION",
        "SchemaVersion": profile["SchemaVersion"],
        "RequirementId": profile["RequirementId"],
        "Mode": mode.upper(),
        "Valid": True,
        "G2Eligible": mode == "live",
        "RequiredG2DecisionMode": (
            "HUMAN" if any(item["Triggered"] and item["MethodKind"] == "DETERMINISTIC_ASSERTION" for item in plan["Controls"])
            else "AUTOMATIC_ALLOWED"
        ),
    }


def validate_g2(control_path, acceptance_path):
    result = validate_plan(control_path, acceptance_path, "live")
    require(result["Mode"] == "LIVE" and result["G2Eligible"] is True, "G2 live eligibility")
    return {**result, "ResultType": "G2_ELIGIBILITY"}


def adjustment_digest(data):
    unsigned = {key: value for key, value in data.items() if key not in ("ApprovalDigest", "ReviewerDigest")}
    return content_digest(json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def attributed_failures(payload):
    failures = array(payload.get("ObservedFailures"), "ObservedFailures")
    for failure in failures:
        require(isinstance(failure, dict), "attributed failure")
        require(failure.get("Attribution") in ("CANDIDATE", "PREEXISTING", "UNATTRIBUTED"), "failure Attribution")
        unique_strings(failure.get("ValidationIds"), "failure ValidationIds", minimum=1)
        unique_strings(failure.get("EvidenceRefs"), "failure EvidenceRefs", minimum=1)
    return [failure for failure in failures if failure["Attribution"] == "CANDIDATE"]


def validate_adjustment(path):
    """Validate an explicit adjustment without rewriting the frozen plan or past results."""
    path = Path(path).absolute()
    repo = repository_root(path)
    data = load_contract(path, "ValidationAdjustment")
    exact_object(data, "ValidationAdjustment", (
        "GovernanceVersion", "RequirementId", "OriginalPlanRef", "OriginalPlanDigest", "ControlProfileRef",
        "ControlProfileDigest", "CandidateRef", "CandidateEventDigest", "G2Ref", "G2Digest", "OriginQARef", "OriginQADigest",
        "Reason", "EvidenceRefs", "EvidenceDigests", "Replacements", "ReviewerRef", "ReviewerDigest", "ApprovalRef", "ApprovalDigest",
    ))
    require(data["GovernanceVersion"] == "v3.0.1", "adjustment governance version")
    require(valid_requirement_id(data["RequirementId"]), "adjustment RequirementId")
    requirement_dir = repo / "docs/requirements" / data["RequirementId"]
    require(path.parent.resolve() == (requirement_dir / "adjustments").resolve(), "adjustment directory")
    plan_path = validate_file_reference(repo, path, data["OriginalPlanRef"], "OriginalPlanRef")
    profile_path = validate_file_reference(repo, path, data["ControlProfileRef"], "ControlProfileRef")
    require(content_digest(plan_path.read_text(encoding="utf-8-sig")) == data["OriginalPlanDigest"], "original plan digest")
    require(content_digest(profile_path.read_text(encoding="utf-8-sig")) == data["ControlProfileDigest"], "original profile digest")
    validate_plan(profile_path, plan_path)
    profile = load_contract(profile_path, "ControlProfile")
    plan = load_contract(plan_path, "AcceptancePlan")
    require(profile["RequirementId"] == data["RequirementId"] and profile["GovernanceVersion"] == "v3.0.1", "adjustment profile binding")
    require(profile["SelectedProfile"] in ("STANDARD", "CONTROLLED"), "adjustment requires independent quality profile")
    require(data["Reason"] in ("PREEXISTING_FAILURE", "BASELINE_UNATTRIBUTABLE", "VALIDATION_DEFINITION_INVALID"), "adjustment reason")
    evidence_refs = set(unique_strings(data["EvidenceRefs"], "adjustment evidence", minimum=1))
    for replacement in array(data["Replacements"], "Replacements"):
        require(isinstance(replacement, dict), "replacement")
        evidence_refs.update(unique_strings(replacement.get("CoverageEvidenceRefs"), "replacement coverage", minimum=1))
    exact_object(data["EvidenceDigests"], "EvidenceDigests", evidence_refs)
    for ref in evidence_refs:
        evidence = validate_file_reference(repo, path, ref, "adjustment evidence")
        require(content_digest(evidence.read_text(encoding="utf-8-sig")) == data["EvidenceDigests"][ref], "adjustment evidence digest")

    def fact(reference, kind, digest=None):
        source_path = validate_file_reference(repo, path, reference, kind)
        require(source_path.resolve().is_relative_to((requirement_dir / "facts").resolve()), kind + " requirement facts")
        value = load_contract(source_path, kind)
        validate_event_structure(value, data["RequirementId"], source_path)
        require(value["EventType"] == kind and value["SupersedesEventRef"] is None, kind + " type/uncorrected")
        if digest is not None:
            require(content_digest(source_path.read_text(encoding="utf-8-sig")) == digest, kind + " digest")
        return value

    candidate = fact(data["CandidateRef"], "CANDIDATE_STATE", data["CandidateEventDigest"])
    require(candidate["CandidateRef"] in (candidate["EventId"], data["CandidateRef"]), "adjustment candidate identity")
    origin = fact(data["OriginQARef"], "QA_RESULT", data["OriginQADigest"])
    require(origin["Payload"].get("Status") == "BLOCKED" and not attributed_failures(origin["Payload"]), "adjustment cannot bypass candidate FAIL")
    noncandidate_ids = set()
    for failure in origin["Payload"]["ObservedFailures"]:
        noncandidate_ids.update(failure["ValidationIds"])
        require(set(failure["EvidenceRefs"]) <= evidence_refs, "failure attribution pinned evidence")
    require(all(item.get("Result") != "FAIL" or item.get("ValidationId") in noncandidate_ids for item in origin["Payload"].get("EffectiveValidationSet", [])), "adjustment cannot hide failed validation")
    require(origin["Payload"].get("CountDisposition") == "ACTIVE", "adjustment active origin")
    g2 = fact(data["G2Ref"], "GATE_DECISION", data["G2Digest"])
    require(g2["Payload"].get("Gate") == "G2" and g2["Payload"].get("Decision") == "FROZEN", "adjustment frozen G2")
    require(g2["ContextPackVersion"] == candidate["ContextPackVersion"], "adjustment frozen context")
    require(g2["Payload"].get("ArtifactRef") == f"docs/requirements/{data['RequirementId']}/开发上下文包.md", "adjustment G2 artifact")
    require(g2["Payload"].get("ArtifactVersion") == candidate["ContextPackVersion"], "adjustment G2 version")
    require(timestamp(g2["CreatedAt"]) <= timestamp(candidate["CreatedAt"]), "adjustment gate chronology")
    digest = adjustment_digest(data)
    reviewer = fact(data["ReviewerRef"], "REVIEW_RESULT", data["ReviewerDigest"])
    approval = fact(data["ApprovalRef"], "HUMAN_RECOVERY", data["ApprovalDigest"])
    for event in (origin, reviewer, approval):
        for key in ("CandidateRef", "ContextPackVersion", "RepairLineageRef"):
            require(event[key] == candidate[key], "adjustment " + key)
    require(reviewer["Payload"].get("Decision") == "APPROVE" and not reviewer["Payload"].get("Findings") and not reviewer["Payload"].get("EvidenceIssues"), "adjustment independent review")
    require(reviewer["Payload"].get("CandidateDigestVerified") is True, "adjustment reviewed candidate digest")
    unique_strings(reviewer["EvidenceRefs"], "adjustment review evidence", minimum=1)
    for event in (reviewer, approval):
        require(event["Payload"].get("AdjustmentDigest") == digest, "adjustment approval content binding")
    require(approval["Payload"].get("Decision") == "APPROVED", "adjustment human approval")
    human_approval_reference(approval["Payload"].get("ApprovalRef"))
    require(timestamp(candidate["CreatedAt"]) <= timestamp(origin["CreatedAt"]) <= timestamp(reviewer["CreatedAt"]) <= timestamp(approval["CreatedAt"]), "adjustment evidence chronology")
    replacements = array(data["Replacements"], "Replacements")
    require(bool(replacements), "nonempty adjustment")
    controls = {item["ControlId"]: item for item in plan["Controls"]}
    effective = copy.deepcopy(plan)
    changed = set()
    for replacement in replacements:
        exact_object(replacement, "replacement", ("ControlId", "OriginalValidationIds", "Statement", "Method", "MethodKind", "EstimatedMinutes", "CoverageEvidenceRefs"))
        ident = replacement["ControlId"]
        require(ident in controls and ident not in changed, "replacement control uniqueness")
        changed.add(ident)
        old = controls[ident]
        require(old["Class"] in ("RB", "QG") and old["Triggered"] and old["Blocking"], "replacement blocking technical control only")
        require(old["Owner"] != "REVIEWER", "replacement cannot waive independent reviewer")
        require(replacement["Method"] != old["Method"], "replacement must change method")
        require(type(replacement["EstimatedMinutes"]) is int and replacement["EstimatedMinutes"] >= old["EstimatedMinutes"], "replacement cannot understate frozen budget")
        blocked = {vid for item in origin["Payload"].get("BlockingItems", []) for vid in item.get("AffectedValidationIds", [])}
        validation_ids = unique_strings(replacement["OriginalValidationIds"], "replacement original validation IDs", minimum=1)
        require(set(validation_ids) <= blocked, "replacement originally blocked validation")
        for vid in validation_ids:
            entries = [item for item in origin["Payload"].get("NotExecuted", []) + origin["Payload"].get("EffectiveValidationSet", []) if item.get("ValidationId") == vid]
            require(len(entries) == 1 and entries[0].get("ControlId") == ident, "replacement original control mapping")
        for ref in unique_strings(replacement["CoverageEvidenceRefs"], "replacement coverage", minimum=1):
            validate_file_reference(repo, path, ref, "replacement coverage evidence")
        target = next(item for item in effective["Controls"] if item["ControlId"] == ident)
        for key in ("Statement", "Method", "MethodKind", "EstimatedMinutes"):
            target[key] = replacement[key]
    effective_profile = copy.deepcopy(profile)
    effective_profile["ExecutionBudget"]["BlockingControlsMinutes"] = sum(item["EstimatedMinutes"] for item in effective["Controls"] if item["Class"] in ("RB", "QG") and item["Triggered"] and item["Blocking"])
    sources = {source["AcceptanceSourceId"]: source for source in profile["BusinessAcceptanceSources"]}
    validate_acceptance_plan(effective, effective_profile, sources, plan_path)
    return {"ResultType": "VALIDATION_ADJUSTMENT", "Valid": True, "RequirementId": data["RequirementId"],
            "AdjustmentDigest": digest, "CandidateRef": data["CandidateRef"], "HistoricalResultsUnchanged": True,
            "EffectiveControls": effective["Controls"], "QualityPassed": False}


def validate_adjusted_quality(adjustment_path, qa_path, review_path):
    validated = validate_adjustment(adjustment_path)
    adjustment_path = Path(adjustment_path).absolute()
    repo = repository_root(adjustment_path)
    data = load_contract(adjustment_path, "ValidationAdjustment")
    facts_root = repo / "docs/requirements" / data["RequirementId"] / "facts"
    candidate = load_contract(reference_path(repo, adjustment_path, data["CandidateRef"]), "Candidate")
    approved = load_contract(reference_path(repo, adjustment_path, data["ApprovalRef"]), "AdjustmentApproval")

    def quality_event(path, kind):
        path = safe_path(repo, Path(path).absolute())
        require(path.resolve().is_relative_to(facts_root.resolve()), "quality facts location")
        event = load_contract(path, kind)
        validate_event_structure(event, data["RequirementId"], path)
        require(event["EventType"] == kind and event["SupersedesEventRef"] is None, "quality type/correction")
        for key in ("CandidateRef", "ContextPackVersion", "RepairLineageRef"):
            require(event[key] == candidate[key], "quality " + key)
        p = event["Payload"]
        round_name = "QARound" if kind == "QA_RESULT" else "ReviewerRound"
        require(type(p.get(round_name)) is int and p[round_name] > 0, "quality " + round_name)
        require(p.get("AdjustmentDigest") == validated["AdjustmentDigest"], "quality adjustment digest")
        require(reference_path(repo, path, string(p.get("AdjustmentRef"), "AdjustmentRef")).resolve() == adjustment_path.resolve(), "quality adjustment reference")
        require(p.get("CandidateDigestVerified") is True and p.get("CountDisposition") == "ACTIVE" and p.get("DispositionRef") is None, "quality active verified candidate")
        bounds = timing(p, timestamp(event["CreatedAt"]))
        require(timestamp(approved["CreatedAt"]) <= bounds[0], "quality after adjustment approval")
        unique_strings(event["EvidenceRefs"], "quality evidence", minimum=1)
        return event, bounds

    qa, bounds = quality_event(qa_path, "QA_RESULT")
    review, _ = quality_event(review_path, "REVIEW_RESULT")
    p = qa["Payload"]
    require(p.get("Status") == "PASS" and not attributed_failures(p), "quality QA PASS")
    require(p.get("RedactionState") in ("SAFE", "REDACTED") and p.get("SensitiveEvidenceIssues") == [] and p.get("BlockingItems") == [], "quality no unresolved blockers")
    require(p.get("ValidationPlanRef") == data["OriginalPlanRef"], "quality original plan reference")
    controls = {item["ControlId"]: item for item in validated["EffectiveControls"] if item["Triggered"] and item["Owner"] == "QA"}
    seen, ids = set(), set()
    for item in array(p.get("EffectiveValidationSet"), "quality EffectiveValidationSet") + array(p.get("NotExecuted"), "quality NotExecuted"):
        require(isinstance(item, dict), "quality validation item")
        control = item.get("ControlId")
        require(control in controls and control not in seen, "quality control coverage")
        seen.add(control)
        vid = string(item.get("ValidationId"), "quality ValidationId")
        require(vid not in ids, "quality unique ValidationId")
        ids.add(vid)
        expected = controls[control]
        require(item.get("GateClass") == ("BLOCKING" if expected["Blocking"] else "ADVISORY"), "quality gate class")
        if item in p["NotExecuted"]:
            require(not expected["Blocking"], "quality blocking control unexecuted")
            string(item.get("Reason"), "quality advisory reason")
            continue
        require(command_identity(string(item.get("CommandOrMethod"), "quality executed method")) == command_identity(expected["Method"]), "quality effective method")
        require(item.get("Result") in ("PASS", "FAIL", "BLOCKED"), "quality item result")
        require(not expected["Blocking"] or item["Result"] == "PASS", "quality blocking result")
        unique_strings(item.get("EvidenceRefs"), "quality item evidence", minimum=1)
        disposition = item.get("Disposition")
        if disposition == "EXECUTED":
            timing(item, bounds[1], bounds)
        elif disposition == "REUSED":
            source_path = reference_path(repo, qa_path, string(item.get("SourceQAEventRef"), "quality reuse source"))
            source, _ = quality_event(source_path, "QA_RESULT")
            require(source["Payload"].get("Status") in ("PASS", "BLOCKED") and not attributed_failures(source["Payload"]), "quality reuse nonfailed source")
            require(source["EventId"] != qa["EventId"] and timestamp(source["CreatedAt"]) <= bounds[0], "quality reuse chronology")
            originals = [entry for entry in source["Payload"].get("EffectiveValidationSet", []) if entry.get("ControlId") == control]
            require(len(originals) == 1 and originals[0].get("Disposition") == "EXECUTED" and originals[0].get("Result") == item["Result"] == "PASS", "quality reuse executed PASS")
            require(originals[0].get("CommandOrMethod") == item["CommandOrMethod"], "quality reuse method")
            string(p.get("RevalidationPlanRef"), "quality reuse plan")
        else:
            raise Invalid("quality validation disposition")
    require(seen == set(controls), "quality complete effective control coverage")
    rp = review["Payload"]
    require(rp.get("Decision") == "APPROVE" and rp.get("ReviewScope") == "STATIC_CANDIDATE" and rp.get("Findings") == [] and rp.get("EvidenceIssues") == [], "quality independent reviewer APPROVE")
    require(not any(item["Triggered"] and item["Blocking"] and item["Class"] in ("RB", "QG") and item["Owner"] == "WRITER" for item in validated["EffectiveControls"]), "quality writer-owned blocking controls require runtime verification")
    for path in facts_root.rglob("*.json"):
        event = load_contract(path, "quality correction check")
        require(event.get("EventType") != "FACT_CORRECTION", "quality correction requires runtime resolution")
    # Do not accept a selected old PASS if a newer active result or candidate exists.
    for directory, kind, selected in (("qa", "QA_RESULT", qa), ("reviews", "REVIEW_RESULT", review), ("candidates", "CANDIDATE_STATE", candidate)):
        for path in (facts_root / directory).glob("*.json"):
            event = load_contract(path, "quality freshness")
            validate_event_structure(event, data["RequirementId"], path)
            if event["EventType"] != kind or event["EventId"] == selected["EventId"]:
                continue
            relevant = kind == "CANDIDATE_STATE" or (event["CandidateRef"] == candidate["CandidateRef"] and event["Payload"].get("CountDisposition") == "ACTIVE")
            require(not relevant or timestamp(event["CreatedAt"]) < timestamp(selected["CreatedAt"]), "quality stale selected result/candidate")
    return {"ResultType": "ADJUSTED_QUALITY_JOIN", "Valid": True, "RequirementId": data["RequirementId"],
            "CandidateRef": candidate["CandidateRef"], "AdjustmentDigest": validated["AdjustmentDigest"],
            "ReadyForHumanAcceptance": True, "HumanAccepted": False, "HistoricalResultsUnchanged": True}


def load_events(root):
    LOCATION.set("requirement directory")
    root = safe_path(root, root)
    require(valid_requirement_id(root.name), "RequirementId directory")
    contexts = [root / name for name in ("开发上下文包.md", "CONTEXT_PACK.md") if (root / name).exists()]
    require(len(contexts) == 1, "context file ambiguity")
    context = safe_path(root, contexts[0]).read_text(encoding="utf-8-sig")
    LOCATION.set(contexts[0].name)
    versions = re.findall(r"(?:事实协议版本|FactProtocolVersion)[^\n]*?\b(v\d+\.\d+(?:\.\d+)?)\b", context)
    require(len(set(versions)) == 1 and versions[0] in ("v2.1", "v3.0", "v3.0.1"), "FactProtocolVersion (context)")
    facts = safe_path(root, root / "facts")
    require(facts.is_dir(), "facts directory")
    events = {}
    for path in sorted(facts.rglob("*")):
        LOCATION.set(path.relative_to(root).as_posix())
        safe_path(root, path)
        if path.suffix != ".json":
            continue
        try:
            event = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object)
        except (json.JSONDecodeError, UnicodeError):
            raise Invalid(f"{LOCATION.get()}: invalid JSON encoding or syntax") from None
        ident = validate_event_structure(event, root.name, path.relative_to(root))
        require(event["EventType"] != "FACT_CORRECTION" and event["SupersedesEventRef"] is None, "correction requires runtime resolution")
        if event["EventType"] in ("QA_RESULT", "REVIEW_RESULT", "WRITER_HANDOFF", "CANDIDATE_STATE"):
            string(event["ContextPackVersion"], "ContextPackVersion")
            if event["RepairLineageRef"] is not None:
                string(event["RepairLineageRef"], "RepairLineageRef")
        require(ident not in events, "duplicate EventId")
        event["_path"] = path.relative_to(root).as_posix()
        event["_context_document_version"] = versions[0]
        events[ident] = event
    require(bool(events), "empty facts")
    return events


def resolve(events, ref, kind, source):
    string(ref, "event reference")
    matches = [e for e in events.values() if ref in (e["EventId"], e["_path"], "docs/requirements/" + source["RequirementId"] + "/" + e["_path"])]
    require(len(matches) == 1 and matches[0]["EventType"] == kind, "event reference")
    target = matches[0]
    for key in ("RequirementId", "ContextPackVersion", "RepairLineageRef"):
        require(target[key] == source[key], key + " binding")
    require(timestamp(target["CreatedAt"]) <= timestamp(source["CreatedAt"]), "reference chronology")
    return target


def route_reference(events, event):
    refs = []
    for route in events.values():
        if route["EventType"] != "ROUTING_DECISION":
            continue
        triggers = array(route["Payload"].get("TriggerEventRefs"), "TriggerEventRefs")
        permitted = (event["EventId"], event["_path"], "docs/requirements/" + event["RequirementId"] + "/" + event["_path"])
        if any(ref in permitted for ref in triggers):
            for key in ("RequirementId", "ContextPackVersion", "RepairLineageRef", "CandidateRef"):
                require(route[key] == event[key], "route " + key)
            require(timestamp(route["CreatedAt"]) >= timestamp(event["CreatedAt"]), "route chronology")
            refs.append(route["_path"])
    return "; ".join(sorted(refs)) or None


def validate_qa_resume(events, event, previous):
    """Check an append-only continuation of a blocked logical QA round."""
    require(event["_context_document_version"] == "v3.0.1", "same-round resume document version")
    payload = event["Payload"]
    source = resolve(events, payload.get("ResumeQAEventRef"), "QA_RESULT", event)
    require(previous is not None and source["EventId"] == previous["EventId"], "resume latest round event")
    require(source["CandidateRef"] == event["CandidateRef"], "resume candidate")
    require(source["Payload"].get("Status") == "BLOCKED", "resume BLOCKED only")
    require(source["Payload"].get("CountDisposition") == payload.get("CountDisposition") == "ACTIVE", "resume active round")
    require(source["Payload"].get("QARound") == payload.get("QARound"), "resume QARound")
    plan = string(payload.get("ValidationPlanRef"), "resume ValidationPlanRef")
    require(source["Payload"].get("ValidationPlanRef") == plan, "resume unchanged plan")
    require(source["Payload"].get("AdjustmentDigest") == payload.get("AdjustmentDigest"), "resume unchanged adjustment")
    string(payload.get("RevalidationPlanRef"), "resume RevalidationPlanRef")
    ready = resolve(events, payload.get("ResumeReadinessEventRef"), "TEST_READINESS", event)
    require(ready["CandidateRef"] == event["CandidateRef"], "resume readiness candidate")
    require(ready["Payload"].get("ValidationPlanRef") == plan, "resume readiness plan")
    require(timestamp(source["CreatedAt"]) < timestamp(ready["CreatedAt"]) <= timestamp(payload.get("StartedAt")), "resume readiness chronology")
    unique_strings(ready["EvidenceRefs"], "resume release evidence", minimum=1)
    entries = array(ready["Payload"].get("Entries"), "resume readiness entries")
    readiness = {}
    for item in entries:
        require(isinstance(item, dict), "resume readiness entry")
        vid = string(item.get("ValidationId"), "resume readiness ValidationId")
        require(vid not in readiness, "resume duplicate readiness")
        require(item.get("Readiness") in ("READY", "NOT_READY"), "resume readiness value")
        readiness[vid] = item["Readiness"]
    blocked_ids = {vid for item in source["Payload"]["BlockingItems"] for vid in item["AffectedValidationIds"]}
    require(any(readiness.get(vid) == "READY" for vid in blocked_ids), "resume released blocker")
    for item in array(payload.get("EffectiveValidationSet"), "resume EffectiveValidationSet"):
        if item.get("Disposition") == "EXECUTED":
            require(readiness.get(item.get("ValidationId")) == "READY", "resume executed readiness")
    old_ids = {item["ValidationId"] for item in source["Payload"]["EffectiveValidationSet"] + source["Payload"]["NotExecuted"]}
    new_ids = {item["ValidationId"] for item in payload["EffectiveValidationSet"] + payload["NotExecuted"]}
    require(old_ids == new_ids, "resume unchanged validation set")


def render(root, kind):
    events = load_events(Path(root))
    wanted = {"qa-index": "QA_RESULT", "review-index": "REVIEW_RESULT", "changes": "WRITER_HANDOFF"}[kind]
    selected = sorted((e for e in events.values() if e["EventType"] == wanted), key=lambda e: (timestamp(e["CreatedAt"]), e["EventId"]))
    require(bool(selected), wanted)
    headers = {"qa-index": ["QA 事件", "QARound", "CandidateRef", "状态", "EXECUTED / REUSED / 阻塞的 V-ID", "耗时", "原始 JSON", "处置路由"], "review-index": ["Reviewer 事件", "ReviewerRound", "CandidateRef", "ReviewScope", "决定", "Findings / EvidenceIssues 数量", "耗时", "原始 JSON", "处置路由"], "changes": ["WorkPackageId", "WriterRound", "操作类型", "文件路径", "代码单元", "修改事实摘要", "行为变化 / 原因", "证据引用"]}[kind]
    rows = []
    rounds = {}
    for event in selected:
        LOCATION.set(event["_path"])
        p = event["Payload"]
        round_key = {"qa-index": "QARound", "review-index": "ReviewerRound", "changes": "WriterRound"}[kind]
        number = p.get(round_key)
        require(type(number) is int and number > 0, round_key)
        wp = string(p.get("WorkPackageId"), "WorkPackageId") if kind == "changes" else None
        round_scope = (event["ContextPackVersion"], event["RepairLineageRef"], wp, number)
        if kind == "qa-index" and p.get("ResumeQAEventRef") is not None:
            validate_qa_resume(events, event, rounds.get(round_scope))
        else:
            require(round_scope not in rounds, "duplicate round")
            require(p.get("ResumeReadinessEventRef") is None, "orphan resume readiness")
        rounds[round_scope] = event
        if kind == "changes":
            string(p.get("WriterSlot"), "WriterSlot")
            require(p.get("SensitiveEvidenceDetected") is False, "SensitiveEvidenceDetected")
            actions = set()
            for change in array(p.get("Changes"), "Changes"):
                require(isinstance(change, dict), "Changes item")
                action = string(change.get("ActionId"), "ActionId")
                require(action not in actions, "duplicate ActionId")
                actions.add(action)
                require(change.get("Operation") in ("ADD", "MODIFY", "DELETE", "RENAME", "MOVE"), "Operation")
                for key in ("Path", "CodeUnit", "Summary", "Reason"):
                    string(change.get(key), key)
                path = change["Path"].replace("\\", "/")
                require(not path.startswith("/") and ":" not in path and ".." not in path.split("/"), "Path boundary")
                refs = array(change.get("EvidenceRefs"), "EvidenceRefs")
                for ref in refs:
                    string(ref, "EvidenceRefs")
                rows.append([wp, number, change["Operation"], path, change["CodeUnit"], change["Summary"], change["Reason"], "; ".join(refs)])
            continue
        candidate = resolve(events, event["CandidateRef"], "CANDIDATE_STATE", event)
        require(candidate["CandidateRef"] in (candidate["EventId"], candidate["_path"], "docs/requirements/" + event["RequirementId"] + "/" + candidate["_path"]), "candidate identity")
        bounds = timing(p, timestamp(event["CreatedAt"]))
        require(type(p.get("CandidateDigestVerified")) is bool, "CandidateDigestVerified")
        disposition = p.get("DispositionRef")
        require(p.get("CountDisposition") in ("ACTIVE", "IGNORED_DUE_TO_PRIOR_DISPOSITION"), "CountDisposition")
        require((disposition is None) == (p["CountDisposition"] == "ACTIVE"), "DispositionRef")
        if disposition:
            route = resolve(events, disposition, "ROUTING_DECISION", event)
            require(route["CandidateRef"] == event["CandidateRef"], "route candidate")
        displayed_route = disposition or route_reference(events, event)
        if kind == "review-index":
            require(p.get("ReviewScope") == "STATIC_CANDIDATE", "ReviewScope")
            findings, issues = array(p.get("Findings"), "Findings"), array(p.get("EvidenceIssues"), "EvidenceIssues")
            require(p.get("Decision") in ("APPROVE", "CHANGE"), "Decision")
            require((p["Decision"] == "APPROVE") == (not findings and not issues), "review decision invariant")
            require(p["Decision"] != "APPROVE" or p["CandidateDigestVerified"], "candidate verification")
            rows.append([event["EventId"], number, event["CandidateRef"], p["ReviewScope"], p["Decision"], f"{len(findings)} / {len(issues)}", f'{p["DurationMs"]} ms', event["_path"], displayed_route])
        else:
            status = p.get("Status")
            require(p.get("RedactionState") in ("SAFE", "REDACTED"), "Payload.RedactionState")
            sensitive_issues = array(p.get("SensitiveEvidenceIssues"), "SensitiveEvidenceIssues")
            require(status in ("PASS", "FAIL", "BLOCKED"), "Status")
            require(status != "PASS" or not sensitive_issues, "sensitive evidence PASS")
            failures, blockers = array(p.get("ObservedFailures"), "ObservedFailures"), array(p.get("BlockingItems"), "BlockingItems")
            current_protocol = event["_context_document_version"] == "v3.0.1"
            quality_failures = attributed_failures(p) if current_protocol else failures
            for blocker in blockers:
                require(isinstance(blocker, dict), "BlockingItems item")
                string(blocker.get("BlockerId"), "BlockerId")
                array(blocker.get("AffectedValidationIds"), "AffectedValidationIds")
            require((status == "FAIL") == bool(quality_failures), "FAIL invariant")
            require(status != "BLOCKED" or bool(blockers), "BLOCKED invariant")
            require(status != "PASS" or (not blockers and p["CandidateDigestVerified"]), "PASS invariant")
            ids, labels = set(), []
            for item in array(p.get("EffectiveValidationSet"), "EffectiveValidationSet") + array(p.get("NotExecuted"), "NotExecuted"):
                require(isinstance(item, dict), "validation item")
                vid = string(item.get("ValidationId"), "ValidationId")
                require(vid not in ids, "duplicate ValidationId")
                ids.add(vid)
                require(item.get("GateClass") in ("BLOCKING", "ADVISORY"), "GateClass")
                if item in p["NotExecuted"]:
                    string(item.get("Reason"), "Reason")
                    ready = resolve(events, item.get("ReadinessEventRef"), "TEST_READINESS", event)
                    require(ready["CandidateRef"] == event["CandidateRef"], "readiness candidate")
                    if item["GateClass"] == "BLOCKING":
                        require(any(b.get("BlockerId") == item.get("BlockerId") and vid in b.get("AffectedValidationIds", []) for b in blockers), "blocker coverage")
                    label = "阻塞" if item["GateClass"] == "BLOCKING" else "未执行（ADVISORY）"
                else:
                    label = item.get("Disposition")
                    require(label in ("EXECUTED", "REUSED"), "Disposition")
                    require(item.get("Result") in ("PASS", "FAIL", "BLOCKED"), "Result")
                    noncandidate = current_protocol and any(vid in failure["ValidationIds"] and failure["Attribution"] != "CANDIDATE" for failure in failures)
                    require(item["Result"] != "FAIL" or status == "FAIL" or (status == "BLOCKED" and noncandidate), "observed item failure")
                    require(status != "PASS" or item["GateClass"] != "BLOCKING" or item["Result"] == "PASS", "blocking result")
                    if label == "EXECUTED":
                        timing(item, bounds[1], bounds)
                    else:
                        source = resolve(events, item.get("SourceQAEventRef"), "QA_RESULT", event)
                        require(source["EventId"] != event["EventId"] and source["CandidateRef"] == event["CandidateRef"], "reuse candidate")
                        originals = array(source["Payload"].get("EffectiveValidationSet"), "source EffectiveValidationSet")
                        require(all(isinstance(v, dict) for v in originals), "source validation item")
                        original = [v for v in originals if v.get("ValidationId") == vid]
                        require(len(original) == 1 and original[0].get("Result") == item["Result"] == "PASS", "reuse result")
                if label:
                    labels.append(f"{label}: {vid}")
            rows.append([event["EventId"], number, event["CandidateRef"], status, "; ".join(labels), f'{p["DurationMs"]} ms', event["_path"], displayed_route])
    return "\n".join("| " + " | ".join(cell(v) for v in row) + " |" for row in [headers, ["---"] * len(headers), *rows]) + "\n"


def markdown_body(text):
    return re.sub(r"(?ms)^\s*(```|~~~).*?^\s*\1[^\n]*$", "", text)


def anchors(text):
    result, counts = set(), {}
    for heading in re.findall(r"(?m)^#{1,6}\s+(.+?)\s*#*\s*$", markdown_body(text)):
        slug = re.sub(r"[^\w\-\s]", "", heading.lower()).replace(" ", "-")
        n = counts.get(slug, 0)
        counts[slug] = n + 1
        result.add(slug + (f"-{n}" if n else ""))
    return result


def check(repo):
    LOCATION.set("repository")
    repo = Path(repo).absolute()
    files = [repo / "AGENTS.md", *sorted((repo / "docs/Agent治理").glob("*.md")), *sorted((repo / "docs/开发规范").glob("*.md"))]
    files += sorted((repo / "docs/治理版本说明").glob("v3.0.1*.md"))
    expected_docs = {"Codex运行时拓扑", "多智能体协同开发治理流程", "治理配置与验收控制规范", "治理运行时事实协议", "开发溯源归档规范", "需求基线模板", "开发上下文包模板", "开发记录模板", "临时质量验证记录模板", "只读评审记录模板"}
    require(expected_docs <= {p.stem for p in (repo / "docs/Agent治理").glob("*.md")}, "governance document inventory")
    require({"项目开发总则", "测试规范"} <= {p.stem for p in (repo / "docs/开发规范").glob("*.md")}, "development standards inventory")
    texts = {p: safe_path(repo, p).read_text(encoding="utf-8-sig") for p in files}
    schemas = sorted((repo / "docs/Agent治理/contracts").glob("*.schema.json"))
    require({"control-profile.schema.json", "acceptance-plan.schema.json", "replay-expectations.schema.json"} == {path.name for path in schemas}, "v3 schema inventory")
    for schema in schemas:
        LOCATION.set(schema.relative_to(repo).as_posix())
        try:
            value = json.loads(safe_path(repo, schema).read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object)
        except json.JSONDecodeError:
            raise Invalid(f"{LOCATION.get()}: invalid JSON encoding or syntax") from None
        require(isinstance(value, dict) and value.get("$schema") == "https://json-schema.org/draft/2020-12/schema", "JSON Schema")
    rules = set()
    for text in texts.values():
        rules.update(re.findall(r"(?m)^\|\s*`?((?:ROUTE|[A-Z]{2,8})-\d{3})`?\s*\|", text))
    for path, text in texts.items():
        LOCATION.set(path.relative_to(repo).as_posix())
        body = markdown_body(text)
        width = None
        for line in body.splitlines():
            if line.strip().startswith("|"):
                columns = len(re.split(r"(?<!\\)\|", line.strip())) - 2
                require(width is None or width == columns, path.name + " table width")
                width = columns
            else:
                width = None
        for target in re.findall(r"(?<!!)\[[^\]\n]+\]\(([^)]+)\)", body):
            target = target.strip("<>")
            url = urlsplit(target)
            if url.scheme or url.netloc:
                continue
            local = safe_path(repo, (path.parent / unquote(url.path)).absolute()) if url.path else path
            require(local.exists(), path.name + " local link")
            if url.fragment and local.suffix == ".md":
                require(unquote(url.fragment) in anchors(local.read_text(encoding="utf-8-sig")), path.name + " anchor")
        for rule in re.findall(r"`((?:ROUTE|[A-Z]{2,8})-\d{3})`", body):
            if rule.startswith(("ROUTE-", "GEN-", "BE-", "FE-", "UI-", "TST-")):
                require(rule in rules, path.name + " RuleId reference")
    roles = sorted((repo / ".codex/agents").glob("*.toml"))
    require({"writer", "qa", "reviewer", "orchestrator", "development-trace"} <= {p.stem for p in roles}, "role inventory")
    for path in roles:
        LOCATION.set(path.relative_to(repo).as_posix())
        role = tomllib.loads(safe_path(repo, path).read_text(encoding="utf-8-sig"))
        require(role.get("name") == path.stem, "role name binding")
    return f"Checked {len(files)} Markdown files and {len(roles)} role TOML files (mechanical checks only).\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check").add_argument("--repo", required=True)
    initialization = commands.add_parser("init-developer", help="Set the local repository developer ID (writes Git config)")
    initialization.add_argument("--repo", required=True)
    initialization.add_argument("--developer-id", required=True)
    allocation = commands.add_parser("reserve-id", help="Reserve a new requirement ID (writes local Git metadata only)")
    allocation.add_argument("--repo", required=True)
    adjustment = commands.add_parser("validate-adjustment")
    adjustment.add_argument("--adjustment", required=True)
    quality = commands.add_parser("validate-adjusted-quality")
    quality.add_argument("--adjustment", required=True)
    quality.add_argument("--qa-result", required=True)
    quality.add_argument("--review-result", required=True)
    rendering = commands.add_parser("render")
    rendering.add_argument("--requirement-dir", required=True)
    rendering.add_argument("--kind", required=True, choices=("qa-index", "review-index", "changes"))
    validation = commands.add_parser("validate-plan")
    validation.add_argument("--control-profile", required=True)
    validation.add_argument("--acceptance-plan", required=True)
    validation.add_argument("--mode", choices=("live", "replay"), default="live")
    g2 = commands.add_parser("validate-g2")
    g2.add_argument("--control-profile", required=True)
    g2.add_argument("--acceptance-plan", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            output = check(args.repo)
        elif args.command == "init-developer":
            output = init_developer(args.repo, args.developer_id)
        elif args.command == "reserve-id":
            output = reserve_requirement(args.repo)
        elif args.command == "validate-adjustment":
            output = validate_adjustment(args.adjustment)
        elif args.command == "validate-adjusted-quality":
            output = validate_adjusted_quality(args.adjustment, args.qa_result, args.review_result)
        elif args.command == "render":
            output = render(args.requirement_dir, args.kind)
        elif args.command == "validate-plan":
            output = validate_plan(args.control_profile, args.acceptance_plan, args.mode)
        else:
            output = validate_g2(args.control_profile, args.acceptance_plan)
    except (Invalid, tomllib.TOMLDecodeError) as error:
        print(str(error) if isinstance(error, Invalid) else f"{LOCATION.get()}: Invalid TOML", file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print("Unable to read input", file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, sort_keys=True) if isinstance(output, dict) else output, end="\n" if isinstance(output, dict) else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
