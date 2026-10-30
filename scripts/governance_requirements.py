"""Versioned project tracking, template initialization and explicit developer takeover.

This extension records instructions and observations, never creates G1/G2/QA or
human acceptance facts. Legacy, unregistered requirements keep their old behavior.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

sys.dont_write_bytecode = True
import governance_tools as g
import governance_test_session as sessions

VERSION = "v1.0"
BATCH = ".governance/tracking.json"
TABLE = "docs/需求管理总表.md"
MANIFEST = "tracking.json"
HEAD = "tracking/head.json"
MAX_FILE = 2 * 1024 * 1024
ACTIONS = {"BEGIN", "ASSIGN", "EXIT", "RESUME", "DELIVERY", "CLOSE"}
META_KEYS = {"RequirementTrackingVersion", "BatchId", "RequirementId", "Title", "RegisteredAt", "BindingDigest"}
EVENT_KEYS = {"RequirementTrackingVersion", "RequirementId", "BatchId", "Sequence", "PreviousDigest", "Action", "CreatedAt", "Payload"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def path(root, name):
    from governance_package import safe_relative
    safe_relative(name)
    return g.safe_path(root, Path(root).joinpath(*name.split("/")))


def git(repo, *args, allowed=(0,)):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1", GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    result = subprocess.run(["git", "--no-pager", "-c", "core.fsmonitor=false", "-C", str(repo), *args],
                            capture_output=True, env=env, timeout=30)
    g.require(result.returncode in allowed and len(result.stdout) <= 64 * 1024 * 1024, "bounded Git tracking query")
    return result


def repository(repo):
    repo = Path(os.path.abspath(repo))
    g.safe_path(repo, repo)
    g.require(Path(git(repo, "rev-parse", "--show-toplevel").stdout.decode("utf-8").strip()).resolve() == repo.resolve(), "project root")
    return repo


def text(value, label="safe short text", limit=16384):
    g.require(isinstance(value, str) and bool(value.strip()) and len(value) <= limit and not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", value), label)
    g.sensitive(value)
    from governance_package import check_content
    check_content("tracking.txt", value.encode("utf-8"))
    # Check raw JSON-style assignments too (sensitive() serializes strings).
    g.require(not re.search(r"(?:password|api[_-]?key|access[_-]?token|authorization|cookie)\s*[\"']?\s*[:=]\s*[\"']?[^\s\"',}]{4,}|-----BEGIN .*PRIVATE KEY", value, re.I), "non-sensitive tracking text")
    return value


def decode(data):
    g.require(len(data) <= MAX_FILE, "tracking file size")
    value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=g.unique_object)
    g.require(isinstance(value, dict), "tracking object")
    g.sensitive(value)
    return value


def read(root, name):
    p = path(root, name)
    g.require(p.is_file() and p.stat().st_size <= MAX_FILE, "tracking file")
    return decode(p.read_bytes())


def write_new(p, value):
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("xb") as stream:
        stream.write(canonical(value) + b"\n")


def batch(repo):
    value = read(repo, BATCH)
    g.exact_object(value, "tracking batch", ("RequirementTrackingVersion", "BatchId", "EnabledAt", "ExcludedRequirementIds", "Remote", "WebBase"))
    g.require(value["RequirementTrackingVersion"] == VERSION and re.fullmatch(r"[0-9a-f]{32}", value["BatchId"]), "tracking batch identity")
    g.timestamp(value["EnabledAt"])
    g.unique_strings(value["ExcludedRequirementIds"], "historical exclusions")
    g.require(all(g.valid_requirement_id(x) for x in value["ExcludedRequirementIds"]), "excluded IDs")
    g.require(value["Remote"] is None or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value["Remote"]), "configured remote name")
    if value["WebBase"] is not None:
        from urllib.parse import urlsplit
        u = urlsplit(value["WebBase"])
        g.require(u.scheme == "https" and u.netloc and not (u.username or u.password or u.query or u.fragment) and value["WebBase"].rstrip("/") == value["WebBase"], "safe repository web base")
    return value


@contextmanager
def mutation(repo):
    from governance_local_tests import runtime_root
    root = runtime_root(repo)
    mutex = g.safe_path(root, root / "requirements.lock")
    mutex.parent.mkdir(parents=True, exist_ok=True)
    try:
        with mutex.open("xb") as stream:
            stream.write(b"requirement mutation\n")
    except FileExistsError:
        raise g.Invalid("Requirement operation already in progress; inspect the local lock") from None
    try:
        yield
    finally:
        mutex.unlink(missing_ok=True)


def enable(repo, remote=None, web_base=None):
    repo = repository(repo)
    from governance_project import load_lock, project_config
    g.require(load_lock(repo) is not None and project_config(repo) is not None, "adopted project")
    remotes = git(repo, "remote").stdout.decode("utf-8").splitlines()
    if remote is None and len(remotes) == 1:
        remote = remotes[0]
    g.require(remote is None or remote in remotes, "existing remote")
    with mutation(repo):
        if path(repo, BATCH).exists():
            existing = batch(repo)
            g.require(existing["Remote"] == remote and existing["WebBase"] == web_base, "batch already enabled with fixed configuration")
            return existing
        requirements = path(repo, "docs/requirements")
        excluded = sorted(p.name for p in requirements.iterdir() if g.valid_requirement_id(p.name)) if requirements.exists() else []
        value = {"RequirementTrackingVersion": VERSION, "BatchId": uuid.uuid4().hex, "EnabledAt": now(),
                 "ExcludedRequirementIds": excluded, "Remote": remote, "WebBase": web_base}
        # Validate before publishing, including URL fields.
        if web_base is not None:
            from urllib.parse import urlsplit
            u = urlsplit(web_base)
            g.require(u.scheme == "https" and u.netloc and not (u.username or u.password or u.query or u.fragment) and not web_base.endswith("/"), "repository web base")
        write_new(path(repo, BATCH), value)
        return value


def root_for(repo, ident):
    g.require(g.valid_requirement_id(ident), "RequirementId")
    return path(repo, "docs/requirements/" + ident)


def records(repo, ident, *, loader=None, event_names=None, configuration=None):
    """Validate the complete append-only extension chain, including fork detection."""
    root = root_for(repo, ident)
    def data(name):
        if loader is not None:
            return loader(name)
        p = path(root, name)
        return p.read_bytes() if p.is_file() else None
    def value(name):
        raw = data(name)
        g.require(raw is not None, "required tracking source")
        return decode(raw)
    if data(MANIFEST) is None:
        return None
    meta = value(MANIFEST)
    g.exact_object(meta, "requirement registration", META_KEYS)
    g.require(meta["RequirementTrackingVersion"] == VERSION and meta["RequirementId"] == ident, "registration identity")
    config = configuration or batch(repo)
    g.require(meta["BatchId"] == config["BatchId"] and ident not in config["ExcludedRequirementIds"], "current tracking batch")
    text(meta["Title"], limit=200)
    g.timestamp(meta["RegisteredAt"])
    binding = data("plugin-binding.json")
    g.require(binding is not None and sha(binding) == meta["BindingDigest"], "registration plugin pin")
    head = value(HEAD)
    g.exact_object(head, "tracking cursor", ("EventRef", "EventDigest"))
    directory = path(root, "tracking/events")
    names = set(event_names) if event_names is not None else ({p.relative_to(root).as_posix() for p in directory.glob("*.json")} if directory.exists() else set())
    g.require(len(names) <= 1024, "tracking event count")
    reverse, seen, ref, pin = [], set(), head["EventRef"], head["EventDigest"]
    while ref is not None:
        g.require(isinstance(ref, str) and re.fullmatch(r"tracking/events/[0-9]{6}-[0-9a-f]{64}\.json", ref) and ref not in seen, "tracking chain reference")
        event = value(ref)
        g.exact_object(event, "tracking event", EVENT_KEYS)
        g.require(sha(canonical(event)) == pin and ref.endswith(pin + ".json"), "tracking event digest")
        g.require(event["RequirementTrackingVersion"] == VERSION and event["RequirementId"] == ident and event["BatchId"] == meta["BatchId"] and event["Action"] in ACTIONS, "tracking event identity")
        g.require(type(event["Sequence"]) is int and event["Sequence"] >= 1 and isinstance(event["Payload"], dict), "tracking sequence/payload")
        g.timestamp(event["CreatedAt"])
        reverse.append(event)
        seen.add(ref)
        pin = event["PreviousDigest"]
        ref = None if pin is None else next((n for n in names if n.endswith(pin + ".json")), "MISSING")
    g.require(seen == names and bool(reverse), "complete tracking chain; no orphan or fork")
    events = list(reversed(reverse))
    g.require([e["Sequence"] for e in events] == list(range(1, len(events)+1)), "tracking event order")
    cycles, current, mode, resumed = [], None, "GOVERNED", None
    for event in events:
        payload, action = event["Payload"], event["Action"]
        g.require(g.timestamp(event["CreatedAt"]) >= g.timestamp(meta["RegisteredAt"]), "event follows registration")
        if action == "BEGIN":
            g.exact_object(payload, "begin", ("Instruction", "Title", "BaselineRef", "PreviousIterationCommit", "StartFacts"))
            g.require(current is None or current["Closed"], "serial requirement iterations")
            text(payload["Instruction"]); text(payload["Title"], limit=200)
            path(root, payload["BaselineRef"])
            g.require(isinstance(payload["StartFacts"], dict), "initial fact pins")
            current = {"Ordinal": len(cycles)+1, "Iteration": None, "Closed": False, "Outcome": None, "Mode": mode,
                       "Title": payload["Title"], "BaselineRef": payload["BaselineRef"], "Begin": event, "Close": None}
            cycles.append(current)
            resumed = None
        else:
            g.require(current is not None and not current["Closed"], "active requirement iteration")
            if action == "ASSIGN":
                g.exact_object(payload, "iteration assignment", ("Iteration", "G2Ref", "G2Digest", "ContextDigest"))
                g.require(current["Iteration"] is None and re.fullmatch(r"V[0-9]{3,}", payload["Iteration"]), "unique G2 iteration assignment")
                g.require(payload["Iteration"] == f"V{sum(c['Iteration'] is not None for c in cycles)+1:03d}", "monotonic iteration")
                current["Iteration"] = payload["Iteration"]
            elif action == "EXIT":
                g.exact_object(payload, "exit instruction", ("Instruction", "Reason", "LastStage"))
                text(payload["Instruction"])
                g.require(isinstance(payload["Reason"], str) and isinstance(payload["LastStage"], str), "exit description")
                mode = "MANUAL"
            elif action == "RESUME":
                g.exact_object(payload, "resume instruction", ("Instruction", "Review", "BeforeCandidates", "PlanRecheck"))
                g.require(mode == "MANUAL" and isinstance(payload["BeforeCandidates"], list), "explicit resume")
                text(payload["Instruction"]); text(payload["Review"])
                g.require(payload["PlanRecheck"] in ("VALID", "INCOMPLETE_OR_INVALID", "NO_PLAN_YET"), "resumption plan observation")
                mode, resumed = "GOVERNED", event
            elif action == "DELIVERY":
                g.exact_object(payload, "delivery observation", ("Instruction", "Summary", "Commit", "TargetRef"))
                text(payload["Instruction"]); text(payload["Summary"])
            elif action == "CLOSE":
                g.exact_object(payload, "iteration closure", ("Instruction", "Outcome", "Summary", "SourceCommit", "Sources", "ArchiveChecked"))
                text(payload["Instruction"]); text(payload["Summary"])
                g.require(payload["Outcome"] in ("DELIVERY_RECORDED", "TERMINATED") and isinstance(payload["Sources"], dict) and type(payload["ArchiveChecked"]) is bool, "explicit closure outcome")
                g.require(isinstance(payload["SourceCommit"], str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", payload["SourceCommit"]), "closed iteration commit")
                for name, pin in payload["Sources"].items():
                    path(repo, name)
                    g.require(name.startswith("docs/requirements/" + ident + "/") and isinstance(pin, str) and re.fullmatch(r"[0-9a-f]{64}", pin), "closed source pin")
                current.update(Closed=True, Outcome=payload["Outcome"], Close=event)
            current["Mode"] = mode
    return {"Metadata": meta, "Events": events, "Cycles": cycles, "Current": current, "Mode": mode,
            "LastResume": resumed, "HeadDigest": head["EventDigest"]}


def append(repo, ident, action, payload):
    root = root_for(repo, ident)
    cursor = read(root, HEAD)
    state = records(repo, ident) if cursor["EventRef"] is not None else None
    meta = read(root, MANIFEST)
    event = {"RequirementTrackingVersion": VERSION, "RequirementId": ident, "BatchId": meta["BatchId"],
             "Sequence": len(state["Events"])+1 if state else 1, "PreviousDigest": cursor["EventDigest"],
             "Action": action, "CreatedAt": now(), "Payload": payload}
    g.sensitive(event)
    pin = sha(canonical(event))
    ref = f"tracking/events/{event['Sequence']:06d}-{pin}.json"
    target = path(root, ref)
    from governance_project import write_atomic
    write_new(target, event)
    try:
        write_atomic(path(root, HEAD), canonical({"EventRef": ref, "EventDigest": pin}) + b"\n")
    except BaseException:
        target.unlink()
        raise
    return event


def fact_pins(repo, ident):
    root = root_for(repo, ident)
    result = {}
    for p in (root / "facts").rglob("*.json"):
        g.safe_path(repo, p)
        g.require(p.stat().st_size <= MAX_FILE, "fact size")
        result[p.relative_to(root).as_posix()] = sha(p.read_bytes())
    g.require(len(result) <= 1024, "fact count")
    return result


def render_baseline(repo, ident, title, instruction):
    source = g.resource_path(repo, "docs/Agent治理/需求基线模板.md").read_text(encoding="utf-8-sig")
    source = source.replace("> [逐字保留原始需求及必要上下文]", "\n".join("> " + line for line in instruction.splitlines()))
    source = source.replace("- **产物版本：**\n", "- **产物版本：** v1.0\n", 1)
    source = source.replace("- **`RequirementId`：**\n", "- **`RequirementId`：** " + ident + "\n", 1)
    source = source.replace("- **需求目录：**\n", "- **需求目录：** docs/requirements/" + ident + "/\n", 1)
    source = source.replace("- **产物当前状态：**", "- **产物当前状态：** DRAFT", 1)
    return ("<!-- requirement-tracking:v1.0; generated draft; no approval -->\n\n# " + title + "\n\n" + source).encode("utf-8")


def reserve_tracking_id(repo):
    """Original ID spelling, workspace-local reservation, explicit global limits."""
    from governance_local_tests import runtime_root
    developer = g.developer_id(git(repo, "config", "--local", "--get", "governance.developerId").stdout.decode("utf-8").strip())
    from datetime import timedelta
    day = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    prefix = f"REQ-{developer}-{day}-"
    root = runtime_root(repo)
    reservations = g.safe_path(root, root / "id-reservations")
    reservations.mkdir(parents=True, exist_ok=True)
    used = {p.name.upper() for p in reservations.iterdir()}
    for item in git(repo, "worktree", "list", "--porcelain", "-z").stdout.decode("utf-8").split("\0"):
        if item.startswith("worktree "):
            tree = Path(item[9:])
            directory = g.safe_path(tree, tree / "docs/requirements")
            if directory.is_dir():
                used.update(p.name.upper() for p in directory.iterdir())
    revisions = set(git(repo, "for-each-ref", "--format=%(objectname)", "refs/heads", "refs/remotes", "refs/tags").stdout.decode("ascii").splitlines())
    g.require(len(revisions) <= 512, "ID source ref count")
    for revision in revisions:
        listed = git(repo, "ls-tree", "-d", "--name-only", revision + ":docs/requirements", allowed=(0, 128))
        if listed.returncode == 0:
            used.update(name.upper() for name in listed.stdout.decode("utf-8").splitlines())
    for number in range(1, 1000000):
        ident = prefix + f"{number:02d}"
        if ident in used:
            continue
        try:
            g.safe_path(root, reservations / ident).mkdir()
            return ident
        except FileExistsError:
            continue
    raise g.Invalid("Local requirement number sequence exhausted")


def register(repo, title, instruction, requirement_id=None):
    repo = repository(repo)
    text(title, limit=200); text(instruction)
    g.require("\n" not in title and "\r" not in title, "single-line requirement title")
    config = batch(repo)
    ident = requirement_id or reserve_tracking_id(repo)
    g.require(g.valid_requirement_id(ident) and ident not in config["ExcludedRequirementIds"], "new requirement in current batch")
    from governance_project import binding, load_lock
    root = root_for(repo, ident)
    with mutation(repo):
        if path(root, MANIFEST).exists():
            state = records(repo, ident)
            g.require(state["Metadata"]["Title"] == title and state["Events"][0]["Payload"]["Instruction"] == instruction, "idempotent registration input")
            return {"RequirementId": ident, "Created": False, "Stage": "WAITING_FOR_G1"}
        prebound = root.exists() and {p.name for p in root.iterdir()} == {"plugin-binding.json"}
        g.require(not root.exists() or not any(root.iterdir()) or prebound, "existing requirement cannot be silently adopted")
        from governance_project import write_atomic, verify_byte_attributes
        attributes = path(repo, ".gitattributes")
        attributes_before = attributes.read_bytes()
        marker = b"# requirement-tracking:begin"
        end_marker = b"# requirement-tracking:end"
        raw = attributes_before
        g.require(raw.count(marker) == raw.count(end_marker) and raw.count(marker) <= 1, "unique tracking byte-policy block")
        line = ('"/docs/requirements/' + ident + '/**" -text').encode("utf-8")
        if marker in raw:
            first, last = raw.index(marker), raw.index(end_marker)
            g.require(first < last, "tracking byte-policy block order")
            block = raw[first:last].replace(b"\r\n", b"\n")
            if line not in block.splitlines():
                raw = raw[:last] + line + b"\n" + raw[last:]
        else:
            raw += (b"" if raw.endswith(b"\n") else b"\n") + marker + b"\n" + line + b"\n" + end_marker + b"\n"
        write_atomic(attributes, raw)
        try:
            verify_byte_attributes(repo, {"docs/requirements/" + ident + "/" + n for n in
                                         ("plugin-binding.json", MANIFEST, HEAD, "需求基线.md", "facts/gates/GATE-001.json")})
            binding(repo, load_lock(repo), ident, create=True)
        except BaseException:
            if attributes.read_bytes() == raw:
                write_atomic(attributes, attributes_before)
            raise
        created = [] if prebound else [path(root, "plugin-binding.json")]
        try:
            meta = {"RequirementTrackingVersion": VERSION, "BatchId": config["BatchId"], "RequirementId": ident,
                    "Title": title, "RegisteredAt": now(), "BindingDigest": sha(path(root, "plugin-binding.json").read_bytes())}
            for name, value in ((MANIFEST, meta), (HEAD, {"EventRef": None, "EventDigest": None})):
                write_new(path(root, name), value); created.append(path(root, name))
            baseline = path(root, "需求基线.md")
            with baseline.open("xb") as stream:
                stream.write(render_baseline(repo, ident, title, instruction))
            created.append(baseline)
            append(repo, ident, "BEGIN", {"Instruction": instruction, "Title": title, "BaselineRef": "需求基线.md", "PreviousIterationCommit": None, "StartFacts": {}})
        except BaseException:
            for p in reversed(created):
                p.unlink(missing_ok=True)
            for folder in (root / "tracking/events", root / "tracking", root):
                if folder.is_dir() and not any(folder.iterdir()):
                    folder.rmdir()
            if attributes.read_bytes() == raw:
                write_atomic(attributes, attributes_before)
            raise
    return {"RequirementId": ident, "Created": True, "Stage": "WAITING_FOR_G1", "Iteration": None, "GatesPassed": False}


def verified_g2(repo, ident):
    from governance_local_tests import metadata
    root = root_for(repo, ident)
    checked = g.validate_g2(root / "control-profile.json", root / "acceptance-plan.json")
    context = path(root, "开发上下文包.md")
    body = context.read_text(encoding="utf-8-sig")
    g.require(metadata(body, "产物当前状态") == "FROZEN", "frozen context")
    version = g.artifact_version(body)
    events = g.load_events(root)
    gates = [e for e in events.values() if e["EventType"] == "GATE_DECISION" and e["Payload"].get("Gate") == "G2" and e["ContextPackVersion"] == version]
    g.require(len(gates) == 1, "unique current G2")
    event = gates[0]
    g.require(event["Payload"].get("Decision") == "FROZEN" and event["Payload"].get("ArtifactVersion") == version and event["Payload"].get("ArtifactRef") == context.relative_to(repo).as_posix(), "G2 context binding")
    ref = event["_path"]
    state = records(repo, ident)
    g.require(g.timestamp(event["CreatedAt"]) >= g.timestamp(state["Current"]["Begin"]["CreatedAt"]), "G2 follows iteration registration")
    mode = event["Payload"].get("DecisionMode")
    g.require(bool(event["Payload"].get("BasisRefs")) and
              ((mode == "HUMAN" and bool(event["Payload"].get("ApprovalRef"))) or
               (mode == "AUTOMATIC" and checked["RequiredG2DecisionMode"] == "AUTOMATIC_ALLOWED" and event["Payload"].get("ApprovalRef") is None)), "G2 approval mode")
    previous = state["Current"]["Begin"]["Payload"]["StartFacts"]
    g.require(all(path(root, n).is_file() and sha(path(root, n).read_bytes()) == old for n, old in previous.items()), "prior iteration facts unchanged")
    pin = sha(path(root, ref).read_bytes())
    g.require(state["Current"]["Begin"]["Payload"]["StartFacts"].get(ref) != pin, "G2 belongs to active iteration")
    return ref, pin, sha(context.read_bytes()), body


def initialize_record(repo, ident):
    repo = repository(repo)
    with mutation(repo):
        state = records(repo, ident)
        g.require(state is not None and not state["Current"]["Closed"] and state["Mode"] == "GOVERNED", "active governed requirement")
        ref, pin, ctx, body = verified_g2(repo, ident)
        iteration = state["Current"]["Iteration"] or f"V{sum(c['Iteration'] is not None for c in state['Cycles'])+1:03d}"
        root = root_for(repo, ident)
        record = path(root, "开发记录.md")
        if state["Current"]["Iteration"] is None:
            if record.exists() and state["Current"]["Ordinal"] == 1:
                # Existing runtime records may be adopted only with exact identity/version.
                existing = record.read_text(encoding="utf-8-sig")
                g.require(re.search(r"\*\*`RequirementId`[：:]\*\*\s*" + re.escape(ident), existing) and
                          re.search(r"\*\*需求迭代版本[：:]\*\*\s*" + iteration, existing), "existing development record belongs to this iteration")
            else:
                if record.exists():
                    previous = state["Cycles"][-2]["Close"]["Payload"]
                    reference = record.relative_to(repo).as_posix()
                    g.require(previous["Sources"].get(reference) == sha(record.read_bytes()), "previous record retained by immutable history")
                template = g.resource_path(repo, "docs/Agent治理/开发记录模板.md").read_text(encoding="utf-8-sig")
                for field, value in (("产物版本", "v1.0"), ("`RequirementId`", ident), ("需求迭代版本", iteration),
                                     ("需求名称", state["Current"]["Title"]), ("记录生命周期状态", "IN_PROGRESS")):
                    template = re.sub(r"(?m)^- \*\*" + re.escape(field) + r"[：:]\*\*[^\n]*$", lambda _m: "- **" + field + "：** " + value, template, count=1)
                from governance_status import planned_workpackages
                workpackages = sorted(planned_workpackages(body))
                rows = "\n".join(f"| {wp} | 见冻结上下文包 | `NOT_STARTED` | 尚无交接事实 | N/A | [开发上下文包](开发上下文包.md) |" for wp in workpackages)
                template = re.sub(r"(?m)^\| WP-01 \|.*$", lambda _m: rows, template, count=1)
                from governance_project import write_atomic
                write_atomic(record, template.encode("utf-8"))
            append(repo, ident, "ASSIGN", {"Iteration": iteration, "G2Ref": ref, "G2Digest": pin, "ContextDigest": ctx})
        return {"RequirementId": ident, "Iteration": iteration, "RecordRef": record.relative_to(repo).as_posix(), "GatesCreated": False}


def early_stage(repo, ident):
    root = root_for(repo, ident)
    gates = []
    for p in (root / "facts/gates").glob("*.json"):
        event = read(root, p.relative_to(root).as_posix())
        g.validate_event_structure(event, ident, p)
        if event["Payload"].get("Gate") == "G1":
            gates.append(event)
    state = records(repo, ident)
    start = state["Current"]["Begin"]["Payload"]["StartFacts"] if state else {}
    gates = [event for event in gates if start.get("facts/gates/" + event["EventId"] + ".json") != sha(path(root, "facts/gates/" + event["EventId"] + ".json").read_bytes())]
    if not gates:
        return "WAITING_FOR_G1"
    g.require(len(gates) == 1, "early G1 requires full plan validation")
    e = gates[0]
    g.require(e["Payload"].get("DecisionMode") == "HUMAN" and e["Payload"].get("ApprovalRef"), "human G1 evidence")
    baseline = path(root, "需求基线.md")
    g.require(e["Payload"].get("ArtifactRef") == baseline.relative_to(repo).as_posix() and
              g.artifact_version(baseline.read_text(encoding="utf-8-sig")) == e["Payload"].get("ArtifactVersion"), "G1 baseline binding")
    return "PLANNING" if e["Payload"].get("Decision") == "CONFIRMED" else "G1_REJECTED"


def prepare_planning(repo, ident):
    """Create deliberately incomplete drafts; the agent fills them from real evidence."""
    repo = repository(repo)
    with mutation(repo):
        require_governed(repo, ident)
        g.require(early_stage(repo, ident) == "PLANNING", "current recorded human G1")
        root = root_for(repo, ident)
        state = records(repo, ident)
        prefix = "" if state["Current"]["Ordinal"] == 1 else f"tracking/requests/{state['Current']['Ordinal']:06d}/"
        created = []
        context = path(root, prefix + "开发上下文包.md")
        if not context.exists():
            template = g.resource_path(repo, "docs/Agent治理/开发上下文包模板.md").read_text(encoding="utf-8-sig")
            template = template.replace("- **产物当前状态：**", "- **产物当前状态：** DRAFT", 1)
            template = template.replace("- **`RequirementId`：**\n", "- **`RequirementId`：** " + ident + "\n", 1)
            context.parent.mkdir(parents=True, exist_ok=True)
            with context.open("xb") as stream:
                stream.write(template.encode("utf-8"))
            created.append(context.relative_to(repo).as_posix())
        for name in ("control-profile", "acceptance-plan"):
            target = path(root, prefix + name + ".json")
            if target.exists():
                continue
            schema = g.load_contract(g.resource_path(repo, "docs/Agent治理/contracts/" + name + ".schema.json"), "planning schema")
            value = {field: None for field in schema.get("required", [])}
            value["RequirementId"] = ident
            write_new(target, value)
            created.append(target.relative_to(repo).as_posix())
        return {"CreatedDrafts": created, "PlanValidated": False, "GatesPassed": False, "NextAction": "FILL_DRAFT_FROM_APPROVED_BASELINE_AND_PROJECT_EVIDENCE"}


def execution_state(repo, ident):
    state = records(repo, ident)
    if state is None:
        return {"Mode": "LEGACY", "Closed": False, "RequiresFreshCandidate": False}
    return {"Mode": state["Mode"], "Closed": state["Current"]["Closed"], "RequiresFreshCandidate": state["LastResume"] is not None,
            "ResumeEvent": state["LastResume"], "Iteration": state["Current"]["Iteration"]}


def require_governed(repo, ident, *, execution=False):
    state = execution_state(repo, ident)
    g.require(state["Mode"] != "MANUAL" and not state["Closed"], "developer takeover or closed iteration; automatic governance stopped")
    if execution and state["Mode"] != "LEGACY":
        verified_g2(repo, ident)


def takeover(repo, ident, instruction, reason=""):
    repo = repository(repo)
    text(instruction)
    if reason:
        text(reason)
    with mutation(repo):
        state = records(repo, ident)
        g.require(state is not None and not state["Current"]["Closed"], "registered active requirement")
        if state["Mode"] != "MANUAL":
            from governance_status import query_status
            previous = query_status(repo, ident)["CurrentStage"]
            append(repo, ident, "EXIT", {"Instruction": instruction, "Reason": reason, "LastStage": previous})
    # Persist the instruction first: a cleanup problem must not silently resume governance.
    stopped = []
    from governance_local_tests import runtime_root
    directory = runtime_root(repo) / "local-tests/sessions"
    for folder in directory.glob("*"):
        g.safe_path(repo if folder.is_relative_to(repo) else directory, folder)
        if re.fullmatch(r"[0-9a-f]{32}", folder.name) and (folder / "ready.json").is_file() and not any((folder / n).exists() for n in ("finished.json", "error.json")):
            ready = sessions.read(folder / "ready.json")
            if ready.get("Observations", {}).get("RequirementId") == ident:
                try:
                    result = sessions.stop(repo, folder.name)
                    stopped.append({"SessionId": folder.name, "Cleanup": result.get("Cleanup", "UNCONFIRMED")})
                except (ValueError, OSError, TimeoutError):
                    stopped.append({"SessionId": folder.name, "Cleanup": "UNCONFIRMED"})
    return {"RequirementId": ident, "Mode": "MANUAL", "StoppedSessions": stopped, "HumanAccepted": False}


def resume(repo, ident, instruction, review):
    repo = repository(repo)
    text(instruction); text(review)
    with mutation(repo):
        state = records(repo, ident)
        g.require(state is not None and state["Mode"] == "MANUAL" and not state["Current"]["Closed"], "explicit current-requirement resumption")
        root = root_for(repo, ident)
        plan_check = "NO_PLAN_YET"
        if (root / "control-profile.json").exists():
            try:
                g.validate_plan(root / "control-profile.json", root / "acceptance-plan.json")
                plan_check = "VALID"
            except (ValueError, OSError, KeyError, TypeError):
                plan_check = "INCOMPLETE_OR_INVALID"
        pins = fact_pins(repo, ident)
        candidates = sorted(value for name, value in pins.items() if name.startswith("facts/candidates/"))
        append(repo, ident, "RESUME", {"Instruction": instruction, "Review": review, "BeforeCandidates": candidates, "PlanRecheck": plan_check})
    return {"RequirementId": ident, "Mode": "GOVERNED", "PlanRecheck": plan_check, "ReviewRecorded": True, "ReviewSemanticsVerified": False, "FreshCandidateRequired": bool(candidates)}


def delivery(repo, ident, instruction, summary, commit=None, target_ref=None):
    repo = repository(repo)
    text(instruction); text(summary)
    if commit is not None:
        g.require(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit), "delivery commit identity")
        git(repo, "cat-file", "-e", commit + "^{commit}")
    if target_ref is not None:
        g.require(target_ref.startswith(("refs/heads/", "refs/remotes/")), "target delivery ref")
        git(repo, "check-ref-format", target_ref)
        g.require(commit is not None, "delivery commit")
        git(repo, "merge-base", "--is-ancestor", commit, target_ref)
    with mutation(repo):
        state = records(repo, ident)
        g.require(state is not None and not state["Current"]["Closed"], "active delivery observation")
        append(repo, ident, "DELIVERY", {"Instruction": instruction, "Summary": summary, "Commit": commit, "TargetRef": target_ref})
    return {"DeliveryRecorded": True, "CommitVerified": commit is not None, "TargetAncestryVerified": target_ref is not None, "HumanAccepted": False}


def close_iteration(repo, ident, instruction, outcome, summary, source_commit):
    repo = repository(repo)
    text(instruction); text(summary)
    g.require(outcome in ("DELIVERY_RECORDED", "TERMINATED"), "closure outcome")
    g.require(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source_commit), "immutable history commit")
    git(repo, "cat-file", "-e", source_commit + "^{commit}")
    with mutation(repo):
        state = records(repo, ident)
        g.require(state is not None and not state["Current"]["Closed"], "serial active iteration")
        root = root_for(repo, ident)
        pins = {}
        for p in root.rglob("*"):
            g.safe_path(repo, p)
            if not p.is_file() or p.relative_to(root).parts[0] == "tracking":
                continue
            path(root, p.relative_to(root).as_posix())
            g.require(p.stat().st_size <= 32 * 1024 * 1024, "bounded iteration history file")
            name = p.relative_to(repo).as_posix()
            data = p.read_bytes()
            old = git(repo, "show", source_commit + ":" + name).stdout
            g.require(old == data, "iteration inputs must already be committed unchanged")
            pins[name] = sha(data)
        archive_checked = False
        if outcome == "DELIVERY_RECORDED" and state["Mode"] == "GOVERNED":
            record = path(root, "开发记录.md").read_text(encoding="utf-8-sig")
            g.require(re.search(r"\*\*记录生命周期状态[：:]\*\*\s*`?FROZEN", record), "existing final archive record")
            ledger = path(repo, "docs/需求版本管理.md").read_text(encoding="utf-8-sig")
            g.require(state["Current"]["Iteration"] is not None and any(ident in line and state["Current"]["Iteration"] in line for line in ledger.splitlines() if line.startswith("|")), "existing final archive registration")
            archive_checked = True
        append(repo, ident, "CLOSE", {"Instruction": instruction, "Outcome": outcome, "Summary": summary,
               "SourceCommit": source_commit, "Sources": pins, "ArchiveChecked": archive_checked})
    return {"Closed": True, "Outcome": outcome, "ArchiveConsistencyChecked": archive_checked, "HumanAccepted": False}


def begin_iteration(repo, ident, title, instruction):
    repo = repository(repo)
    text(title, limit=200); text(instruction)
    with mutation(repo):
        state = records(repo, ident)
        g.require(state is not None and state["Current"]["Closed"], "previous iteration explicitly closed")
        ordinal = len(state["Cycles"])+1
        ref = f"tracking/requests/{ordinal:06d}.md"
        target = path(root_for(repo, ident), ref)
        with _new_stream(target) as stream:
            stream.write(render_baseline(repo, ident, title, instruction))
        try:
            append(repo, ident, "BEGIN", {"Instruction": instruction, "Title": title, "BaselineRef": ref,
                   "PreviousIterationCommit": state["Current"]["Close"]["Payload"]["SourceCommit"], "StartFacts": fact_pins(repo, ident)})
        except BaseException:
            target.unlink()
            raise
    return {"Iteration": None, "DraftRef": target.relative_to(repo).as_posix(), "NextAction": "REVISE_BASELINE_AND_PLAN_USING_EXISTING_VERSION_CHAIN", "OriginalArtifactsPreserved": True}


def _new_stream(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    return p.open("xb")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.error = lambda _m: parser.exit(2, "Invalid requirement-management arguments.\n")
    parser.add_argument("action", choices=("enable", "init", "prepare-planning", "initialize-record", "exit", "resume", "delivery", "close", "next", "inspect"))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--requirement-id")
    parser.add_argument("--title")
    parser.add_argument("--instruction")
    parser.add_argument("--reason", default="")
    parser.add_argument("--review")
    parser.add_argument("--summary")
    parser.add_argument("--commit")
    parser.add_argument("--target-ref")
    parser.add_argument("--outcome", choices=("DELIVERY_RECORDED", "TERMINATED"))
    parser.add_argument("--remote")
    parser.add_argument("--web-base")
    a = parser.parse_args(argv)
    try:
        repo = repository(a.repo)
        if a.action == "enable": result = enable(repo, a.remote, a.web_base)
        elif a.action == "init": result = register(repo, a.title, a.instruction, a.requirement_id)
        elif a.action == "initialize-record": result = initialize_record(repo, a.requirement_id)
        elif a.action == "prepare-planning": result = prepare_planning(repo, a.requirement_id)
        elif a.action == "exit": result = takeover(repo, a.requirement_id, a.instruction, a.reason)
        elif a.action == "resume": result = resume(repo, a.requirement_id, a.instruction, a.review)
        elif a.action == "delivery": result = delivery(repo, a.requirement_id, a.instruction, a.summary, a.commit, a.target_ref)
        elif a.action == "close": result = close_iteration(repo, a.requirement_id, a.instruction, a.outcome, a.summary, a.commit)
        elif a.action == "next": result = begin_iteration(repo, a.requirement_id, a.title, a.instruction)
        else: result = records(repo, a.requirement_id)
        if a.action != "inspect":
            try:
                from governance_overview import refresh
                view = refresh(repo, fetch=False)
                result["OverviewRef"] = view["TableRef"]
            except (ValueError, OSError, TypeError, KeyError):
                result["OverviewRefresh"] = "PENDING"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, TypeError, KeyError, subprocess.TimeoutExpired):
        print("Requirement operation rejected; check the batch, explicit instruction, history and bound inputs.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
