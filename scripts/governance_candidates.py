"""B2 byte-preserving, scoped candidate snapshots; never registers candidate facts."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import stat
import subprocess
import sys
import uuid

sys.dont_write_bytecode = True
import governance_tools as g

FORMAT = "v1.0"
UNSPECIFIED = object()
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
GOVERNANCE_FILES = ("control-profile.json", "acceptance-plan.json", "开发上下文包.md", "rules-projection.json", "plugin-binding.json", "tracking.json", "tracking/head.json")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def git(repo, *args):
    child_env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    child_env.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1")
    result = subprocess.run(["git", "--no-pager", "-c", "core.fsmonitor=false", "-C", str(repo), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=child_env,
                            timeout=20, check=False)
    g.require(result.returncode == 0, "local Git snapshot query")
    g.require(len(result.stdout) <= MAX_TOTAL_BYTES, "Git query size")
    return result.stdout


def repository(repo):
    value = str(repo)
    g.require(not value.replace("\\", "/").startswith("//"), "local repository")
    root = Path(repo).absolute()
    g.safe_path(root, root)
    g.require(Path(git(root, "rev-parse", "--show-toplevel").decode("utf-8").strip()).resolve() == root.resolve(), "working tree root")
    return root


def relative(root, value):
    g.string(value, "repository path")
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    g.require(not normalized.startswith("/") and not PureWindowsPath(value).drive, "relative path")
    g.require(all(part not in ("", ".", "..") and not re.search(r'[\x00-\x1f:*?<>|]', part) for part in parts), "safe path components")
    g.sensitive(normalized)
    return g.safe_path(root, root.joinpath(*parts))


def delivery_path(root, value):
    path = relative(root, value)
    parts = {part.lower() for part in Path(value.replace("\\", "/")).parts}
    blocked = {".git", ".parallel", "node_modules", ".venv", "venv", "target", "dist", "build", "coverage", ".playwright-cli", "logs", "runtime-data"}
    name = path.name.lower()
    g.require(not parts & blocked, "non-delivery runtime directory")
    tokens = set(re.split(r"[._-]+", name))
    sensitive_data_name = bool(tokens & {"credential", "credentials", "secret", "secrets", "password", "token", "tokens"}) and path.suffix.lower() in {"", ".json", ".yaml", ".yml", ".toml", ".ini", ".conf", ".config", ".txt", ".csv"}
    g.require(not (sensitive_data_name
                   or name == ".env" or name.startswith((".env.", ".env-")) or name in {"id_rsa", "id_ed25519"}
                   or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".db", ".sqlite", ".sqlite3", ".log"}), "non-sensitive delivery filename")
    return path


def safe_content(data):
    g.require(len(data) <= MAX_FILE_BYTES, "delivery file size")
    def scan_view(text):
        # Inspect the source text itself; serializing it as a JSON string escapes
        # JSON quotes and can hide the original credential assignment syntax.
        pattern = (r"-----BEGIN .*PRIVATE KEY|(?:password|api[_-]?key|access[_-]?token|cookie|authorization)"
                   r"\s*[\"']?\s*[:=]\s*[\"']?[^\s\"',}]{4,}|Bearer\s+[A-Za-z0-9._-]+")
        g.require(not re.search(pattern, text, re.I), "sensitive candidate content")
        g.sensitive(text)
    # Scan ASCII/UTF-8 credential patterns even in binary assets; never log the data.
    scan_view(data.decode("utf-8", errors="replace"))
    if data.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")):
        try:
            scan_view(data.decode("utf-32" if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")) else "utf-16"))
        except UnicodeError:
            g.require(False, "supported sensitive content encoding")
    if b"\x00" in data:
        # Inspection views only: the stored/hashed bytes below are never transcoded.
        # Replacement also keeps an appended partial code unit from hiding a credential.
        for encoding in ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
            scan_view(data.decode(encoding, errors="replace"))
        # Cover common NUL-padded ASCII fields even when a binary prefix shifts alignment.
        scan_view(data.replace(b"\x00", b"").decode("utf-8", errors="replace"))
    return data


def tree(repo, revision):
    result = {}
    for record in git(repo, "ls-tree", "-r", "-z", revision).split(b"\0"):
        if not record:
            continue
        metadata, name = record.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split()
        result[name.decode("utf-8")] = {"Mode": mode, "Kind": kind, "BlobId": oid}
    return result


def index(repo):
    result = {}
    for record in git(repo, "ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        metadata, name = record.split(b"\t", 1)
        mode, oid, stage = metadata.decode("ascii").split()
        path = name.decode("utf-8")
        g.require(stage == "0" and path not in result, "resolved unique index entries")
        result[path] = {"Mode": mode, "BlobId": oid}
    return result


def renames(repo, revision):
    records = git(repo, "diff", "--cached", "--name-status", "-z", "--find-renames", "--no-ext-diff", "--no-textconv", revision, "--").split(b"\0")
    result = {}
    position = 0
    while position < len(records) and records[position]:
        status_value = records[position].decode("ascii")
        position += 1
        old = records[position].decode("utf-8")
        position += 1
        if status_value.startswith(("R", "C")):
            new = records[position].decode("utf-8")
            position += 1
            if status_value.startswith("R"):
                result[new] = old
    return result


def handoffs(repo, refs, requirement_id, context_version, repair_lineage_ref):
    g.unique_strings(list(refs), "writer handoff references", minimum=1)
    paths, bindings, operations = set(), [], {}
    expected_root = repo / "docs/requirements" / requirement_id / "facts/writers"
    fact_root = g.safe_path(repo, expected_root.parent)
    correction_candidates = list(fact_root.rglob("*.json"))
    g.require(len(correction_candidates) <= 2048, "fact inspection limit")
    for candidate in correction_candidates:
        raw = g.safe_path(repo, candidate).read_bytes()
        g.require(len(raw) <= 1024 * 1024, "fact inspection size")
        event = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=g.unique_object)
        g.require(isinstance(event, dict) and event.get("EventType") != "FACT_CORRECTION" and event.get("SupersedesEventRef") is None, "correction requires runtime resolution")
    for ref in refs:
        path = relative(repo, ref)
        g.require(path.is_relative_to(expected_root) and path.suffix == ".json", "requirement writer fact path")
        data = path.read_bytes()
        g.require(len(data) <= 1024 * 1024, "writer fact size")
        event = json.loads(data.decode("utf-8-sig"), object_pairs_hook=g.unique_object)
        g.validate_event_structure(event, requirement_id, path)
        g.require(event["EventType"] == "WRITER_HANDOFF" and event["SupersedesEventRef"] is None, "uncorrected writer handoff")
        g.require(event["ContextPackVersion"] == context_version and event["RepairLineageRef"] == repair_lineage_ref, "writer context binding")
        g.require(event["Payload"].get("Status") == "COMPLETED", "completed writer handoff")
        changes = g.array(event["Payload"].get("Changes"), "writer changes")
        for change in changes:
            g.require(isinstance(change, dict), "writer change")
            value = change.get("Path")
            delivery_path(repo, value)
            operation = change.get("Operation")
            g.require(operation in {"ADD", "MODIFY", "DELETE", "RENAME", "MOVE"}, "writer operation")
            g.require(value not in paths, "unambiguous writer path ownership")
            paths.add(value)
            operations[value] = operation
        bindings.append({"Ref": path.relative_to(repo).as_posix(), "SHA256": sha256(data), "EventId": event["EventId"]})
    g.require(paths, "nonempty writer delivery")
    return paths, bindings, operations


def collect(repo, revision, scope, declared_untracked):
    base = tree(repo, revision)
    staged = index(repo)
    actual_untracked = {item.decode("utf-8") for item in git(repo, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if item}
    files, blobs = [], {}
    for name in sorted(scope):
        path = delivery_path(repo, name)
        prior = base.get(name)
        if prior:
            g.require(prior["Kind"] == "blob" and prior["Mode"] in {"100644", "100755"}, "regular baseline file")
            data = safe_content(git(repo, "cat-file", "blob", prior["BlobId"]))
            prior = {"Mode": prior["Mode"], "BlobId": prior["BlobId"], "SHA256": sha256(data), "Size": len(data)}
            blobs[prior["SHA256"]] = data
        current = None
        if path.exists():
            g.require(path.is_file(), "regular delivery file")
            data = safe_content(path.read_bytes())
            mode = ("100755" if path.stat().st_mode & stat.S_IXUSR else "100644") if os.name != "nt" else staged.get(name, {}).get("Mode", "100644")
            g.require(mode in {"100644", "100755"}, "regular index file")
            current = {"Mode": mode, "SHA256": sha256(data), "Size": len(data)}
            blobs[current["SHA256"]] = data
        untracked = name in actual_untracked
        g.require(not untracked or name in declared_untracked, "undeclared untracked delivery")
        g.require(name not in declared_untracked or untracked, "declared untracked must be present and non-ignored")
        g.require(current is not None or prior is not None, "existing or deleted delivery path")
        g.require(prior is None or current is None or prior["SHA256"] != current["SHA256"] or prior["Mode"] != current["Mode"], "delivery path has actual raw-byte change")
        g.require(prior is not None or untracked or name in staged, "included file is tracked or explicit untracked")
        files.append({"Path": name, "Baseline": prior, "Current": current, "Index": staged.get(name), "Untracked": untracked})
    g.require(sum(len(value) for value in blobs.values()) <= MAX_TOTAL_BYTES, "snapshot total size")
    return files, blobs


def governance_inputs(repo, requirement_id):
    result = {}
    for name in GOVERNANCE_FILES:
        path = relative(repo, "docs/requirements/" + requirement_id + "/" + name)
        if path.exists():
            g.require(path.is_file() and path.stat().st_size <= 8 * 1024 * 1024, "governance input file")
            result[path.relative_to(repo).as_posix()] = sha256(path.read_bytes())
    return result


def capture_snapshot(repo, *, requirement_id, context_version, repair_lineage_ref, baseline_revision,
                     writer_handoff_refs, scope_paths, untracked_paths=(), output_root=None, expected_files=None):
    repo = repository(repo)
    g.require(g.valid_requirement_id(requirement_id), "RequirementId")
    if (repo / "docs/requirements" / requirement_id / "tracking.json").exists():
        from governance_requirements import require_governed
        require_governed(repo, requirement_id, execution=True)
    g.require(isinstance(context_version, str) and re.fullmatch(r"v[1-9]\d*\.\d+", context_version), "context version")
    g.require(repair_lineage_ref is None or bool(g.string(repair_lineage_ref, "repair lineage")), "repair lineage")
    g.require(isinstance(baseline_revision, str) and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", baseline_revision), "pinned baseline commit")
    revision = git(repo, "rev-parse", "--verify", baseline_revision + "^{commit}").decode("ascii").strip()
    scope_paths, untracked_paths, writer_handoff_refs = list(scope_paths), list(untracked_paths), list(writer_handoff_refs)
    g.unique_strings(scope_paths, "exact delivery scope", minimum=1)
    g.unique_strings(untracked_paths, "explicit untracked delivery")
    scope, untracked = set(scope_paths), set(untracked_paths)
    g.require(len(scope) <= 4096 and untracked <= scope, "delivery scope")
    expected, handoff_bindings, operations = handoffs(repo, writer_handoff_refs, requirement_id, context_version, repair_lineage_ref)
    input_bindings = governance_inputs(repo, requirement_id)
    initial_index = index(repo)
    rename_sources = renames(repo, revision)
    rename_entries = []
    for name, operation in operations.items():
        if operation in {"RENAME", "MOVE"}:
            g.require(name in rename_sources, "rename source must be observable or reported as explicit delete/add")
            expected.add(rename_sources[name])
            rename_entries.append({"From": rename_sources[name], "To": name})
    g.require(scope == expected, "complete scope and writer handoff agreement")
    managed = repo / "docs/requirements" / requirement_id / "snapshots"
    local_storage = output_root is not None and Path(output_root).absolute() != managed
    head = git(repo, "rev-parse", "HEAD").decode("ascii").strip()
    files, blobs = collect(repo, revision, scope, untracked)
    if expected_files is not None:
        g.require(isinstance(expected_files, dict) and set(expected_files) == scope, "pinned writer content set")
        g.require(all(expected_files[item["Path"]] == (item["Current"]["SHA256"] if item["Current"] else None) for item in files), "pinned writer bytes")
    for item in files:
        operation = operations.get(item["Path"])
        g.require(operation != "ADD" or item["Baseline"] is None, "writer ADD agrees with baseline")
        g.require(operation != "DELETE" or item["Current"] is None, "writer DELETE agrees with current bytes")
        g.require(operation not in {"MODIFY", "RENAME", "MOVE"} or item["Current"] is not None, "writer current file exists")
    manifest = {"SnapshotFormatVersion": FORMAT, "RequirementId": requirement_id, "ContextPackVersion": context_version,
                "RepairLineageRef": repair_lineage_ref, "BaseCommitSHA": revision, "CandidateCommitSHA": None,
                "WorkspaceRoot": str(repo.resolve()), "ScopePaths": sorted(scope), "UntrackedDeliveryPaths": sorted(untracked),
                "WriterHandoffs": sorted(handoff_bindings, key=lambda item: item["Ref"]), "Files": files, "Renames": sorted(rename_entries, key=lambda item: item["To"]),
                "ByteDigestAlgorithm": "SHA256", "ChangeAttribution": "CALLER_SCOPED_NOT_AUTHORSHIP_PROVEN",
                "ExpectedFileDigestsChecked": expected_files is not None, "GovernanceInputs": input_bindings, "LocalOnly": local_storage}
    serialized = canonical(manifest)
    identity = sha256(serialized)
    # All source inputs are reread before any snapshot is published.
    final_files, final_blobs = collect(repo, revision, scope, untracked)
    _, final_handoffs, _ = handoffs(repo, writer_handoff_refs, requirement_id, context_version, repair_lineage_ref)
    g.require(files == final_files and blobs == final_blobs and final_handoffs == handoff_bindings and initial_index == index(repo)
              and input_bindings == governance_inputs(repo, requirement_id)
              and head == git(repo, "rev-parse", "HEAD").decode("ascii").strip(), "stable candidate collection")
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir").decode("utf-8").strip())
    destination = Path(output_root).absolute() if output_root is not None else managed
    g.require(not str(destination).replace("\\", "/").startswith("//"), "local snapshot destination")
    g.safe_path(destination, destination)
    # Do not write snapshots into the source delivery tree.
    g.require(destination == managed or destination.is_relative_to(git_dir) or not destination.is_relative_to(repo), "managed snapshot destination")
    g.require(not any(relative(repo, name).is_relative_to(managed) for name in scope), "delivery cannot contain snapshot storage")
    destination.mkdir(parents=True, exist_ok=True)
    target = g.safe_path(destination, destination / identity)
    if target.exists():
        verify_snapshot(target, expected_snapshot_digest=identity)
    else:
        pending = g.safe_path(destination, destination / (".pending-" + uuid.uuid4().hex))
        pending.mkdir()
        try:
            (pending / "blobs").mkdir()
            for name, data in blobs.items():
                with (pending / "blobs" / name).open("xb") as stream:
                    stream.write(data)
            with (pending / "snapshot.json").open("xb") as stream:
                stream.write(serialized)
            try:
                pending.rename(target)
            except FileExistsError:
                verify_snapshot(target, expected_snapshot_digest=identity)
        finally:
            if pending.exists():
                g.require(pending.parent.resolve() == destination.resolve() and pending.name.startswith(".pending-"), "owned pending snapshot")
                g.safe_path(destination, pending)
                shutil.rmtree(pending)
    report = verify_snapshot(target, expected_snapshot_digest=identity)
    snapshot_ref = (target / "snapshot.json").relative_to(repo).as_posix() if target.is_relative_to(repo) and not target.is_relative_to(git_dir) else str(target / "snapshot.json")
    local_only = manifest["LocalOnly"]
    report["CandidateFactDraft"] = {"DraftOnly": True, "RequirementId": requirement_id, "ContextPackVersion": context_version,
                                    "RepairLineageRef": repair_lineage_ref, "CandidateRef": None,
                                    "Payload": {"SnapshotFormatVersion": FORMAT, "CandidateSnapshotRef": snapshot_ref,
                                                "CandidateSnapshotDigest": identity, "BaseCommitSHA": revision, "CandidateCommitSHA": None,
                                                "ChangedPaths": sorted(scope), "UntrackedDeliveryPaths": sorted(untracked),
                                                "WriterHandoffRefs": [item["Ref"] for item in handoff_bindings], "WorkspaceRoot": str(repo.resolve()),
                                                "LocalOnly": local_only, "CannotArchiveAsFact": local_only,
                                                "DraftOnly": True, "QualityPassed": False}}
    return report


def verify_snapshot(snapshot_dir, *, repo=None, requirement_id=None, context_version=None,
                    repair_lineage_ref=UNSPECIFIED, expected_snapshot_digest=None, verify_worktree=False):
    root = Path(snapshot_dir).absolute()
    root = root.parent if root.name == "snapshot.json" else root
    g.safe_path(root, root)
    path = g.safe_path(root, root / "snapshot.json")
    data = path.read_bytes()
    g.require(len(data) <= 8 * 1024 * 1024, "snapshot manifest size")
    manifest = json.loads(data.decode("utf-8"), object_pairs_hook=g.unique_object)
    g.exact_object(manifest, "snapshot manifest", ("SnapshotFormatVersion", "RequirementId", "ContextPackVersion", "RepairLineageRef", "BaseCommitSHA",
                   "CandidateCommitSHA", "WorkspaceRoot", "ScopePaths", "UntrackedDeliveryPaths", "WriterHandoffs", "Files", "Renames", "ByteDigestAlgorithm", "ChangeAttribution", "ExpectedFileDigestsChecked", "GovernanceInputs", "LocalOnly"))
    g.sensitive(manifest)
    identity = sha256(data)
    g.require(data == canonical(manifest) and root.name == identity, "immutable snapshot identity")
    g.require(manifest["SnapshotFormatVersion"] == FORMAT and manifest["ByteDigestAlgorithm"] == "SHA256", "supported snapshot format")
    g.require(expected_snapshot_digest is None or identity == expected_snapshot_digest, "expected snapshot digest")
    for requested, actual in ((requirement_id, manifest["RequirementId"]), (context_version, manifest["ContextPackVersion"])):
        g.require(requested is None or requested == actual, "snapshot scope binding")
    g.require(repair_lineage_ref is UNSPECIFIED or repair_lineage_ref == manifest["RepairLineageRef"], "snapshot lineage binding")
    g.require(g.valid_requirement_id(manifest["RequirementId"]), "snapshot RequirementId")
    g.require(isinstance(manifest["GovernanceInputs"], dict), "governance input bindings")
    prefix = "docs/requirements/" + manifest["RequirementId"] + "/"
    g.require(set(manifest["GovernanceInputs"]) <= {prefix + name for name in GOVERNANCE_FILES}, "known governance input sources")
    g.require(all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in manifest["GovernanceInputs"].values()), "governance raw-byte digest")
    complete_governance = {prefix + name for name in GOVERNANCE_FILES[:3]} <= set(manifest["GovernanceInputs"])
    g.require(re.fullmatch(r"v[1-9]\d*\.\d+", manifest["ContextPackVersion"]), "snapshot context version")
    g.require(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", manifest["BaseCommitSHA"]) and manifest["CandidateCommitSHA"] is None, "snapshot commit metadata")
    g.require(manifest["RepairLineageRef"] is None or isinstance(manifest["RepairLineageRef"], str) and bool(manifest["RepairLineageRef"].strip()), "snapshot lineage")
    g.require(Path(manifest["WorkspaceRoot"]).is_absolute() and not manifest["WorkspaceRoot"].replace("\\", "/").startswith("//"), "snapshot workspace metadata")
    g.require(manifest["ChangeAttribution"] == "CALLER_SCOPED_NOT_AUTHORSHIP_PROVEN" and type(manifest["ExpectedFileDigestsChecked"]) is bool, "snapshot attribution boundary")
    g.require(type(manifest["LocalOnly"]) is bool, "snapshot storage boundary")
    g.unique_strings(manifest["ScopePaths"], "snapshot paths", minimum=1)
    g.require(len(manifest["ScopePaths"]) <= 4096, "snapshot path limit")
    g.unique_strings(manifest["UntrackedDeliveryPaths"], "snapshot untracked paths")
    g.require(set(manifest["UntrackedDeliveryPaths"]) <= set(manifest["ScopePaths"]), "snapshot untracked membership")
    g.require(manifest["ScopePaths"] == sorted(manifest["ScopePaths"]) and manifest["UntrackedDeliveryPaths"] == sorted(manifest["UntrackedDeliveryPaths"]), "ordered snapshot scope")
    g.require(isinstance(manifest["WriterHandoffs"], list) and bool(manifest["WriterHandoffs"]), "bound writer handoffs")
    writer_refs = []
    for handoff in manifest["WriterHandoffs"]:
        g.exact_object(handoff, "bound handoff", ("Ref", "SHA256", "EventId"))
        relative(root, handoff["Ref"])
        g.require(handoff["Ref"].startswith("docs/requirements/" + manifest["RequirementId"] + "/facts/writers/"), "bound handoff requirement")
        g.require(re.fullmatch(r"[0-9a-f]{64}", handoff["SHA256"]) and Path(handoff["Ref"]).stem == handoff["EventId"], "bound handoff identity")
        writer_refs.append(handoff["Ref"])
    g.unique_strings(writer_refs, "unique bound handoffs")
    g.require(isinstance(manifest["Files"], list), "snapshot files")
    g.require(all(isinstance(item, dict) for item in manifest["Files"]), "snapshot file objects")
    g.require([item.get("Path") for item in manifest["Files"]] == manifest["ScopePaths"], "manifest complete file set")
    files_by_path = {item["Path"]: item for item in manifest["Files"]}
    g.require(isinstance(manifest["Renames"], list), "snapshot renames")
    rename_targets, rename_origins = set(), set()
    for rename in manifest["Renames"]:
        g.exact_object(rename, "snapshot rename", ("From", "To"))
        g.require(rename["From"] in files_by_path and rename["To"] in files_by_path and rename["From"] != rename["To"], "rename scope")
        g.require(rename["To"] not in rename_targets and rename["From"] not in rename_origins, "unique rename binding")
        rename_targets.add(rename["To"])
        rename_origins.add(rename["From"])
        prior, current = files_by_path[rename["From"]], files_by_path[rename["To"]]
        g.require(prior["Baseline"] is not None and prior["Current"] is None and current["Current"] is not None, "rename byte delta")
    blob_names, total_bytes = set(), 0
    for item in manifest["Files"]:
        g.exact_object(item, "snapshot file", ("Path", "Baseline", "Current", "Index", "Untracked"))
        delivery_path(root, item["Path"])
        g.require(type(item["Untracked"]) is bool and item["Untracked"] == (item["Path"] in manifest["UntrackedDeliveryPaths"]), "untracked file marker")
        g.require(item["Baseline"] is not None or item["Current"] is not None, "nonempty byte delta")
        if item["Index"] is not None:
            g.exact_object(item["Index"], "snapshot index entry", ("Mode", "BlobId"))
            g.require(item["Index"]["Mode"] in {"100644", "100755"} and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", item["Index"]["BlobId"]), "index metadata")
        for label in ("Baseline", "Current"):
            entry = item[label]
            if entry is None:
                continue
            keys = ("Mode", "BlobId", "SHA256", "Size") if label == "Baseline" else ("Mode", "SHA256", "Size")
            g.exact_object(entry, "snapshot byte entry", keys)
            g.require(entry["Mode"] in {"100644", "100755"} and re.fullmatch(r"[0-9a-f]{64}", entry["SHA256"]), "snapshot blob metadata")
            g.require(type(entry["Size"]) is int and 0 <= entry["Size"] <= MAX_FILE_BYTES, "snapshot byte size")
            if label == "Baseline":
                g.require(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", entry["BlobId"]), "baseline object identity")
            blob_path = g.safe_path(root, root / "blobs" / entry["SHA256"])
            g.require(blob_path.is_file() and blob_path.stat().st_size <= MAX_FILE_BYTES, "bounded snapshot blob")
            if entry["SHA256"] not in blob_names:
                total_bytes += blob_path.stat().st_size
                g.require(total_bytes <= MAX_TOTAL_BYTES, "snapshot byte total")
            content = safe_content(blob_path.read_bytes())
            g.require(len(content) == entry["Size"] and sha256(content) == entry["SHA256"], "snapshot raw byte integrity")
            if label == "Baseline":
                algorithm = hashlib.sha1 if len(entry["BlobId"]) == 40 else hashlib.sha256
                g.require(algorithm(b"blob " + str(len(content)).encode("ascii") + b"\0" + content).hexdigest() == entry["BlobId"], "baseline Git blob digest")
            blob_names.add(entry["SHA256"])
    actual = {item.name for item in g.safe_path(root, root / "blobs").iterdir()}
    g.require(actual == blob_names and {item.name for item in root.iterdir()} == {"snapshot.json", "blobs"}, "no unbound snapshot content")
    if repo is not None:
        workspace = repository(repo)
        g.require(str(workspace.resolve()) == manifest["WorkspaceRoot"], "snapshot workspace binding")
        g.require(governance_inputs(workspace, manifest["RequirementId"]) == manifest["GovernanceInputs"], "current governance byte binding")
        reported_paths, bound, reported_operations = handoffs(workspace, writer_refs, manifest["RequirementId"], manifest["ContextPackVersion"], manifest["RepairLineageRef"])
        g.require(sorted(bound, key=lambda item: item["Ref"]) == manifest["WriterHandoffs"], "writer fact byte binding")
        g.require(set(manifest["ScopePaths"]) == reported_paths | rename_origins, "complete bound writer delivery")
        g.require(rename_targets == {name for name, operation in reported_operations.items() if operation in {"RENAME", "MOVE"}}, "bound writer rename operations")
        baseline = tree(workspace, manifest["BaseCommitSHA"])
        for item in manifest["Files"]:
            original = baseline.get(item["Path"])
            entry = item["Baseline"]
            if entry is None:
                g.require(original is None, "baseline absence binding")
            else:
                g.require(original is not None and original["Mode"] == entry["Mode"] and original["BlobId"] == entry["BlobId"], "baseline object binding")
                g.require(sha256(git(workspace, "cat-file", "blob", entry["BlobId"])) == entry["SHA256"], "baseline byte binding")
    else:
        workspace = Path(manifest["WorkspaceRoot"])
    if verify_worktree:
        g.require(repo is not None, "explicit worktree verification target")
        current_index = index(workspace)
        for item in manifest["Files"]:
            current_path = delivery_path(workspace, item["Path"])
            current = item["Current"]
            g.require(not current_path.exists() if current is None else current_path.is_file() and sha256(current_path.read_bytes()) == current["SHA256"], "worktree delivery bytes")
            if current is not None:
                mode = ("100755" if current_path.stat().st_mode & stat.S_IXUSR else "100644") if os.name != "nt" else current_index.get(item["Path"], {}).get("Mode", "100644")
                g.require(mode == current["Mode"], "worktree file mode")
    return {"ResultType": "CANDIDATE_SNAPSHOT_VERIFICATION", "SnapshotFormatVersion": FORMAT, "Valid": True,
            "SnapshotDigest": identity, "CandidateSnapshotRef": str(path), "RequirementId": manifest["RequirementId"],
            "ContextPackVersion": manifest["ContextPackVersion"], "RepairLineageRef": manifest["RepairLineageRef"],
            "WorkspaceRoot": manifest["WorkspaceRoot"], "ChangedPaths": manifest["ScopePaths"], "DraftOnly": True,
            "QualityPassed": False, "WorktreeVerified": verify_worktree, "ChangeAttribution": manifest["ChangeAttribution"],
            "AuthorshipVerified": False, "ExpectedFileDigestsChecked": manifest["ExpectedFileDigestsChecked"],
            "GovernanceInputsComplete": complete_governance, "GovernanceInputsChecked": repo is not None and complete_governance,
            "GovernanceInputs": manifest["GovernanceInputs"], "LocalOnly": manifest["LocalOnly"], "CannotArchiveAsFact": manifest["LocalOnly"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.error = lambda _message: parser.exit(2, "Invalid candidate snapshot arguments.\n")
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("--repo", required=True)
    capture.add_argument("--requirement-id", required=True)
    capture.add_argument("--context-version", required=True)
    capture.add_argument("--repair-lineage")
    capture.add_argument("--baseline", required=True)
    capture.add_argument("--writer-handoff", action="append", required=True)
    capture.add_argument("--path", action="append", required=True)
    capture.add_argument("--untracked", action="append", default=[])
    capture.add_argument("--output-root")
    verify = commands.add_parser("verify")
    verify.add_argument("--snapshot", required=True)
    verify.add_argument("--repo")
    verify.add_argument("--requirement-id")
    verify.add_argument("--context-version")
    verify.add_argument("--digest")
    verify.add_argument("--verify-worktree", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "capture":
            result = capture_snapshot(args.repo, requirement_id=args.requirement_id, context_version=args.context_version,
                                      repair_lineage_ref=args.repair_lineage, baseline_revision=args.baseline,
                                      writer_handoff_refs=args.writer_handoff, scope_paths=args.path,
                                      untracked_paths=args.untracked, output_root=args.output_root)
        else:
            result = verify_snapshot(args.snapshot, repo=args.repo, requirement_id=args.requirement_id, context_version=args.context_version,
                                     expected_snapshot_digest=args.digest, verify_worktree=args.verify_worktree)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (g.Invalid, OSError, UnicodeError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        print("Unable to capture or verify candidate snapshot; inspect its scoped delivery and bound writer facts.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
