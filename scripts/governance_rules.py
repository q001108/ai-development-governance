"""Read-only, pinned rule-pack composition and existing ApplicableRules projection.

RuleCompositionVersion is a tool format, not a new governance or fact version.
The tool never installs files, edits requirements, or creates acceptance/test controls.
"""

import argparse
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
import sys

sys.dont_write_bytecode = True
import governance_tools as g


VERSION = "v1.0"
CORE_SOURCE = "docs/开发规范/项目开发总则.md"
RULE_ID = r"[A-Z]{2,8}-[0-9]{3}"
IDENTIFIER = r"[a-z][a-z0-9.-]{0,63}"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def safe_text(text):
    g.sensitive(text)
    g.require(not re.search(r"-----BEGIN .*PRIVATE KEY|(?:password|api[_-]?key|access[_-]?token|cookie|authorization)\s*[\"']?\s*[:=]\s*[\"']?[^\s\"',}]{4,}|Bearer\s+[A-Za-z0-9._-]+", text, re.I), "sensitive raw source")


def relative_path(repo, value, *, directory=False, project_only=False):
    g.string(value, "repository-relative source")
    g.require("\\" not in value and not PureWindowsPath(value).drive and not value.startswith("/"), "POSIX source path")
    parts = value.split("/")
    g.require(all(part not in ("", ".", "..") and not re.search(r'[\x00-\x1f:*?<>|#%]', part) for part in parts), "source path components")
    g.require(not any(part.lower() in {".git", "secrets", "credentials", "secrets.json", "credentials.json", "id_rsa", "id_ed25519"}
                      or part.lower().startswith(".env") or Path(part).suffix.lower() in {".pem", ".key", ".pfx", ".p12"} for part in parts), "non-sensitive source")
    path = g.safe_path(repo, repo.joinpath(*parts))
    if not directory and not project_only:
        path = g.resource_path(repo, path)
    g.require(path.is_dir() if directory else path.is_file(), "source exists with required kind")
    return path


def strings(value, field, *, pattern=None, minimum=0):
    g.unique_strings(value, field, minimum=minimum)
    if pattern is not None:
        g.require(all(re.fullmatch(pattern, item) for item in value), field)
    return value


def read_source(repo, source, pins, *, expected=None, project_only=False):
    path = relative_path(repo, source, project_only=project_only)
    g.require(path.stat().st_size <= 512 * 1024, "rule source size limit")
    data = path.read_bytes()
    digest = sha256(data)
    if expected is not None:
        g.require(isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected) and expected == digest, "source SHA-256 binding")
    g.require(source not in pins or pins[source] == digest, "source changed during composition")
    pins[source] = digest
    text = data.decode("utf-8-sig")
    safe_text(text)
    return text


def read_json(repo, source, pins, *, expected=None):
    g.require(isinstance(source, str) and source.endswith(".json"), "JSON source extension")
    value = json.loads(read_source(repo, source, pins, expected=expected), object_pairs_hook=g.unique_object)
    g.require(isinstance(value, dict), "JSON source object")
    g.sensitive(value)
    return value


def definitions(text):
    """Use existing RuleId table rows, not incidental references or code examples."""
    text = g.markdown_body(text)
    result, heading = {}, None
    for line in text.splitlines():
        title = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if title:
            heading = title[1]
        match = re.match(r"^\|\s*`?(" + RULE_ID + r")`?\s*\|(.+)\|\s*$", line)
        if match:
            g.require(heading is not None and match[1] not in result, "unique RuleId definition")
            result[match[1]] = {"Section": heading, "Definition": match[2]}
    return result


def section_anchor(text, section):
    g.string(section, "rule section")
    titles = re.findall(r"(?m)^#{1,6}\s+(.+?)\s*#*\s*$", g.markdown_body(text))
    g.require(titles.count(section) == 1, "unique referenced section")
    anchor = re.sub(r"[^\w\-\s]", "", section.lower()).replace(" ", "-")
    g.require(anchor in g.anchors(text), "section anchor")
    return anchor


def parameter(repo, name, value, definition, pins):
    g.require(isinstance(value, str) and value and len(value) <= 1024 and not re.search(r"[\x00-\x1f]", value), "single-line parameter value")
    safe_text(value)
    kind = definition.get("Kind") if isinstance(definition, dict) else None
    if kind == "FILE_REF":
        g.exact_object(definition, "file parameter", ("Kind",))
        source, separator, anchor = value.partition("#")
        path = relative_path(repo, source, project_only=True)
        text = read_source(repo, source, pins, project_only=True)
        if separator:
            g.require(path.suffix == ".md" and anchor in g.anchors(text), "parameter source anchor")
    elif kind == "CHOICE":
        g.exact_object(definition, "choice parameter", ("Kind", "Values"))
        strings(definition["Values"], "parameter choices", minimum=1)
        g.require(value in definition["Values"], "configured parameter choice")
    else:
        g.require(False, "supported parameter kind")
    return {"Name": name, "Value": value, "Kind": kind}


def compose_rules(repo, project_config, affected_modules=None, direct_rule_ids=None):
    """Resolve fixed sources and project selected modules without mutating any file."""
    repo = Path(repo).absolute()
    g.require(not str(repo).replace("\\", "/").startswith("//"), "local repository root")
    g.safe_path(repo, repo)
    g.require(repo.is_dir(), "repository root")
    pins = {}
    project = read_json(repo, project_config, pins)
    g.exact_object(project, "rule project declaration", ("RuleCompositionVersion", "GovernanceVersion", "Core", "Modules", "Packs"))
    g.require(project["RuleCompositionVersion"] == VERSION, "rule composition format")
    g.require(project["GovernanceVersion"] in ("v3.0", "v3.0.1", "v3.0.2"), "existing governance version")
    g.exact_object(project["Core"], "core binding", ("Source", "Sha256"))
    g.require(project["Core"]["Source"] == CORE_SOURCE, "existing core rule source")
    core_text = read_source(repo, CORE_SOURCE, pins, expected=project["Core"]["Sha256"])
    core = definitions(core_text)
    g.require(bool(core) and all(ident.startswith("GEN-") for ident in core), "existing GEN core rules")
    existing = {ident: CORE_SOURCE for ident in core}
    formal_documents = g.resource_glob(repo, "docs/开发规范", "*.md")
    g.require(len(formal_documents) <= 128, "existing development standards count")
    for source in formal_documents:
        if source == CORE_SOURCE:
            continue
        text = read_source(repo, source, pins)
        for ident in definitions(text):
            g.require(ident not in existing, "duplicate existing formal RuleId")
            existing[ident] = source
    module_values = g.array(project["Modules"], "project modules")
    g.require(1 <= len(module_values) <= 64, "project module count")
    modules, roots = {}, []
    for module in module_values:
        g.exact_object(module, "module declaration", ("ModuleId", "Root", "Capabilities", "Parameters"))
        ident = module["ModuleId"]
        g.require(isinstance(ident, str) and re.fullmatch(IDENTIFIER, ident) and ident not in modules, "unique module ID")
        root = relative_path(repo, module["Root"], directory=True)
        g.require(not any(root.is_relative_to(other) or other.is_relative_to(root) for other in roots), "non-overlapping module roots")
        roots.append(root)
        strings(module["Capabilities"], "module capabilities", pattern=IDENTIFIER, minimum=1)
        g.require(isinstance(module["Parameters"], dict), "module parameters")
        g.require(all(re.fullmatch(r"[A-Z][A-Za-z0-9]{0,63}", name) for name in module["Parameters"]), "parameter names")
        modules[ident] = module
    if affected_modules is None:
        selected = sorted(modules)
    else:
        strings(affected_modules, "affected module selection", pattern=IDENTIFIER, minimum=1)
        g.require(set(affected_modules) <= set(modules), "declared affected modules")
        selected = sorted(affected_modules)
    direct = strings([] if direct_rule_ids is None else direct_rule_ids, "directly relevant existing RuleIds", pattern=RULE_ID)
    bindings = g.array(project["Packs"], "fixed rule packs")
    g.require(len(bindings) <= 32, "rule pack count")
    pack_ids, rule_ids, loaded, parameter_definitions = set(), set(core), [], {}
    for binding in bindings:
        g.exact_object(binding, "fixed pack binding", ("PackId", "Version", "Source", "Sha256"))
        g.require(isinstance(binding["PackId"], str) and re.fullmatch(IDENTIFIER, binding["PackId"]), "pack ID")
        g.require(binding["PackId"] not in pack_ids, "single pinned version per pack")
        pack_ids.add(binding["PackId"])
        g.require(isinstance(binding["Version"], str) and re.fullmatch(r"[1-9]\d*\.\d+\.\d+", binding["Version"]), "fixed pack version")
        pack = read_json(repo, binding["Source"], pins, expected=binding["Sha256"])
        g.exact_object(pack, "rule pack", ("RulePackFormatVersion", "PackId", "Version", "GovernanceVersions", "Document", "Parameters", "Rules"))
        g.require(pack["RulePackFormatVersion"] == VERSION and pack["PackId"] == binding["PackId"] and pack["Version"] == binding["Version"], "pack identity and format binding")
        strings(pack["GovernanceVersions"], "compatible governance versions", minimum=1)
        g.require(set(pack["GovernanceVersions"]) <= {"v3.0", "v3.0.1", "v3.0.2"}, "known governance compatibility")
        g.require(project["GovernanceVersion"] in pack["GovernanceVersions"], "pack/governance compatibility")
        g.exact_object(pack["Document"], "rule document binding", ("Source", "Sha256"))
        g.require(isinstance(pack["Document"]["Source"], str) and pack["Document"]["Source"].endswith(".md"), "rule document extension")
        text = read_source(repo, pack["Document"]["Source"], pins, expected=pack["Document"]["Sha256"])
        document_rules = definitions(text)
        g.require(isinstance(pack["Parameters"], dict), "pack parameter definitions")
        g.require(all(re.fullmatch(r"[A-Z][A-Za-z0-9]{0,63}", name) for name in pack["Parameters"]), "pack parameter names")
        for name, definition in pack["Parameters"].items():
            g.require(isinstance(definition, dict), "parameter definition object")
            if definition.get("Kind") == "FILE_REF":
                g.exact_object(definition, "file parameter definition", ("Kind",))
            else:
                g.exact_object(definition, "choice parameter definition", ("Kind", "Values"))
                g.require(definition["Kind"] == "CHOICE", "supported parameter definition")
                strings(definition["Values"], "parameter choice values", minimum=1)
            g.require(name not in parameter_definitions or parameter_definitions[name] == definition, "unambiguous cross-pack parameter definition")
            parameter_definitions[name] = definition
        rules = g.array(pack["Rules"], "pack rules")
        g.require(1 <= len(rules) <= 128, "pack rule count")
        declared, consumed_parameters = set(), set()
        for rule in rules:
            g.exact_object(rule, "rule metadata", ("RuleId", "Section", "RequiresAll", "Parameters", "ConflictsWith"))
            ident = rule["RuleId"]
            g.require(isinstance(ident, str) and re.fullmatch(RULE_ID, ident), "existing RuleId shape")
            g.require(ident.split("-", 1)[0] not in {"GEN", "TST", "ROUTE", "BA", "RB", "QG", "AD"}, "reserved governance/control namespace")
            g.require(ident not in existing or existing[ident] == pack["Document"]["Source"], "package conflicts with existing formal rule source")
            g.require(ident not in rule_ids and ident not in declared, "RuleId conflict or core override")
            declared.add(ident)
            g.require(ident in document_rules and document_rules[ident]["Section"] == rule["Section"], "rule/document section binding")
            rule["_anchor"] = section_anchor(text, rule["Section"])
            strings(rule["RequiresAll"], "rule applicability", pattern=IDENTIFIER, minimum=1)
            strings(rule["Parameters"], "rule parameter references", pattern=r"[A-Z][A-Za-z0-9]{0,63}")
            strings(rule["ConflictsWith"], "explicit conflicting RuleIds", pattern=RULE_ID)
            g.require(set(rule["Parameters"]) <= set(pack["Parameters"]), "declared parameter references")
            placeholders = set(re.findall(r"\{\{([A-Z][A-Za-z0-9]{0,63})\}\}", document_rules[ident]["Definition"]))
            g.require(placeholders == set(rule["Parameters"]), "rule template parameter coverage")
            consumed_parameters.update(rule["Parameters"])
        g.require(declared == set(document_rules), "rule document complete metadata coverage")
        g.require(consumed_parameters == set(pack["Parameters"]), "parameter definition coverage")
        rule_ids.update(declared)
        loaded.append((binding, pack))
    resolved, applicability = [], []
    for ident, definition in sorted(core.items()):
        resolved.append({"RuleId": ident, "PackId": "governance-core", "PackVersion": project["GovernanceVersion"],
                         "Source": CORE_SOURCE, "SourceSha256": pins[CORE_SOURCE], "Section": definition["Section"],
                         "Anchor": section_anchor(core_text, definition["Section"]), "Modules": selected,
                         "Parameters": [], "Reason": "existing governance core", "DirectlyRelevant": ident in direct})
    for binding, pack in sorted(loaded, key=lambda value: value[0]["PackId"]):
        for rule in pack["Rules"]:
            g.require(set(rule["ConflictsWith"]) <= rule_ids, "known conflict references")
            applies = [ident for ident in selected if set(rule["RequiresAll"]) <= set(modules[ident]["Capabilities"])]
            applicability.append({"PackId": pack["PackId"], "RuleId": rule["RuleId"], "Modules": applies,
                                  "Status": "APPLICABLE" if applies else "NOT_APPLICABLE",
                                  "RequiresAll": rule["RequiresAll"]})
            if not applies:
                continue
            values = []
            for module_id in applies:
                for name in rule["Parameters"]:
                    g.require(name in modules[module_id]["Parameters"], "configured applicable rule parameter")
                    value = parameter(repo, name, modules[module_id]["Parameters"][name], pack["Parameters"][name], pins)
                    values.append({**value, "ModuleId": module_id, "DeclaredAt": project_config + "#Modules." + module_id + ".Parameters." + name})
            resolved.append({"RuleId": rule["RuleId"], "PackId": pack["PackId"], "PackVersion": pack["Version"],
                             "Source": pack["Document"]["Source"], "SourceSha256": pack["Document"]["Sha256"],
                             "Section": rule["Section"], "Anchor": rule["_anchor"], "Modules": applies, "Parameters": values,
                             "Reason": "requires capabilities: " + ", ".join(rule["RequiresAll"]), "DirectlyRelevant": rule["RuleId"] in direct})
    active_ids = {rule["RuleId"] for rule in resolved}
    for _binding, pack in loaded:
        for rule in pack["Rules"]:
            g.require(rule["RuleId"] not in active_ids or not (set(rule["ConflictsWith"]) & active_ids), "simultaneously applicable rule conflict")
    g.require(set(direct) <= active_ids, "direct RuleIds must be applicable")
    groups = {}
    for rule in resolved:
        key = (rule["Source"], rule["Section"], rule["Anchor"], rule["PackId"], rule["PackVersion"])
        group = groups.setdefault(key, {"Direct": [], "Modules": set()})
        group["Modules"].update(rule["Modules"])
        if rule["DirectlyRelevant"]:
            group["Direct"].append(rule["RuleId"])
    rows = []
    for (source, section, anchor, pack_id, version), group in sorted(groups.items()):
        rows.append([source + "#" + anchor, section, ", ".join(sorted(group["Direct"])) or "N/A", "N/A",
                     pack_id + "@" + version + "; modules: " + ", ".join(sorted(group["Modules"])) + "; chapter defaults apply"])
    markdown = "| 规范文档 | 适用章节 | 直接相关 RuleId | 关联控制项 | 本需求适用说明 |\n|---|---|---|---|---|\n"
    markdown += "".join("| " + " | ".join(g.cell(value) for value in row) + " |\n" for row in rows)
    for source, expected in pins.items():
        g.require(sha256(relative_path(repo, source).read_bytes()) == expected, "source changed during composition")
    return {"ResultType": "RULE_COMPOSITION_PREVIEW", "RuleCompositionVersion": VERSION,
            "GovernanceVersion": project["GovernanceVersion"], "Scope": "PROJECT_PREVIEW" if affected_modules is None else "AFFECTED_MODULES",
            "SelectedModules": selected, "RequirementBound": False, "GatesChanged": False, "FilesWritten": False,
            "AcceptanceControlsCreated": False, "ValidationSetExpanded": False, "Sources": pins,
            "ResolvedRules": resolved, "Applicability": applicability, "ApplicableRules": rows,
            "ApplicableRulesMarkdown": markdown,
            "PlanSourceRefs": {rule["RuleId"]: (rule["Source"] if rule["Source"].startswith(("docs/", "scripts/", ".codex/"))
                                                 or rule["Source"] == "AGENTS.md" else "../../../" + rule["Source"])
                               + "#" + rule["Anchor"] for rule in resolved},
            "PlanSourceRefBase": "docs/requirements/<RequirementId>/"}


def verify_composition_sources(repo, projection):
    """Check an already saved preview's source pins; never resolve to newer packages."""
    repo = Path(repo).absolute()
    g.safe_path(repo, repo)
    g.require(isinstance(projection, dict) and projection.get("ResultType") == "RULE_COMPOSITION_PREVIEW"
              and projection.get("RuleCompositionVersion") == VERSION, "saved rule projection format")
    sources = projection.get("Sources")
    g.require(isinstance(sources, dict) and bool(sources), "saved source pins")
    checked = {}
    for source, expected in sources.items():
        read_source(repo, source, checked, expected=expected)
    return {"ResultType": "RULE_SOURCE_VERIFICATION", "RuleCompositionVersion": VERSION,
            "SourcesChecked": len(checked), "Valid": True, "GatesChanged": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.error = lambda _message: parser.exit(2, "Invalid rule composition arguments.\n")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--module", action="append")
    parser.add_argument("--direct-rule", action="append")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args(argv)
    try:
        result = compose_rules(args.repo, args.project, args.module, args.direct_rule)
    except (g.Invalid, OSError, UnicodeError, ValueError, TypeError):
        print("Unable to compose rules; check pinned sources, applicability and project parameters.", file=sys.stderr)
        return 2
    print(result["ApplicableRulesMarkdown"] if args.format == "markdown" else json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
