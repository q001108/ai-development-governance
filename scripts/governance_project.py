"""Preview/apply a pinned governance package; run requirements with their original release.

Project facts and configuration remain project-owned. No Git commits, publication,
business-record migration, or native Codex installation are performed here.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
import governance_package as package
import governance_tools as g


FORMAT = "v1.0"
LOCK = ".governance/plugin.lock.json"
PROJECT = ".governance/project.json"
BEGIN = b"<!-- development-governance:begin -->"
END = b"<!-- development-governance:end -->"
BLOCK = BEGIN + b"\n[Development governance](.governance/entry.md)\n" + END
ATTR_BEGIN = b"# development-governance:begin"
ATTR_END = b"# development-governance:end"
IGNORE_BLOCK = b"# development-governance:begin\n/.governance/local/\n# development-governance:end"
RELEASE_FIELDS = ("PluginName", "PluginVersion", "GovernanceVersion", "TestingVersion", "SourceCommit", "SourceDirty")
DIGEST = r"[0-9a-f]{64}"
TEMPLATES = {
    "baseline": "docs/Agent治理/需求基线模板.md",
    "context": "docs/Agent治理/开发上下文包模板.md",
    "development": "docs/Agent治理/开发记录模板.md",
    "qa": "docs/Agent治理/临时质量验证记录模板.md",
    "review": "docs/Agent治理/只读评审记录模板.md",
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def path_at(root, name):
    package.safe_relative(name)
    return g.safe_path(root, Path(root).joinpath(*name.split("/")))


def read_bytes(root, name):
    path = path_at(root, name)
    if not path.exists():
        return None
    g.require(path.is_file() and path.stat().st_size <= 16 * 1024 * 1024, "regular bounded project file")
    return path.read_bytes()


def read_json(root, name):
    data = read_bytes(root, name)
    g.require(data is not None, "required project metadata")
    value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=g.unique_object)
    g.require(isinstance(value, dict), "metadata object")
    return value


def metadata(verified):
    return {key: verified[key] for key in RELEASE_FIELDS}


def validate_metadata(value):
    g.exact_object(value, "release identity", RELEASE_FIELDS)
    g.require(value["PluginName"] == "development-governance", "plugin identity")
    for key in RELEASE_FIELDS[:-1]:
        g.string(value[key], "release identity text")
    g.require(type(value["SourceDirty"]) is bool, "candidate identity")


def managed_name(name):
    return (name in {".governance/run.py", ".governance/entry.md", ".governance/templates.md"}
            or name.startswith(("docs/Agent治理/", "docs/治理版本说明/", "docs/工具说明/", ".codex/agents/", "rulepacks/"))
            or name in {"docs/开发规范/项目开发总则.md", "docs/开发规范/测试规范.md"}
            or bool(re.fullmatch(r"scripts/governance_[a-z_]+\.py", name)))


def load_lock(repo):
    if read_bytes(repo, LOCK) is None:
        return None
    value = read_json(repo, LOCK)
    g.exact_object(value, "plugin lock", ("ProjectFormatVersion", "ActivePackageDigest", "Releases", "ManagedFiles"))
    g.require(value["ProjectFormatVersion"] == FORMAT and isinstance(value["Releases"], dict) and bool(value["Releases"]), "lock format/releases")
    for key, item in value["Releases"].items():
        g.require(re.fullmatch(DIGEST, key), "release digest")
        validate_metadata(item)
    g.require(value["ActivePackageDigest"] in value["Releases"], "locked active release")
    g.require(isinstance(value["ManagedFiles"], dict), "managed file map")
    for name, pin in value["ManagedFiles"].items():
        path_at(repo, name)
        g.require(managed_name(name) and isinstance(pin, str) and re.fullmatch(DIGEST, pin), "managed ownership/hash")
    return value


def verify_release(repo, lock, pin):
    g.require(pin in lock["Releases"] and re.fullmatch(DIGEST, pin), "retained release binding")
    root = path_at(repo, ".governance/releases/" + pin)
    verified = package.verify_package(root)
    g.require(verified["PackageDigest"] == pin and metadata(verified) == lock["Releases"][pin], "release lock integrity")
    return root, verified


def project_config(repo):
    if read_bytes(repo, PROJECT) is None:
        return None
    value = read_json(repo, PROJECT)
    g.require(value.get("ProjectFormatVersion") == FORMAT, "project adaptation format")
    declaration = value.get("RuleDeclaration")
    if declaration is not None:
        path_at(repo, declaration)
    g.require(value.get("LocalRuntimeDirectory") in (None, ".governance/local"), "local runtime adaptation")
    g.require(value.get("CompatibilityMode") in (None, "MANAGED", "PRESERVE_EXISTING"), "compatibility ownership mode")
    g.require(value.get("TemplateLayout") in (None, "MIRRORS", "RELEASE_ONLY"), "template layout")
    return value


# This small bootstrap verifies bytes before importing code from the dependency.
# Release-specific routing and package validation live in the verified manager.
BOOTSTRAP = '''"""Generated governance launcher. Project configuration lives in project.json."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
sys.dont_write_bytecode = True

def safe(root, name):
    if not isinstance(name, str) or "\\\\" in name or any(not p or p in (".", "..") or re.search(r'[\\x00-\\x1f:*?<>|]', p) for p in name.split("/")):
        raise ValueError("unsafe dependency path")
    path = root.joinpath(*name.split("/"))
    if not path.resolve().is_relative_to(root.resolve()) or any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
        raise ValueError("symbolic dependency path")
    return path

def main():
    root = Path(__file__).absolute().parents[1]
    lock = json.loads(safe(root, ".governance/plugin.lock.json").read_text(encoding="utf-8"))
    pin = lock["ActivePackageDigest"]
    if not isinstance(pin, str) or not re.fullmatch("[0-9a-f]{64}", pin):
        raise ValueError("invalid dependency digest")
    release = safe(root, ".governance/releases/" + pin)
    manifest = json.loads(safe(release, "release.json").read_text(encoding="utf-8"))
    data = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(data).hexdigest() != pin:
        raise ValueError("dependency manifest changed")
    expected = manifest["Files"]
    actual = set()
    for path in release.rglob("*"):
        safe(release, path.relative_to(release).as_posix())
        if path.is_file():
            actual.add(path.relative_to(release).as_posix())
    if actual != set(expected) | {"release.json"}:
        raise ValueError("dependency inventory changed")
    for name, checksum in expected.items():
        if hashlib.sha256(safe(release, name).read_bytes()).hexdigest() != checksum:
            raise ValueError("dependency content changed")
    manager = safe(release, "scripts/governance_project.py")
    return subprocess.call([sys.executable, "-B", str(manager), "run", "--repo", str(root), "--", *sys.argv[1:]], cwd=root)

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError):
        print("Governance dependency is missing or invalid; review the project lock and retained package.", file=sys.stderr)
        raise SystemExit(2)
'''


def launcher(stem):
    return (f'"""Generated launcher for the project-pinned {stem}."""\n'
            'from pathlib import Path\nimport subprocess\nimport sys\n'
            'sys.dont_write_bytecode = True\n'
            'if __name__ == "__main__":\n'
            '    root = Path(__file__).absolute().parents[1]\n'
            f'    raise SystemExit(subprocess.call([sys.executable, "-B", str(root / ".governance/run.py"), "{stem}", *sys.argv[1:]], cwd=root))\n').encode("utf-8")


def agents_entry(current):
    if current is None:
        return b"# Project instructions\n\n" + BLOCK + b"\n"
    g.require(current.count(BEGIN) == current.count(END) and current.count(BEGIN) <= 1, "unique AGENTS managed block")
    if BEGIN not in current:
        return current + (b"" if current.endswith(b"\n\n") else b"\n\n") + BLOCK + b"\n"
    start, end = current.index(BEGIN), current.index(END) + len(END)
    g.require(start < end and current[start:end] == BLOCK, "unchanged AGENTS managed block")
    return current


def attribute_block(names):
    fixed = {"AGENTS.md", ".gitattributes", LOCK, PROJECT, ".governance/run.py", ".governance/entry.md"}
    patterns = [b"/.governance/releases/** -text"]
    patterns.extend((json.dumps("/" + name, ensure_ascii=False) + " -text").encode("utf-8") for name in sorted(set(names) | fixed))
    return ATTR_BEGIN + b"\n" + b"\n".join(patterns) + b"\n" + ATTR_END


def attributes_entry(current, previous, incoming):
    block = attribute_block(incoming)
    if current is None:
        return block + b"\n"
    g.require(current.count(ATTR_BEGIN) == current.count(ATTR_END) and current.count(ATTR_BEGIN) <= 1, "unique attributes managed block")
    if ATTR_BEGIN not in current:
        g.require(not previous, "existing attributes block was removed")
        return current + (b"" if current.endswith(b"\n") else b"\n") + block + b"\n"
    start, end = current.index(ATTR_BEGIN), current.index(ATTR_END) + len(ATTR_END)
    g.require(start < end and current[start:end] == attribute_block(previous), "unchanged attributes managed block")
    return current[:start] + block + current[end:]


def ignore_entry(current):
    if current is None:
        return IGNORE_BLOCK + b"\n"
    start, end = b"# development-governance:begin", b"# development-governance:end"
    g.require(current.count(start) == current.count(end) and current.count(start) <= 1, "unique runtime ignore block")
    if start in current:
        first, last = current.index(start), current.index(end) + len(end)
        g.require(first < last and current[first:last].replace(b"\r\n", b"\n") == IGNORE_BLOCK, "unchanged runtime ignore block")
        return current
    return current + (b"" if current.endswith(b"\n") else b"\n") + IGNORE_BLOCK + b"\n"


def desired_files(repo, package_root, verified, *, preserve_existing=False):
    lock = load_lock(repo)
    if lock:
        g.require(read_bytes(repo, "AGENTS.md") is not None and read_bytes(repo, ".gitattributes") is not None,
                  "project entry or byte policy was removed; resolve explicitly")
    result = {}
    release_only = (project_config(repo) or {}).get("TemplateLayout") == "RELEASE_ONLY"
    for name in verified["Files"]:
        if release_only and name in TEMPLATES.values():
            continue  # Existing paths are retained; convergence never deletes project files.
        if managed_name(name) and not name.startswith("scripts/"):
            result[name] = read_bytes(package_root, name)
        elif re.fullmatch(r"scripts/governance_[a-z_]+\.py", name) and Path(name).stem not in {"governance_project", "governance_package"}:
            result[name] = launcher(Path(name).stem)
    result[".governance/run.py"] = BOOTSTRAP.encode("utf-8")
    result[".governance/entry.md"] = (
        "# 项目治理入口\n\n"
        f"默认插件 `{verified['PluginVersion']}`，治理 `{verified['GovernanceVersion']}`，测试 `{verified['TestingVersion']}`。\n\n"
        f"读取[固定发行入口](releases/{verified['PackageDigest']}/AGENTS.md)后按当前需求绑定执行。\n"
        "使用 `python .governance/run.py <工具文件名去掉.py> ...`。\n"
        "已启用需求管理时，实施/续接先调用统一入口 `python .governance/run.py start --repo .`；只读任务加 `--intent query`。\n"
        "新实施请求用 `--intent new --title <摘要> --instruction <需求> --request-key <AI生成且重试复用的键>`；仅凭仓库里有一条需求不能自动续接。\n"
        + ("已有脚本、角色和项目文档由项目保留；新能力必须走固定发行运行器，不能把旧 scripts 当作新内核入口。\n" if preserve_existing else "生成的兼容 scripts 入口转发到同一运行器。\n")
        +
        "工具没有需求参数时，在工具名后加 `--for-requirement <ID>` 选择已有需求版本；该参数由运行器消费。\n"
        "新需求先 `python .governance/run.py bind --requirement-id <ID>`；已有需求继续原绑定，未绑定的历史需求需先明确迁移方案。\n"
        "项目配置由 project.json 和既有本地测试/规则声明提供，不在此重复维护。AGENTS 项目规则及原治理门禁继续适用。\n"
        "插件默认版本升级不迁移已有需求；查阅旧需求规则时使用其 plugin-binding.json 指定的发行目录。\n"
        "通用模板正文见[模板导航](templates.md)；已有需求必须使用 template --requirement-id 选择原绑定，不从旧镜像复制。\n"
    ).encode("utf-8")
    result[".governance/templates.md"] = template_navigation(verified)
    result["AGENTS.md"] = agents_entry(read_bytes(repo, "AGENTS.md"))
    if project_config(repo) is None:
        result[PROJECT] = canonical({"ProjectFormatVersion": FORMAT, "RuleDeclaration": None, "LocalRuntimeDirectory": ".governance/local",
                                     "CompatibilityMode": "PRESERVE_EXISTING" if preserve_existing else "MANAGED"}) + b"\n"
    result[".gitignore"] = ignore_entry(read_bytes(repo, ".gitignore"))
    # The ledger becomes project-owned immediately and is never upgraded.
    if lock is None and read_bytes(repo, "docs/需求版本管理.md") is None:
        result["docs/需求版本管理.md"] = read_bytes(package_root, "docs/需求版本管理.md")
    previous = set(lock["ManagedFiles"]) if lock else set()
    incoming_names = {name for name in result if managed_name(name)}
    if preserve_existing:
        incoming_names = {name for name in incoming_names if name in previous or name.startswith(".governance/") or read_bytes(repo, name) is None}
    result[".gitattributes"] = attributes_entry(read_bytes(repo, ".gitattributes"), previous,
                                              previous | incoming_names)
    return result


def template_navigation(verified):
    rows = ["# 通用模板导航", "", "模板正文由固定发行提供，项目专用规则与真实需求记录继续保存在项目中。",
            "已有需求用 `python .governance/run.py template --kind <下表类型> --requirement-id <ID>` 定位原绑定的正文；命令只读，不登记、绑定或迁移需求。",
            "新需求尚未登记时，仅预览默认模板可显式使用 `--default`；已登记后必须携带 ID。未绑定的历史需求按原 Git/版本来源读取，不回退到当前默认版。",
            "", f"以下链接只供当前默认插件 `{verified['PluginVersion']}` 的预览，不能替代旧需求绑定。", "",
            "| 类型 | 默认发行模板 |", "|---|---|"]
    for kind, name in TEMPLATES.items():
        if name in verified["Files"]:
            rows.append(f"| {kind} | [{Path(name).stem}](releases/{verified['PackageDigest']}/{name}) |")
    rows += ["", "TemplateLayout=RELEASE_ONLY 时，后续接入/升级不生成通用模板的 docs 镜像；已有副本不自动删除，需单独核对引用、定制和历史来源后收敛。", ""]
    return "\n".join(rows).encode("utf-8")


def locate_template(repo, lock, kind, *, requirement_id=None, default=False):
    g.require(kind in TEMPLATES and ((requirement_id is not None) != default), "select a requirement or explicitly preview the default template")
    selected = binding(repo, lock, requirement_id) if requirement_id is not None else None
    pin = selected["PackageDigest"] if selected else lock["ActivePackageDigest"]
    release, verified = verify_release(repo, lock, pin)
    name = TEMPLATES[kind]
    g.require(name in verified["Files"], "template belongs to selected release")
    data = read_bytes(release, name)
    g.require(data is not None and digest(data) == verified["Files"][name], "template bytes match selected release")
    return {"ResultType": "GOVERNANCE_TEMPLATE_RESOURCE", "ReadOnly": True, "RequirementId": requirement_id,
            "Selection": "REQUIREMENT_BINDING" if selected else "EXPLICIT_DEFAULT_PREVIEW", "PluginVersion": verified["PluginVersion"],
            "PackageDigest": pin, "Kind": kind, "LogicalRef": name,
            "ResourceRef": (release / name).relative_to(repo).as_posix(), "Sha256": digest(data),
            "GatesChanged": False, "FactsWritten": False}


def git_observation(repo):
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    args = ["git", "-C", str(repo)]
    result = subprocess.run(args + ["rev-parse", "--show-toplevel"], capture_output=True, env=env)
    g.require(result.returncode == 0 and Path(os.fsdecode(result.stdout).strip()).resolve() == repo.resolve(), "target Git repository root")
    result = subprocess.run(args + ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", ".", ":(exclude).governance/apply.lock"], capture_output=True, env=env)
    g.require(result.returncode == 0, "project Git status")
    dirty, records, index = set(), result.stdout.split(b"\0"), 0
    while index < len(records) and records[index]:
        record = records[index]
        dirty.add(os.fsdecode(record[3:]).replace("\\", "/"))
        if b"R" in record[:2] or b"C" in record[:2]:
            index += 1
            if index < len(records):
                dirty.add(os.fsdecode(records[index]).replace("\\", "/"))
        index += 1
    return {"StatusDigest": digest(result.stdout), "DirtyPaths": sorted(dirty)}


def preview_project(repo, package_dir, *, preserve_existing=False):
    repo, package_dir = Path(os.path.abspath(repo)), Path(os.path.abspath(package_dir))
    g.safe_path(repo, repo)
    git_state = git_observation(repo)
    verified = package.verify_package(package_dir)
    g.require(verified["PluginName"] == "development-governance", "supported plugin")
    lock = load_lock(repo)
    config = project_config(repo)
    preserve_existing = preserve_existing or (config or {}).get("CompatibilityMode") == "PRESERVE_EXISTING"
    if lock:
        g.require(config is not None, "existing project adaptation")
        for pin in lock["Releases"]:
            verify_release(repo, lock, pin)
        active = lock["Releases"][lock["ActivePackageDigest"]]
        g.require(all(active[key] == verified[key] for key in ("GovernanceVersion", "TestingVersion")), "governance/testing migration requires an explicit migration workflow")
    else:
        release_dir = path_at(repo, ".governance/releases")
        g.require(not release_dir.exists() or not any(release_dir.iterdir()), "unmanaged release directory")
    target = path_at(repo, ".governance/releases/" + verified["PackageDigest"])
    if target.exists():
        g.require(lock is not None and verified["PackageDigest"] in lock["Releases"], "unmanaged release collision")
    wanted = desired_files(repo, package_dir, verified, preserve_existing=preserve_existing)
    previous = lock["ManagedFiles"] if lock else {}
    files, conflicts = [], []
    for name in sorted(set(wanted) | set(previous)):
        current = read_bytes(repo, name)
        current_pin = digest(current) if current is not None else None
        incoming = wanted.get(name)
        new_pin = digest(incoming) if incoming is not None else None
        old_pin = previous.get(name)
        if name not in wanted:
            action = "RETAIN_REMOVED"
        elif preserve_existing and old_pin is None and current is not None and managed_name(name) and not name.startswith(".governance/"):
            action = "PRESERVE_UNMANAGED"
        elif current_pin == new_pin:
            action = "UNCHANGED" if old_pin else "ADOPT_IDENTICAL"
        elif name in {"AGENTS.md", ".gitattributes", ".gitignore", PROJECT, "docs/需求版本管理.md"}:
            action = "WRITE"
        elif old_pin is None:
            action = "WRITE" if current is None else ("PRESERVE_UNMANAGED" if preserve_existing and managed_name(name) and not name.startswith(".governance/") else "CONFLICT")
        elif new_pin == old_pin:
            action = "PRESERVE_PROJECT"
        elif current_pin == old_pin:
            action = "WRITE"
        else:
            action = "CONFLICT"
        if action == "WRITE" and old_pin is not None and name in git_state["DirtyPaths"]:
            action = "CONFLICT_UNCOMMITTED"
        if action.startswith("CONFLICT"):
            conflicts.append(name)
        files.append({"Path": name, "Action": action, "PreviousSha256": old_pin, "CurrentSha256": current_pin, "IncomingSha256": new_pin})
    # These project-owned metadata bytes also participate in stale-preview detection.
    observed = {name: (digest(data) if (data := read_bytes(repo, name)) is not None else None) for name in (LOCK, PROJECT)}
    return {"ProjectPlanVersion": FORMAT, "TargetRepo": str(repo), "SourcePackage": str(package_dir),
            "PackageDigest": verified["PackageDigest"], "Release": metadata(verified), "Mode": "UPGRADE" if lock else "INIT",
            "Files": files, "Conflicts": conflicts, "Applicable": not conflicts, "Observed": observed, "Git": git_state, "PreserveExisting": preserve_existing}


def write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".governance-write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def verify_byte_attributes(repo, names):
    """Nested or developer-local attributes must not undo the release byte policy."""
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    listing = b"\0".join(name.encode("utf-8") for name in sorted(names)) + b"\0"
    result = subprocess.run(["git", "-C", str(repo), "check-attr", "-z", "--stdin", "text"], input=listing, capture_output=True, env=env)
    fields = result.stdout.split(b"\0")[:-1]
    g.require(result.returncode == 0 and len(fields) == len(names) * 3, "managed byte attributes query")
    g.require(all(fields[index] == b"unset" for index in range(2, len(fields), 3)), "nested or local Git attributes override byte preservation")


def apply_project(repo, plan):
    repo = Path(os.path.abspath(repo))
    g.require(isinstance(plan, dict) and plan.get("ProjectPlanVersion") == FORMAT, "upgrade plan format")
    g.require(plan.get("TargetRepo") == str(repo), "preview target repository")
    fresh = preview_project(repo, plan.get("SourcePackage", ""), preserve_existing=plan.get("PreserveExisting", False))
    g.require(fresh == plan, "stale or modified preview; generate a new preview")
    g.require(fresh["Applicable"], "upgrade conflicts require project-side resolution")
    package_root = Path(plan["SourcePackage"])
    verified = package.verify_package(package_root)
    wanted = desired_files(repo, package_root, verified, preserve_existing=plan["PreserveExisting"])
    lock = load_lock(repo) or {"ProjectFormatVersion": FORMAT, "ActivePackageDigest": plan["PackageDigest"], "Releases": {}, "ManagedFiles": {}}
    lock["ActivePackageDigest"] = plan["PackageDigest"]
    lock["Releases"][plan["PackageDigest"]] = metadata(verified)
    for item in plan["Files"]:
        if managed_name(item["Path"]) and item["IncomingSha256"] is not None and item["Action"] != "PRESERVE_UNMANAGED":
            lock["ManagedFiles"][item["Path"]] = item["IncomingSha256"]
    release = path_at(repo, ".governance/releases/" + plan["PackageDigest"])
    existed = release.exists()
    before, written, new_release_files = {}, {}, []
    mutex = path_at(repo, ".governance/apply.lock")
    mutex.parent.mkdir(parents=True, exist_ok=True)
    try:
        with mutex.open("xb") as stream:
            stream.write(b"apply in progress\n")
    except FileExistsError:
        raise g.Invalid("Another apply may be in progress; inspect .governance/apply.lock before retrying") from None
    try:
        # The lock prevents simultaneous installers; byte rechecks protect a stale preview.
        g.require(preview_project(repo, package_root, preserve_existing=plan["PreserveExisting"]) == plan, "preview changed before apply")
        if not existed:
            release.mkdir(parents=True, exist_ok=False)
            for name in [*verified["Files"], "release.json"]:
                data = read_bytes(package_root, name)
                target = path_at(release, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(data)
                new_release_files.append((target, digest(data)))
            installed = package.verify_package(release)
            g.require(installed["PackageDigest"] == plan["PackageDigest"], "copied release integrity")
        for item in plan["Files"]:
            if item["Action"] != "WRITE":
                continue
            name = item["Path"]
            before[name] = read_bytes(repo, name)
            g.require((digest(before[name]) if before[name] is not None else None) == item["CurrentSha256"], "project changed during apply")
            write_atomic(path_at(repo, name), wanted[name])
            written[name] = digest(wanted[name])
        byte_paths = set(lock["ManagedFiles"]) | {"AGENTS.md", ".gitattributes", LOCK, PROJECT}
        byte_paths.update(".governance/releases/" + plan["PackageDigest"] + "/" + name for name in [*verified["Files"], "release.json"])
        verify_byte_attributes(repo, byte_paths)
        # The dependency lock is the last mutation, after every managed write succeeds.
        lock_data = canonical(lock) + b"\n"
        before[LOCK] = read_bytes(repo, LOCK)
        g.require((digest(before[LOCK]) if before[LOCK] is not None else None) == plan["Observed"][LOCK], "dependency lock changed during apply")
        config_now = read_bytes(repo, PROJECT)
        expected_config = wanted.get(PROJECT)
        g.require((digest(config_now) if config_now is not None else None) ==
                  (digest(expected_config) if expected_config is not None else plan["Observed"][PROJECT]), "project configuration changed during apply")
        if before[LOCK] != lock_data:
            write_atomic(path_at(repo, LOCK), lock_data)
            written[LOCK] = digest(lock_data)
        return {"Applied": True, "PackageDigest": plan["PackageDigest"], "WrittenPaths": sorted(written), "RetainedReleases": sorted(lock["Releases"])}
    except BaseException:
        for name in reversed(written):
            path = path_at(repo, name)
            if read_bytes(repo, name) is not None and digest(read_bytes(repo, name)) == written[name]:
                if before[name] is None:
                    path.unlink()
                else:
                    write_atomic(path, before[name])
        for path, pin in reversed(new_release_files):
            if path.is_file() and digest(path.read_bytes()) == pin:
                path.unlink()
        if not existed and release.exists():
            # Remove only empty directories created inside this exact new dependency.
            for directory in sorted((p for p in release.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                if not any(directory.iterdir()):
                    directory.rmdir()
            if not any(release.iterdir()):
                release.rmdir()
        raise
    finally:
        mutex.unlink(missing_ok=True)


def binding(repo, lock, requirement_id, *, create=False):
    g.require(g.valid_requirement_id(requirement_id), "RequirementId")
    base = "docs/requirements/" + requirement_id
    name = base + "/plugin-binding.json"
    if read_bytes(repo, name) is not None:
        value = read_json(repo, name)
        g.exact_object(value, "requirement plugin binding", ("ProjectFormatVersion", "RequirementId", "PackageDigest", *RELEASE_FIELDS))
        g.require(value["ProjectFormatVersion"] == FORMAT and value["RequirementId"] == requirement_id, "binding identity/format")
        _, verified = verify_release(repo, lock, value["PackageDigest"])
        g.require({key: value[key] for key in RELEASE_FIELDS} == metadata(verified), "binding release identity")
        return value
    g.require(create, "requirement has no plugin binding; do not silently adopt a default release")
    directory = path_at(repo, base)
    g.require(not directory.exists() or (directory.is_dir() and not any(directory.iterdir())), "existing unbound requirement requires explicit migration")
    pin = lock["ActivePackageDigest"]
    _, verified = verify_release(repo, lock, pin)
    value = {"ProjectFormatVersion": FORMAT, "RequirementId": requirement_id, "PackageDigest": pin, **metadata(verified)}
    directory.mkdir(parents=True, exist_ok=True)
    with path_at(repo, name).open("xb") as stream:
        stream.write(canonical(value) + b"\n")
    return value


def requirement_from_args(repo, args):
    found = set()
    for index, arg in enumerate(args):
        if arg == "--requirement-id":
            g.require(index + 1 < len(args) and g.valid_requirement_id(args[index + 1]), "requirement argument")
            found.add(args[index + 1])
        elif arg.startswith("--requirement-id="):
            value = arg.split("=", 1)[1]
            g.require(g.valid_requirement_id(value), "requirement argument")
            found.add(value)
        for part in arg.replace("\\", "/").split("/"):
            if g.valid_requirement_id(part):
                found.add(part)
        if arg in ("--repo", "--repository"):
            g.require(index + 1 < len(args) and Path(args[index + 1]).absolute().resolve() == repo.resolve(), "runner project argument")
        elif arg.startswith("--repo="):
            g.require(Path(arg.split("=", 1)[1]).absolute().resolve() == repo.resolve(), "runner project argument")
        option, _, attached = arg.partition("=")
        if option in {"--control-profile", "--acceptance-plan", "--requirement-dir", "--adjustment", "--qa-result", "--review-result", "--project"}:
            g.require(bool(attached) or index + 1 < len(args), "project input argument")
            value = attached if "=" in arg else args[index + 1]
            target = Path(value)
            g.safe_path(repo, target if target.is_absolute() else repo / target)
    g.require(len(found) <= 1, "single requirement per invocation")
    return next(iter(found), None)


def runner_arguments(args):
    """A version selector is separate from the legacy tool's own CLI contract."""
    forwarded, selected, index = [], None, 0
    while index < len(args):
        arg = args[index]
        if arg == "--for-requirement" or arg.startswith("--for-requirement="):
            if arg == "--for-requirement":
                index += 1
                g.require(index < len(args), "runner requirement selector")
                value = args[index]
            else:
                value = arg.split("=", 1)[1]
            g.require(selected is None and g.valid_requirement_id(value), "single valid runner requirement selector")
            selected = value
        else:
            forwarded.append(arg)
        index += 1
    return selected, forwarded


def run_project(repo, args):
    repo = Path(os.path.abspath(repo))
    lock = load_lock(repo)
    g.require(lock is not None and project_config(repo) is not None, "initialized project")
    g.require(bool(args), "tool or bind action")
    tool, rest = args[0], args[1:]
    if tool == "start":
        tool = "governance_start"
    if tool == "template":
        parser = argparse.ArgumentParser(description="Locate a read-only template in the requirement's pinned release.")
        parser.add_argument("--kind", choices=tuple(TEMPLATES), required=True)
        selector = parser.add_mutually_exclusive_group(required=True)
        selector.add_argument("--requirement-id")
        selector.add_argument("--default", action="store_true")
        options = parser.parse_args(rest)
        print(json.dumps(locate_template(repo, lock, options.kind, requirement_id=options.requirement_id, default=options.default), indent=2))
        return 0
    if tool == "bind":
        parser = argparse.ArgumentParser(description="Pin a new requirement; existing bindings remain immutable.")
        parser.add_argument("--requirement-id", required=True)
        options = parser.parse_args(rest)
        print(json.dumps(binding(repo, lock, options.requirement_id, create=True), ensure_ascii=False, indent=2))
        return 0
    g.require(bool(re.fullmatch(r"governance_[a-z_]+", tool)) and tool not in {"governance_project", "governance_package"}, "runtime tool")
    selected, rest = runner_arguments(rest)
    requirement = requirement_from_args(repo, rest)
    g.require(selected is None or requirement is None or selected == requirement, "selector matches actual requirement inputs")
    requirement = selected or requirement
    help_only = "--help" in rest or "-h" in rest
    needs_requirement = tool in {"governance_local_tests", "governance_test_session", "governance_status", "governance_candidates"}
    needs_requirement |= tool == "governance_tools" and bool(rest) and rest[0] in {"validate-plan", "validate-g2", "validate-adjustment", "validate-adjusted-quality", "render"}
    g.require(requirement is not None or help_only or not needs_requirement, "requirement cannot be determined for version selection")
    registration = tool == "governance_requirements" and bool(rest) and rest[0] in {"enable", "init"}
    # Startup is a project router; it calls each selected requirement's bound tools.
    registration |= tool == "governance_start"
    pin = binding(repo, lock, requirement)["PackageDigest"] if requirement and not registration else lock["ActivePackageDigest"]
    release, verified = verify_release(repo, lock, pin)
    name = "scripts/" + tool + ".py"
    g.require(name in verified["Files"], "tool belongs to selected release")
    if requirement is not None and not help_only and tool not in {"governance_requirements", "governance_status", "governance_overview", "governance_start"}:
        if not (tool == "governance_test_session" and rest[:1] == ["stop"]):
            from governance_requirements import require_governed
            require_governed(repo, requirement)
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    return subprocess.call([sys.executable, "-B", str(path_at(release, name)), *rest], cwd=repo, env=env)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser("preview", help="Create a local init/upgrade preview; project stays unchanged")
    preview.add_argument("--repo", required=True)
    preview.add_argument("--package", required=True)
    preview.add_argument("--output", required=True)
    preview.add_argument("--preserve-existing", action="store_true", help="Keep existing project/legacy docs, roles and scripts; execute the kernel through the pinned runner")
    apply = commands.add_parser("apply", help="Apply an unchanged, conflict-free preview to the specified project")
    apply.add_argument("--repo", required=True)
    apply.add_argument("--plan", required=True)
    run = commands.add_parser("run", help="Used by the generated project launcher")
    run.add_argument("--repo", required=True)
    run.add_argument("arguments", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    try:
        if options.command == "preview":
            value = preview_project(options.repo, options.package, preserve_existing=options.preserve_existing)
            output = Path(os.path.abspath(options.output))
            g.require(not output.is_relative_to(Path(os.path.abspath(options.repo))), "preview output outside project")
            g.safe_path(output.parent, output)
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("xb") as stream:
                stream.write(canonical(value) + b"\n")
            print(json.dumps({"Applicable": value["Applicable"], "Conflicts": value["Conflicts"], "PackageDigest": value["PackageDigest"]}, ensure_ascii=False, indent=2))
        elif options.command == "apply":
            value = json.loads(Path(options.plan).read_text(encoding="utf-8-sig"), object_pairs_hook=g.unique_object)
            print(json.dumps(apply_project(options.repo, value), ensure_ascii=False, indent=2))
        else:
            args = options.arguments[1:] if options.arguments[:1] == ["--"] else options.arguments
            return run_project(options.repo, args)
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        reason = str(exc) if isinstance(exc, (g.Invalid, package.PackageError)) else "invalid metadata or unavailable file"
        print("Project operation rejected: " + reason + ". Inspect the preview, lock and requirement binding.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
