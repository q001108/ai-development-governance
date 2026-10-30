"""Requirement navigation from local observations and uploaded Git snapshots.

No branch checkout, remote code execution, commits or pushes. Remote projections
remain explicitly reported observations, never gate approvals.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote, urlsplit

sys.dont_write_bytecode = True
import governance_requirements as r
import governance_tools as g

PROJECTION = "tracking/view.json"
MARKER = "<!-- requirement-overview:v1.0; generated -->"


def local_observation(repo, ident):
    state = r.records(repo, ident)
    root = r.root_for(repo, ident)
    result = {"RequirementId": ident, "TrackingHead": state["HeadDigest"], "ObservedAt": r.now(), "Stage": "UNDETERMINED",
              "NextActions": [], "Problems": [], "Sources": {}, "QualityPassed": False, "HumanAccepted": False}
    try:
        g.require(all(n.stat().st_size <= r.MAX_FILE for n in root.glob("*") if n.is_file() and n.suffix in (".md", ".json")), "bounded local observation source")
        result["Sources"] = {n.relative_to(root).as_posix(): r.sha(n.read_bytes()) for n in root.glob("*")
                             if n.is_file() and n.name not in (r.MANIFEST,) and n.suffix in (".md", ".json")}
        result["Sources"].update(r.fact_pins(repo, ident))
        g.require(len(result["Sources"]) <= 1024, "bounded observation")
        if state["Current"]["Closed"]:
            result["Stage"] = state["Current"]["Outcome"]
        elif state["Mode"] == "MANUAL":
            result["Stage"] = "MANUAL_TRACKING"
            if any(e["Action"] == "DELIVERY" for e in state["Events"][state["Current"]["Begin"]["Sequence"]:]):
                result["Stage"] = "MANUAL_DELIVERY_RECORDED"
        elif state["Current"]["Ordinal"] > 1:
            try:
                r.verified_g2(repo, ident)
            except (ValueError, OSError):
                result["Stage"] = "NEXT_ITERATION_PLANNING"
                return result
        if result["Stage"] == "UNDETERMINED":
            if not (root / "control-profile.json").exists():
                result["Stage"] = r.early_stage(repo, ident)
            else:
                from governance_status import query_status
                observed = query_status(repo, ident)
                result.update(Stage=observed["CurrentStage"], NextActions=observed.get("NextActions", []), Problems=observed.get("Problems", []))
                if result["Stage"] == "UNDETERMINED" and r.read(root, "control-profile.json").get("SchemaVersion") is None:
                    result["Stage"] = "PLANNING_DRAFT"
        if state["LastResume"] is not None and result["Stage"] not in ("DELIVERY_RECORDED", "TERMINATED"):
            candidates = {v for n, v in r.fact_pins(repo, ident).items() if n.startswith("facts/candidates/")}
            if candidates and candidates <= set(state["LastResume"]["Payload"]["BeforeCandidates"]):
                result["Stage"] = "FRESH_CANDIDATE_REQUIRED_AFTER_RESUME"
    except (ValueError, OSError, KeyError, TypeError):
        result.update(Stage="UNDETERMINED", Problems=["TRACKING_SOURCE_REQUIRES_REVIEW"])
    return result


def remote_cache(repo, remote, *, create=False):
    """Keep fetch writes outside the sandbox-protected business .git directory."""
    url = r.git(repo, "remote", "get-url", remote).stdout.decode("utf-8").strip()
    parsed = urlsplit(url)
    g.require(not parsed.password and not parsed.query and not parsed.fragment and
              (parsed.scheme in ("ssh", "file") or parsed.username is None), "credential-free configured fetch address")
    from governance_project import project_config
    config = project_config(repo)
    g.require(config is not None and config.get("LocalRuntimeDirectory") == ".governance/local", "project-local overview cache adaptation")
    root = r.path(repo, ".governance/local")
    if create:
        from governance_local_tests import runtime_root
        root = runtime_root(repo)
    cache = g.safe_path(root, root / "overview-remotes" / r.sha(url.encode("utf-8")))
    return cache, url


def fetch_uploaded(repo, remote):
    if remote is None:
        return "LOCAL_ONLY"
    try:
        cache, url = remote_cache(repo, remote, create=True)
        g.require(not url.startswith("-") and "\n" not in url and "\r" not in url, "configured fetch address")
        if not cache.exists():
            cache.mkdir(parents=True)
            r.git(cache, "init", "--bare", "-q")
        g.require(r.git(cache, "rev-parse", "--is-bare-repository").stdout.strip() == b"true", "owned bare observation cache")
        r.git(cache, "-c", "core.hooksPath=" + str(cache / "disabled-hooks"), "fetch", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules", url,
              f"+refs/heads/*:refs/remotes/{remote}/*")
        return "FETCHED"
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return "FETCH_FAILED_USING_CACHED_REFS"


class Snapshot:
    def __init__(self, repo, commit):
        self.repo, self.commit, self.cache = repo, commit, {}
        g.require(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit), "snapshot commit")
        raw = r.git(repo, "ls-tree", "-rz", "--full-tree", commit, "--", "docs/requirements", r.BATCH).stdout
        self.paths = {}
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            header, name = entry.split(b"\t", 1)
            mode, kind, blob = header.decode("ascii").split()
            source = name.decode("utf-8")
            g.require(len(self.paths) < 50000, "remote tree limit")
            self.paths[source] = (mode, kind, blob)
        self.bytes_read = 0

    def load(self, name):
        r.path(self.repo, name)
        if name not in self.paths:
            return None
        if name not in self.cache:
            mode, kind, blob = self.paths[name]
            g.require(mode in ("100644", "100755") and kind == "blob", "regular uploaded tracking source")
            size = int(r.git(self.repo, "cat-file", "-s", blob).stdout)
            g.require(size <= r.MAX_FILE and self.bytes_read + size <= 32 * 1024 * 1024, "remote observation size")
            data = r.git(self.repo, "cat-file", "blob", blob).stdout
            self.bytes_read += len(data)
            self.cache[name] = data
        return self.cache[name]


def uploaded_observation(snapshot, prefix, state):
    raw = snapshot.load(prefix + PROJECTION)
    if raw is None:
        return {"Stage": "NO_UPLOADED_OBSERVATION", "EvidenceStatus": "MISSING", "ObservedAt": None, "NextActions": []}
    try:
        value = r.decode(raw)
        g.require(value["RequirementId"] == state["Metadata"]["RequirementId"] and value["TrackingHead"] == state["HeadDigest"], "uploaded observation identity")
        g.require(isinstance(value["Sources"], dict) and len(value["Sources"]) <= 1024, "uploaded source pins")
        expected = {name[len(prefix):] for name in snapshot.paths if name.startswith(prefix) and
                    (name[len(prefix):].startswith("facts/") and name.endswith(".json") or
                     "/" not in name[len(prefix):] and name.endswith((".json", ".md")) and name[len(prefix):] != r.MANIFEST)}
        g.require(set(value["Sources"]) == expected, "complete uploaded observation source inventory")
        g.require(all((data := snapshot.load(prefix + name)) is not None and r.sha(data) == pin for name, pin in value["Sources"].items()), "uploaded observation source pins")
        text_stage = r.text(value["Stage"], limit=128)
        return {"Stage": text_stage, "EvidenceStatus": "UPLOADED_OBSERVATION_SOURCE_PINS_MATCH", "ObservedAt": value["ObservedAt"], "NextActions": value.get("NextActions", []),
                "SourcePinsDigest": r.sha(r.canonical(value["Sources"]))}
    except (ValueError, OSError, KeyError, TypeError):
        return {"Stage": "STALE_OR_INVALID_UPLOADED_OBSERVATION", "EvidenceStatus": "UNDETERMINED", "ObservedAt": None, "NextActions": []}


def entries(repo, state, observation, *, ref, commit, web_base, paths):
    ident = state["Metadata"]["RequirementId"]
    rows = []
    for cycle in state["Cycles"]:
        current = cycle is state["Current"]
        stage = observation["Stage"] if current else cycle["Outcome"]
        revision = cycle["Close"]["Payload"]["SourceCommit"] if cycle["Close"] else commit
        candidate = "docs/requirements/" + ident + "/开发记录.md"
        if cycle["Close"]:
            if candidate not in cycle["Close"]["Payload"]["Sources"]:
                candidate = "docs/requirements/" + ident + "/" + cycle["BaselineRef"]
        elif candidate not in paths:
            candidate = "docs/requirements/" + ident + "/" + cycle["BaselineRef"]
        if current and cycle["Ordinal"] > 1 and cycle["Iteration"] is None:
            candidate = "docs/requirements/" + ident + "/" + cycle["BaselineRef"]
        detail_url = f"{web_base}/{revision}/{quote(candidate, safe='/')}" if revision and web_base else None
        if revision is None:
            detail_url = Path(candidate).relative_to("docs").as_posix()
        rows.append({"RequirementId": ident, "Cycle": cycle["Ordinal"], "Iteration": cycle["Iteration"], "Title": cycle["Title"],
                     "Summary": cycle["Close"]["Payload"]["Summary"] if cycle["Close"] else cycle["Begin"]["Payload"]["Instruction"],
                     "Stage": stage, "Mode": cycle["Mode"], "TrackingHead": state["HeadDigest"],
                     "Chain": [r.sha(r.canonical(e)) for e in state["Events"]], "RegistrationDigest": r.sha(r.canonical(state["Metadata"])),
                     "SourceRef": ref, "SourceCommit": commit, "DetailCommit": revision, "DetailPath": candidate, "DetailUrl": detail_url,
                     "EvidenceStatus": observation.get("EvidenceStatus", "LOCAL_RECHECKED"), "ObservedAt": observation.get("ObservedAt"),
                     "SourcePinsDigest": observation.get("SourcePinsDigest"),
                     "NextActions": observation.get("NextActions", []) if current else [], "HumanAccepted": False, "QualityPassed": False})
    return rows


def overview(repo):
    """Read-only: local worktree and cached uploaded refs, with explicit provenance."""
    repo = r.repository(repo)
    config = r.batch(repo)
    grouped, local, problems = {}, {}, []
    root = r.path(repo, "docs/requirements")
    for folder in sorted(root.iterdir()) if root.exists() else []:
        if not g.valid_requirement_id(folder.name) or folder.name in config["ExcludedRequirementIds"] or not (folder / r.MANIFEST).is_file():
            continue
        try:
            state = r.records(repo, folder.name)
            obs = local_observation(repo, folder.name)
            files = {p.relative_to(repo).as_posix() for p in folder.glob("*") if p.is_file()}
            for row in entries(repo, state, obs, ref="LOCAL_WORKTREE", commit=None, web_base=config["WebBase"], paths=files):
                local[(row["RequirementId"], row["Cycle"])] = row
        except (ValueError, OSError, KeyError, TypeError):
            problems.append({"Scope": "LOCAL_REQUIREMENT", "RequirementId": folder.name, "Code": "INVALID_TRACKING_SOURCE"})
    if config["Remote"]:
        cache, _ = remote_cache(repo, config["Remote"])
        object_repo = cache if cache.exists() else repo
        refs = r.git(object_repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes/" + config["Remote"] + "/").stdout.decode("utf-8").splitlines()
        g.require(len(refs) <= 128, "uploaded branch count")
        for item in refs:
            ref, commit = item.split()
            if ref.endswith("/HEAD"):
                continue
            snapshot = Snapshot(object_repo, commit)
            uploaded_batch = snapshot.load(r.BATCH)
            if uploaded_batch is None or r.decode(uploaded_batch).get("BatchId") != config["BatchId"]:
                continue
            for name in sorted(snapshot.paths):
                match = re.fullmatch(r"docs/requirements/([^/]+)/tracking\.json", name)
                if not match or match[1] in config["ExcludedRequirementIds"] or not g.valid_requirement_id(match[1]):
                    continue
                ident, prefix = match[1], "docs/requirements/" + match[1] + "/"
                try:
                    state = r.records(repo, ident, loader=lambda n: snapshot.load(prefix+n),
                                      event_names=[n[len(prefix):] for n in snapshot.paths if n.startswith(prefix+"tracking/events/")], configuration=config)
                    obs = uploaded_observation(snapshot, prefix, state)
                    for row in entries(repo, state, obs, ref=ref, commit=commit, web_base=config["WebBase"], paths=snapshot.paths):
                        grouped.setdefault((ident, row["Cycle"]), []).append(row)
                except (ValueError, OSError, KeyError, TypeError):
                    problems.append({"Scope": "UPLOADED_REQUIREMENT", "SourceRef": ref, "RequirementId": ident, "Code": "INVALID_TRACKING_SOURCE"})
    result = []
    for key in sorted(set(grouped) | set(local)):
        candidates = grouped.get(key, [])
        if not candidates:
            row = local[key]
            row["Publication"] = "LOCAL_NOT_SHARED"
        else:
            maximal = []
            for candidate in candidates:
                if any(candidate["SourceCommit"] != other["SourceCommit"] and candidate["RegistrationDigest"] == other["RegistrationDigest"] and
                       candidate["TrackingHead"] in other["Chain"] and
                       r.git(object_repo, "merge-base", "--is-ancestor", candidate["SourceCommit"], other["SourceCommit"], allowed=(0, 1)).returncode == 0 for other in candidates):
                    continue
                maximal.append(candidate)
            signatures = {(v["RegistrationDigest"], v["TrackingHead"], v["Stage"], v["SourcePinsDigest"]) for v in maximal}
            row = dict(sorted(maximal, key=lambda v: v["SourceRef"])[0])
            row["Publication"] = "UPLOADED"
            row["UploadedSources"] = [{"Ref": v["SourceRef"], "Commit": v["SourceCommit"]} for v in maximal]
            if len(signatures) > 1:
                row.update(Stage="CROSS_BRANCH_CONFLICT", EvidenceStatus="UNDETERMINED", DetailUrl=None)
            if key in local:
                row["LocalObservation"] = {k: local[key][k] for k in ("Stage", "Mode", "TrackingHead", "ObservedAt")}
        row.pop("Chain", None)
        result.append(row)
    return {"ResultType": "REQUIREMENT_OVERVIEW", "RequirementTrackingVersion": r.VERSION, "ReadOnly": True,
            "GeneratedAt": r.now(), "BatchId": config["BatchId"], "RemoteKnowledge": "CACHED_REFS_ONLY", "Entries": result,
            "Problems": problems, "UniqueRequirements": len({e["RequirementId"] for e in result}), "IterationRows": len(result),
            "QualityPassed": False, "HumanAccepted": False}


LABELS = {"WAITING_FOR_G1": "待需求确认", "PLANNING": "规划中（G1记录待完整核验）", "G1_REJECTED": "需求确认被拒绝",
          "MANUAL_TRACKING": "已退出治理", "MANUAL_DELIVERY_RECORDED": "已退出·人工交付声明", "DELIVERY_RECORDED": "交付已登记", "TERMINATED": "本轮已终止",
          "WAITING_FOR_G2": "待执行就绪确认", "IMPLEMENTATION_INCOMPLETE": "开发中", "IMPLEMENTATION_BLOCKED": "开发受阻",
          "TEST_PREREQUISITE_WAIT": "测试前置等待", "NEXT_ITERATION_PLANNING": "下一迭代规划中", "CROSS_BRANCH_CONFLICT": "分支信息冲突"}


def markdown(report):
    lines = [MARKER, "# 需求管理总表", "", f"批次 `{report['BatchId']}`；生成于 {report['GeneratedAt']}；远端取数：{report['RemoteKnowledge']}。", "",
             "总表是导航投影；共享状态只覆盖已上传记录，分支报告不替代正式门禁核验。", "",
             "| 需求 / 迭代 | 摘要 | 当前阶段 | 下一动作 | 过程记录 | 来源 | 最近观察 |", "|---|---|---|---|---|---|---|"]
    for row in report["Entries"]:
        ident = row["RequirementId"] + " / " + (row["Iteration"] or f"待分配（第{row['Cycle']}轮）")
        label = LABELS.get(row["Stage"], row["Stage"])
        if row["Publication"] == "UPLOADED":
            label = "上传观察：" + label
        if row.get("LocalObservation") and row["LocalObservation"]["TrackingHead"] != row["TrackingHead"]:
            label += "；本地：" + LABELS.get(row["LocalObservation"]["Stage"], row["LocalObservation"]["Stage"])
        navigation = "[过程](" + row["DetailUrl"] + ")" if row["DetailUrl"] else "`git show " + str(row["DetailCommit"]) + ":" + row["DetailPath"] + "`"
        source = row["SourceRef"] + (" @ " + row["SourceCommit"][:12] if row["SourceCommit"] else "（未共享）")
        action = "; ".join(str(x.get("Action", "")) for x in row["NextActions"] if isinstance(x, dict))
        values = [ident, row["Title"] + "：" + row["Summary"][:200], label, action or "按原事实与职责继续", navigation, source, row["ObservedAt"] or "无已上传观察"]
        lines.append("| " + " | ".join(v if i == 4 else g.cell(v) for i, v in enumerate(values)) + " |")
    if report["Problems"]:
        lines += ["", "存在 " + str(len(report["Problems"])) + " 项来源无法核验，查询 JSON 中保留缺项；未把它们推断为完成。"]
    return "\n".join(lines) + "\n"


def refresh(repo, *, fetch=True):
    repo = r.repository(repo)
    config = r.batch(repo)
    remote_state = fetch_uploaded(repo, config["Remote"]) if fetch else "FETCH_NOT_REQUESTED_CACHED_REFS"
    from governance_project import write_atomic
    with r.mutation(repo):
        table = r.path(repo, r.TABLE)
        if table.exists():
            g.require(table.read_text(encoding="utf-8-sig").startswith(MARKER), "overview file is generated and owned")
        requirements = r.path(repo, "docs/requirements")
        for folder in requirements.iterdir() if requirements.exists() else []:
            if g.valid_requirement_id(folder.name) and folder.name not in config["ExcludedRequirementIds"] and (folder / r.MANIFEST).is_file():
                # Invalid registrations stop the write; read-only overview reports the issue.
                r.records(repo, folder.name)
                write_atomic(r.path(folder, PROJECTION), r.canonical(local_observation(repo, folder.name)) + b"\n")
        report = overview(repo)
        report.update(ReadOnly=False, RemoteKnowledge=remote_state)
        write_atomic(table, markdown(report).encode("utf-8"))
        report["TableRef"] = r.TABLE
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.error = lambda _m: parser.exit(2, "Invalid overview arguments.\n")
    parser.add_argument("action", choices=("query", "refresh"))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    a = parser.parse_args(argv)
    try:
        result = overview(a.repo) if a.action == "query" else refresh(a.repo, fetch=not a.no_fetch)
        print(markdown(result) if a.format == "markdown" else json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not result["Problems"] else 1
    except (ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired):
        print("Unable to build requirement overview; review tracking sources, ownership and remote availability.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
