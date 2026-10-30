"""Build and verify offline governance releases; no installation or publication."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True

RELEASE_VERSION = "v1.0"
GOVERNANCE_VERSION = "v3.0.2"
TESTING_VERSION = "v3.0.1"
PLUGIN_NAME = "development-governance"
HEX = r"[0-9a-f]{64}"
SEMVER = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
FIELDS = {"ReleaseFormatVersion", "PluginName", "PluginVersion", "GovernanceVersion", "TestingVersion", "SourceCommit", "SourceDirty", "Files"}
FIXED_FILES = {
    "plugin.json", "AGENTS.md", "LICENSE", "docs/需求版本管理.md",
    "docs/开发规范/项目开发总则.md", "docs/开发规范/测试规范.md",
}
REQUIRED_FILES = FIXED_FILES | {
    "skills/development-governance/SKILL.md", "scripts/governance_tools.py",
    "scripts/governance_package.py", "scripts/governance_project.py",
    "docs/Agent治理/contracts/control-profile.schema.json",
    "docs/Agent治理/contracts/acceptance-plan.schema.json",
    "docs/Agent治理/contracts/replay-expectations.schema.json",
    "docs/Agent治理/多智能体协同开发治理流程.md",
    "docs/Agent治理/治理运行时事实协议.md",
    "docs/Agent治理/治理配置与验收控制规范.md",
    "docs/工具说明/社区插件与项目适配.md",
    "docs/工具说明/规则组合.md", "docs/工具说明/本地测试准备.md",
    "docs/工具说明/候选快照.md", "docs/工具说明/状态查询.md",
    "docs/工具说明/项目接入与升级.md",
    "docs/工具说明/需求管理.md",
    "docs/治理版本说明/v3.0.2治理修订说明.md",
    "docs/治理版本说明/v3.0.1治理修订说明.md",
}
REQUIRED_FILES |= {f"scripts/governance_{name}.py" for name in (
    "environment", "local_tests", "test_session", "candidates", "status", "rules", "requirements", "overview")}
REQUIRED_FILES |= {f".codex/agents/{name}.toml" for name in (
    "orchestrator", "writer", "qa", "reviewer", "development-trace")}
REQUIRED_FILES |= {f"docs/Agent治理/{name}.md" for name in (
    "Codex运行时拓扑", "开发溯源归档规范", "需求基线模板", "开发上下文包模板",
    "开发记录模板", "临时质量验证记录模板", "只读评审记录模板")}
MAX_FILE_SIZE = 8 * 1024 * 1024
MAX_FILES = 512


class PackageError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise PackageError(reason)


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def file_sha256(data):
    return hashlib.sha256(data).hexdigest()


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "duplicate JSON key")
        value[key] = item
    return value


def read_json(data):
    try:
        return json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PackageError("invalid UTF-8 JSON") from exc


def safe_relative(name):
    require(isinstance(name, str) and bool(name) and "\\" not in name, "POSIX relative package path required")
    require(not PureWindowsPath(name).drive and not name.startswith("/"), "relative package path required")
    parts = name.split("/")
    require(all(part not in ("", ".", "..") and not re.search(r'[\x00-\x1f:*?<>|#%]', part)
                and not part.endswith((".", " ")) for part in parts), "unsafe package path")
    require(all(not re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part, re.I)
                for part in parts), "reserved package path")
    require(not any(part.lower().startswith(".env") or part.lower() in {
        ".git", "secrets", "credentials", "secrets.json", "credentials.json", "id_rsa", "id_ed25519"
    } or PurePosixPath(part).suffix.lower() in {".pem", ".key", ".pfx", ".p12"} for part in parts), "sensitive package path")
    return name


def allowed_asset(name):
    safe_relative(name)
    if name in FIXED_FILES:
        return True
    parts = name.split("/")
    if len(parts) == 2 and parts[0] == "scripts":
        return bool(re.fullmatch(r"governance_[a-z_]+\.py", parts[1]))
    if name.startswith("skills/development-governance/"):
        return PurePosixPath(name).suffix in {".md", ".yaml"}
    if name.startswith("docs/Agent治理/"):
        return PurePosixPath(name).suffix in {".md", ".json"}
    if name.startswith(("docs/治理版本说明/", "docs/工具说明/")):
        return len(parts) == 3 and name.endswith(".md")
    if name.startswith(".codex/agents/"):
        return len(parts) == 3 and name.endswith(".toml")
    if parts[0] == "rulepacks":
        return (len(parts) == 4 and bool(re.fullmatch(r"[a-z][a-z0-9.-]{0,63}", parts[1]))
                and bool(re.fullmatch(r"[1-9][0-9]*\.[0-9]+\.[0-9]+", parts[2]))
                and parts[3] in {"pack.json", "rules.md"})
    return False


def plain_path(root, path):
    """Reject links/junctions before reading, including parents supplied by callers."""
    root, path = Path(root).absolute(), Path(path).absolute()
    require(path.is_relative_to(root), "path escapes package root")
    for current in (root, *root.parents):
        require(not current.is_symlink() and not (hasattr(current, "is_junction") and current.is_junction()), "linked package root")
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        require(not current.is_symlink() and not (hasattr(current, "is_junction") and current.is_junction()), "linked package asset")
    return path


def stable_bytes(root, path):
    path = plain_path(root, path)
    require(path.is_file(), "package file missing")
    before = path.stat()
    require(before.st_size <= MAX_FILE_SIZE, "package file size limit")
    data = path.read_bytes()
    after = path.stat()
    require((before.st_size, before.st_mtime_ns, before.st_ino) ==
            (after.st_size, after.st_mtime_ns, after.st_ino) and len(data) == after.st_size,
            "source changed while reading")
    return data


def check_content(name, data):
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise PackageError("package assets must be UTF-8 text") from exc
    # Source code contains credential-detection regexes; match concrete assignments
    # and known key shapes, not words describing protected fields.
    secret = (r"(?im)-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----|"
              r"\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9_-]{24,})\b|"
              r"^\s*(?:[\"']?(?:password|api[_-]?key|access[_-]?token|authorization|cookie)[\"']?)"
              r"\s*[:=]\s*[\"']?[^\s\"',}]{4,}")
    require(not re.search(secret, text), "possible secret in package asset")
    if name == "docs/需求版本管理.md":
        require(not re.search(r"\|\s*REQ-[A-Z0-9-]+\s*\|", text), "requirement ledger must be an empty template")


def asset_names(source):
    names = set(FIXED_FILES)
    for prefix in ("skills/development-governance", "docs/Agent治理", "docs/治理版本说明", "docs/工具说明", ".codex/agents", "rulepacks"):
        folder = plain_path(source, source / prefix)
        if folder.exists():
            for path in folder.rglob("*"):
                plain_path(source, path)
                if path.is_file():
                    name = path.relative_to(source).as_posix()
                    require(allowed_asset(name), "unexpected file in distribution resource directory")
                    names.add(name)
    for path in (source / "scripts").glob("governance_*.py"):
        name = path.relative_to(source).as_posix()
        require(allowed_asset(name), "unexpected tool path")
        names.add(name)
    require(REQUIRED_FILES <= names, "required distribution assets missing")
    require(len(names) <= MAX_FILES, "package asset count limit")
    return sorted(names)


def source_state(source):
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    def git(*args):
        result = subprocess.run(["git", "-C", str(source), *args], capture_output=True, env=env)
        require(result.returncode == 0, "source must be a readable Git repository")
        return result.stdout
    top = Path(os.fsdecode(git("rev-parse", "--show-toplevel").strip())).resolve()
    require(top == source.resolve(), "build from repository root")
    commit = git("rev-parse", "HEAD").decode("ascii").strip()
    require(bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit)), "source commit identity")
    dirty = bool(git("status", "--porcelain=v1", "--untracked-files=all").strip())
    return commit, dirty


def validate_release(value):
    require(isinstance(value, dict) and set(value) == FIELDS, "release manifest fields")
    require(value["ReleaseFormatVersion"] == RELEASE_VERSION and value["PluginName"] == PLUGIN_NAME, "release identity")
    require(isinstance(value["PluginVersion"], str) and bool(re.fullmatch(SEMVER, value["PluginVersion"])), "fixed plugin version")
    require(value["GovernanceVersion"] == GOVERNANCE_VERSION and value["TestingVersion"] == TESTING_VERSION, "supported governance/testing versions")
    require(isinstance(value["SourceCommit"], str) and bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value["SourceCommit"])), "source commit")
    require(type(value["SourceDirty"]) is bool, "source candidate flag")
    files = value["Files"]
    require(isinstance(files, dict) and REQUIRED_FILES <= set(files) and len(files) <= MAX_FILES, "release file inventory")
    require(len({name.casefold() for name in files}) == len(files), "case-insensitive package collision")
    for name, digest in files.items():
        require(allowed_asset(name) and isinstance(digest, str) and bool(re.fullmatch(HEX, digest)), "release file binding")


def verify_package(package_dir):
    root = Path(package_dir).absolute()
    plain_path(root, root)
    require(root.is_dir(), "package directory missing")
    manifest = read_json(stable_bytes(root, root / "release.json"))
    validate_release(manifest)
    observed = set()
    for path in root.rglob("*"):
        plain_path(root, path)
        if path.is_file():
            observed.add(path.relative_to(root).as_posix())
        else:
            require(path.is_dir(), "unsupported package asset type")
    require(observed == set(manifest["Files"]) | {"release.json"}, "package inventory mismatch")
    for name, digest in manifest["Files"].items():
        data = stable_bytes(root, root / name)
        require(file_sha256(data) == digest, "package asset hash mismatch")
        check_content(name, data)
    plugin = read_json(stable_bytes(root, root / "plugin.json"))
    require(isinstance(plugin, dict) and plugin.get("name") == manifest["PluginName"]
            and plugin.get("version") == manifest["PluginVersion"], "plugin/release identity mismatch")
    return {"Valid": True, "PackageDigest": file_sha256(canonical_json(manifest)), **manifest}


def build_package(source_repo, output_dir, *, allow_dirty=False):
    # Normalize dot components without resolving (and hiding) symbolic parents.
    source, output = Path(os.path.abspath(source_repo)), Path(os.path.abspath(output_dir))
    plain_path(source, source)
    require(not output.exists(), "output directory already exists")
    require(not output.is_relative_to(source), "build output must be outside source repository")
    require(output.parent.is_dir(), "output parent must exist")
    plain_path(output.parent, output.parent)
    initial = source_state(source)
    require(allow_dirty or not initial[1], "dirty source requires explicit allow_dirty")
    names = asset_names(source)
    assets = {}
    for name in names:
        data = stable_bytes(source, source / name)
        check_content(name, data)
        assets[name] = data
    plugin = read_json(assets["plugin.json"])
    require(isinstance(plugin, dict), "plugin manifest object")
    manifest = {"ReleaseFormatVersion": RELEASE_VERSION, "PluginName": plugin.get("name"),
                "PluginVersion": plugin.get("version"), "GovernanceVersion": GOVERNANCE_VERSION,
                "TestingVersion": TESTING_VERSION, "SourceCommit": initial[0], "SourceDirty": initial[1],
                "Files": {name: file_sha256(data) for name, data in assets.items()}}
    validate_release(manifest)
    require(asset_names(source) == names and source_state(source) == initial, "source changed during build")
    require(all(stable_bytes(source, source / name) == data for name, data in assets.items()), "source bytes changed during build")
    temporary = Path(tempfile.mkdtemp(prefix=".governance-build-", dir=output.parent))
    try:
        for name, data in assets.items():
            path = temporary / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (temporary / "release.json").write_bytes(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")
        result = verify_package(temporary)
        # rename is atomic and refuses an existing nonempty target; exclusive mkdir
        # reserves the requested destination even on platforms replacing empty dirs.
        output.mkdir()
        try:
            for item in temporary.iterdir():
                item.rename(output / item.name)
        except BaseException:
            plain_path(output.parent, output)
            shutil.rmtree(output)
            raise
        return result
    finally:
        plain_path(temporary.parent, temporary)
        shutil.rmtree(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    build = actions.add_parser("build", help="create a new fixed release directory")
    build.add_argument("--repo", required=True)
    build.add_argument("--output", required=True)
    build.add_argument("--allow-dirty", action="store_true", help="explicitly build an uncommitted development candidate")
    verify = actions.add_parser("verify", help="verify all package bytes without executing them")
    verify.add_argument("--package", required=True)
    args = parser.parse_args(argv)
    try:
        result = build_package(args.repo, args.output, allow_dirty=args.allow_dirty) if args.action == "build" else verify_package(args.package)
    except (ValueError, OSError) as exc:
        print(json.dumps({"Valid": False, "Error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
