"""Short-lived B1 environment bridge for the existing agent runtime and independent QA.

Only prepared ValidationIds are executable. No role dispatch, candidate generation,
gate decisions or quality facts are performed by this resource helper.
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import threading
import uuid

sys.dont_write_bytecode = True
import governance_local_tests as local


class SessionExpired(RuntimeError):
    pass


def directory(repo, session_id):
    local.g.require(re.fullmatch(r"[0-9a-f]{32}", session_id), "session ID")
    root = local.runtime_root(repo)
    return local.g.safe_path(root, root / "local-tests/sessions" / session_id)


def publish(path, value):
    local.g.safe_path(path.parent, path)
    local.g.sensitive(value)
    # A second full request filename plus UUID can cross Windows MAX_PATH.
    temporary = path.with_name(uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as target:
        json.dump(value, target, ensure_ascii=False, sort_keys=True)
    os.replace(temporary, path)


def read(path):
    local.g.safe_path(path.parent, path)
    local.g.require(path.stat().st_size <= 4 * 1024 * 1024, "session observation size")
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=local.g.unique_object)
    local.g.require(isinstance(value, dict), "session observation object")
    local.g.sensitive(value)
    return value


def stable(repo, before, requirement):
    after = local.source_state(repo)
    allowed = tuple("docs/requirements/" + requirement + "/facts/" + kind + "/" for kind in ("readiness", "qa"))
    return (all(after.get(name) == value for name, value in before.items())
            and all(name.endswith(".json") and name.startswith(allowed) for name in set(after) - set(before)))


def serve(repo, requirement, session_id, seconds):
    folder = directory(repo, session_id)
    local.g.require(type(seconds) is int and 1 <= seconds <= 1800, "session lifetime")
    request = local.project(repo, requirement, "QA")
    local.g.require(request["Config"].get("Automatic") is True, "project automatic B1 adoption")
    before = local.source_state(repo)

    def dispatch(ready, child_env, observations):
        methods = {item["ValidationId"]: item for item in ready}
        publish(folder / "ready.json", {"SessionId": session_id, "ReadyValidationIds": list(methods),
                                       "Observations": observations})
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not (folder / "stop.json").exists():
            tracking = repo / "docs/requirements" / requirement / "tracking.json"
            if tracking.exists():
                from governance_requirements import execution_state
                if execution_state(repo, requirement)["Mode"] == "MANUAL":
                    raise SessionExpired("Developer took over this requirement")
            for path in sorted(folder.glob("*.request.json")):
                response = path.with_name(path.name.replace(".request.json", ".response.json"))
                if response.exists():
                    continue
                value = local.read_json(local.g.safe_path(folder, path))
                local.g.exact_object(value, "session test request", ("ValidationId",))
                ident = value["ValidationId"]
                if ident not in methods:
                    publish(response, {"Status": "VALIDATION_NOT_READY", "QualityPassed": False})
                    continue
                item = methods[ident]
                ready_now = stable(repo, before, requirement)
                checked = set()
                def check(resource_id):
                    if resource_id in checked:
                        return True
                    checked.add(resource_id)
                    spec = request["Config"]["Resources"][resource_id]
                    if not all(check(dependency) for dependency in spec["Requires"]):
                        return False
                    command = [local.executable(spec["Check"][0]), *spec["Check"][1:]]
                    return command[0] is not None and local.run_command(command, repo, child_env, min(2, spec["TimeoutSeconds"]))["Status"] == "SUCCEEDED"
                ready_now = ready_now and all(check(resource_id) for resource_id in item["Requires"]) and stable(repo, before, requirement)
                if ready_now:
                    spec = request["Config"]["Commands"][item["Method"]]
                    program = local.executable(spec["Argv"][0])
                    observation = (local.run_command([program, *spec["Argv"][1:]], repo, child_env, spec["TimeoutSeconds"])
                                   if program is not None else {"Status": "MISSING_TOOL", "ExitCode": None})
                else:
                    observation = {"Status": "ENVIRONMENT_CHANGED", "ExitCode": None}
                publish(response, {"ValidationId": ident, "Observation": observation, "QualityPassed": False,
                                   "RepositoryUnchanged": stable(repo, before, requirement)})
            time.sleep(0.1)
        if not (folder / "stop.json").exists():
            raise SessionExpired("Local test environment session expired")

    try:
        report = local.execute(request, dispatch=dispatch)
    except Exception:
        ready_path = folder / "ready.json"
        state = read(ready_path) if ready_path.exists() else {}
        run_id = state.get("Observations", {}).get("RunId")
        result = {"Status": "SESSION_ENDED", "Cleanup": "UNCONFIRMED"}
        if isinstance(run_id, str) and re.fullmatch(r"[0-9a-f]{32}", run_id):
            path = folder.parent.parent / (run_id + ".json")
            if path.exists():
                result["Cleanup"] = read(path).get("Cleanup", "UNCONFIRMED")
        publish(folder / "error.json", result)
        raise
    publish(folder / "finished.json", report)
    if not (folder / "ready.json").exists():
        publish(folder / "ready.json", {"SessionId": session_id, "ReadyValidationIds": [], "Observations": report})


def start(repo, requirement, seconds=600):
    local.g.require(type(seconds) is int and 1 <= seconds <= 1800, "session lifetime")
    # Validate all input before launching a background helper.
    request = local.project(repo, requirement, "QA")
    local.g.require(request["Config"].get("Automatic") is True, "project automatic B1 adoption")
    ident = uuid.uuid4().hex
    folder = directory(repo, ident)
    folder.mkdir(parents=True, exist_ok=False)
    command = [sys.executable, str(Path(__file__).resolve()), "_serve", "--repo", str(repo),
               "--requirement-id", requirement, "--session-id", ident, "--seconds", str(seconds)]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                               start_new_session=os.name != "nt", close_fds=True)
    threading.Thread(target=process.wait, daemon=True).start()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if (folder / "ready.json").exists():
            return read(folder / "ready.json")
        if process.poll() is not None:
            raise OSError("Local environment helper could not start")
        time.sleep(0.1)
    publish(folder / "stop.json", {})
    raise TimeoutError("Local environment preparation timed out")


def test(repo, session_id, validation_id):
    folder = directory(repo, session_id)
    local.g.require(not (folder / "finished.json").exists(), "live local test session")
    ready = read(folder / "ready.json")
    local.g.require(validation_id in ready["ReadyValidationIds"], "prepared ValidationId")
    correlation = uuid.uuid4().hex
    request_path = folder / (correlation + ".request.json")
    response_path = folder / (correlation + ".response.json")
    publish(request_path, {"ValidationId": validation_id})
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        if response_path.exists():
            return read(response_path)
        if (folder / "finished.json").exists() or (folder / "error.json").exists():
            raise OSError("Local environment session ended")
        time.sleep(0.1)
    raise TimeoutError("Local test request timed out")


def stop(repo, session_id):
    folder = directory(repo, session_id)
    publish(folder / "stop.json", {})
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if (folder / "finished.json").exists():
            return read(folder / "finished.json")
        if (folder / "error.json").exists():
            return read(folder / "error.json")
        time.sleep(0.1)
    raise TimeoutError("Local environment cleanup is not yet confirmed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.error = lambda _message: parser.exit(2, "Invalid local session arguments.\n")
    parser.add_argument("action", choices=("start", "test", "stop", "_serve"))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--requirement-id")
    parser.add_argument("--session-id")
    parser.add_argument("--validation-id")
    parser.add_argument("--seconds", type=int, default=600)
    args = parser.parse_args()
    try:
        if args.action == "start":
            result = start(args.repo, args.requirement_id, args.seconds)
        elif args.action == "test":
            result = test(args.repo, args.session_id, args.validation_id)
        elif args.action == "stop":
            result = stop(args.repo, args.session_id)
        else:
            try:
                serve(Path(args.repo), args.requirement_id, args.session_id, args.seconds)
            except Exception:
                path = directory(args.repo, args.session_id) / "error.json"
                if not path.exists():
                    publish(path, {"Status": "SESSION_ENDED", "Cleanup": "UNCONFIRMED"})
                raise
            return 0
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if args.action == "start":
            return 0 if result["ReadyValidationIds"] else 1
        if args.action == "test":
            return 0 if result.get("Observation", {}).get("Status") == "SUCCEEDED" and result.get("RepositoryUnchanged") else 1
        return 0 if result.get("Cleanup") == "COMPLETED" else 1
    except (local.g.Invalid, OSError, ValueError, TypeError, TimeoutError):
        print("Unable to use local test environment session; inspect its bound inputs and local diagnostics.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
