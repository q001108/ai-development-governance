"""Byte-level scoped snapshots in temporary repositories, never business candidates."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import governance_candidates as candidates
sys.path.pop(0)


class CandidateSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.run_git("init", "-q")
        self.run_git("config", "user.name", "Snapshot Fixture")
        self.run_git("config", "user.email", "snapshot@test.invalid")
        self.run_git("config", "core.autocrlf", "false")
        self.write("src/text.txt", b"original\r\n")
        self.write("src/中文.bin", bytes(range(256)))
        self.write("src/old.txt", b"unchanged rename body\n")
        self.write("src/delete.txt", b"delete me\n")
        self.write("unrelated.txt", b"unrelated original")
        self.run_git("add", ".")
        self.run_git("commit", "-qm", "synthetic baseline")
        self.baseline = self.run_git("rev-parse", "HEAD").decode("ascii").strip()
        self.requirement = "REQ-TEST-20261028-01"
        self.handoff = f"docs/requirements/{self.requirement}/facts/writers/WR-001.json"

    def run_git(self, *args):
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        return subprocess.run(["git", "-C", str(self.repo), *args], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=True, env=env).stdout

    def write(self, name, data):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def facts(self, changes):
        value = {"SchemaVersion": "v1.1", "RequirementId": self.requirement, "ContextPackVersion": "v1.0",
                 "RepairLineageRef": None, "EventId": "WR-001", "EventType": "WRITER_HANDOFF",
                 "CreatedAt": "2026-10-28T01:00:00+08:00", "Actor": "Synthetic Writer", "CandidateRef": None,
                 "Payload": {"Status": "COMPLETED", "Changes": [{"Path": name, "Operation": operation} for name, operation in changes]},
                 "EvidenceRefs": [], "SupersedesEventRef": None, "RedactionState": "SAFE"}
        self.write(self.handoff, json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def capture(self, paths, **kwargs):
        return candidates.capture_snapshot(self.repo, requirement_id=self.requirement, context_version="v1.0", repair_lineage_ref=None,
                                           baseline_revision=self.baseline, writer_handoff_refs=[self.handoff], scope_paths=paths, **kwargs)

    def test_raw_bytes_preserve_staged_unstaged_binary_and_chinese_paths(self):
        self.write("src/text.txt", b"staged\n")
        self.run_git("add", "src/text.txt")
        final = b"unstaged\r\nsecond\n"
        self.write("src/text.txt", final)
        binary = b"\x00\xff\x80updated\x00"
        self.write("src/中文.bin", binary)
        self.facts([("src/text.txt", "MODIFY"), ("src/中文.bin", "MODIFY")])
        report = self.capture(["src/text.txt", "src/中文.bin"])
        root = Path(report["CandidateSnapshotRef"]).parent
        data = json.loads((root / "snapshot.json").read_text(encoding="utf-8"))
        text = next(item for item in data["Files"] if item["Path"] == "src/text.txt")
        self.assertEqual((root / "blobs" / text["Current"]["SHA256"]).read_bytes(), final)
        self.assertEqual(text["Index"]["BlobId"], self.run_git("rev-parse", ":src/text.txt").decode("ascii").strip())
        self.assertTrue(report["Valid"])
        self.assertFalse(report["QualityPassed"])
        self.assertFalse(report["AuthorshipVerified"])
        draft = report["CandidateFactDraft"]
        self.assertIsNone(draft["CandidateRef"])
        self.assertFalse(draft["Payload"]["LocalOnly"])
        self.assertTrue(draft["Payload"]["CandidateSnapshotRef"].startswith("docs/requirements/"))

    def test_same_content_is_idempotent_without_overwriting_existing_files(self):
        self.write("src/text.txt", b"new bytes\n")
        self.facts([("src/text.txt", "MODIFY")])
        first = self.capture(["src/text.txt"])
        root = Path(first["CandidateSnapshotRef"]).parent
        before = {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns) for path in root.rglob("*") if path.is_file()}
        second = self.capture(["src/text.txt"])
        self.assertEqual(first["SnapshotDigest"], second["SnapshotDigest"])
        self.assertEqual(before, {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns) for path in root.rglob("*") if path.is_file()})

    def test_rename_delete_and_declared_untracked_are_all_bound(self):
        self.run_git("mv", "src/old.txt", "src/new.txt")
        (self.repo / "src/delete.txt").unlink()
        self.write("src/untracked.txt", b"declared new\n")
        self.facts([("src/new.txt", "RENAME"), ("src/delete.txt", "DELETE"), ("src/untracked.txt", "ADD")])
        scope = ["src/old.txt", "src/new.txt", "src/delete.txt", "src/untracked.txt"]
        report = self.capture(scope, untracked_paths=["src/untracked.txt"])
        self.assertEqual(set(report["ChangedPaths"]), set(scope))
        self.assertTrue(candidates.verify_snapshot(report["CandidateSnapshotRef"], repo=self.repo, verify_worktree=True)["WorktreeVerified"])
        manifest = json.loads(Path(report["CandidateSnapshotRef"]).read_text(encoding="utf-8"))
        old = next(item for item in manifest["Files"] if item["Path"] == "src/old.txt")
        self.assertIsNone(old["Current"])

    def test_scope_cannot_omit_writer_changes_or_silently_include_other_user_changes(self):
        self.write("src/text.txt", b"new text")
        self.write("src/delete.txt", b"second change")
        self.facts([("src/text.txt", "MODIFY"), ("src/delete.txt", "MODIFY")])
        with self.assertRaises(candidates.g.Invalid):
            self.capture(["src/text.txt"])
        with self.assertRaises(candidates.g.Invalid):
            self.capture(["src/text.txt", "src/delete.txt", "unrelated.txt"])

    def test_outside_scope_private_modifications_are_not_read_or_delivered(self):
        self.write("src/text.txt", b"new text")
        self.write("unrelated.txt", b"password=synthetic-private-value")
        self.facts([("src/text.txt", "MODIFY")])
        original = Path.read_bytes
        def allowed_read(path):
            if path == self.repo / "unrelated.txt":
                raise AssertionError("other user's file must not be read")
            return original(path)
        with mock.patch.object(Path, "read_bytes", allowed_read):
            report = self.capture(["src/text.txt"])
        self.assertEqual(report["ChangedPaths"], ["src/text.txt"])
        self.assertNotIn("synthetic-private-value", json.dumps(report))

    def test_undeclared_ignored_and_sensitive_files_are_rejected(self):
        self.write("src/new.txt", b"new")
        self.facts([("src/new.txt", "ADD")])
        with self.assertRaises(candidates.g.Invalid):
            self.capture(["src/new.txt"])
        self.write(".gitignore", b"src/new.txt\n")
        with self.assertRaises(candidates.g.Invalid):
            self.capture(["src/new.txt"], untracked_paths=["src/new.txt"])
        for name in (".env", "private.pem", "logs/app.txt", "node_modules/a.js"):
            with self.subTest(name=name):
                self.write(name, b"synthetic")
                with self.assertRaises(candidates.g.Invalid):
                    self.facts([(name, "ADD")])
                    self.capture([name], untracked_paths=[name])

    def test_credentials_in_selected_bytes_are_rejected_without_publishing(self):
        self.write("src/text.txt", b"password=synthetic-private-value")
        self.facts([("src/text.txt", "MODIFY")])
        with self.assertRaises(candidates.g.Invalid):
            self.capture(["src/text.txt"])
        self.assertFalse((self.repo / "docs/requirements" / self.requirement / "snapshots").exists())

    def test_utf16_credentials_and_unresolved_corrections_are_rejected(self):
        self.write("src/text.txt", "password=synthetic-private-value".encode("utf-16"))
        self.facts([("src/text.txt", "MODIFY")])
        with self.assertRaises(candidates.g.Invalid):
            self.capture(["src/text.txt"])
        self.write("src/text.txt", b"new text")
        self.write(f"docs/requirements/{self.requirement}/facts/corrections/COR-001.json", json.dumps({"EventType": "FACT_CORRECTION"}).encode())
        with self.assertRaises(candidates.g.Invalid):
            self.capture(["src/text.txt"])

    def test_utf16_and_utf32_credential_views_do_not_change_safe_original_bytes(self):
        unsafe = '{"password":"synthetic-private-value"}'
        for encoding in ("utf-8", "utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"):
            with self.subTest(encoding=encoding), self.assertRaises(candidates.g.Invalid):
                candidates.safe_content(unsafe.encode(encoding))
        for data in (b"\xfe\xff" + unsafe.encode("utf-16-be"), b"\x00\x00\xfe\xff" + unsafe.encode("utf-32-be"),
                     b"x" + unsafe.encode("utf-16-le"), unsafe.encode("utf-32-be") + b"x"):
            with self.subTest(length=len(data)), self.assertRaises(candidates.g.Invalid):
                candidates.safe_content(data)
        safe = "安全测试数据：fixture=example\r\n"
        for encoding in ("utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"):
            data = safe.encode(encoding)
            with self.subTest(safe_encoding=encoding):
                self.assertIs(candidates.safe_content(data), data)
        self.write("src/text.txt", safe.encode("utf-16"))
        self.facts([("src/text.txt", "MODIFY")])
        report = self.capture(["src/text.txt"])
        root = Path(report["CandidateSnapshotRef"]).parent
        self.assertEqual((root / "blobs" / candidates.sha256(safe.encode("utf-16"))).read_bytes(), safe.encode("utf-16"))

    def test_scope_lineage_handoff_status_and_expected_bytes_are_checked(self):
        self.write("src/text.txt", b"new text")
        self.facts([("src/text.txt", "MODIFY")])
        with self.assertRaises(candidates.g.Invalid):
            self.capture(["src/text.txt"], expected_files={"src/text.txt": "0" * 64})
        report = self.capture(["src/text.txt"], expected_files={"src/text.txt": candidates.sha256(b"new text")})
        self.assertTrue(report["ExpectedFileDigestsChecked"])
        for kwargs in ({"requirement_id": "REQ-OTHER-20261028-01"}, {"context_version": "v2.0"}, {"repair_lineage_ref": "OTHER"}, {"expected_snapshot_digest": "0" * 64}):
            with self.subTest(kwargs=kwargs), self.assertRaises(candidates.g.Invalid):
                candidates.verify_snapshot(report["CandidateSnapshotRef"], **kwargs)
        handoff = self.repo / self.handoff
        value = json.loads(handoff.read_text(encoding="utf-8"))
        value["Payload"]["Status"] = "BLOCKED"
        handoff.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(candidates.g.Invalid):
            self.capture(["src/text.txt"])

    def test_governance_sources_are_byte_bound_and_empty_sources_remain_unverified(self):
        self.write("src/text.txt", b"new text")
        self.facts([("src/text.txt", "MODIFY")])
        empty = self.capture(["src/text.txt"])
        self.assertFalse(empty["GovernanceInputsComplete"])
        self.assertFalse(empty["GovernanceInputsChecked"])
        base = f"docs/requirements/{self.requirement}/"
        for name in candidates.GOVERNANCE_FILES[:3]:
            self.write(base + name, b"synthetic frozen source\r\n")
        report = self.capture(["src/text.txt"])
        verified = candidates.verify_snapshot(report["CandidateSnapshotRef"], repo=self.repo)
        self.assertTrue(verified["GovernanceInputsComplete"])
        self.assertTrue(verified["GovernanceInputsChecked"])
        self.write(base + "开发上下文包.md", b"same version changed source\n")
        with self.assertRaises(candidates.g.Invalid):
            candidates.verify_snapshot(report["CandidateSnapshotRef"], repo=self.repo)
        self.write(base + "开发上下文包.md", b"synthetic frozen source\r\n")
        self.write(base + "rules-projection.json", b"{}")
        with self.assertRaises(candidates.g.Invalid):
            candidates.verify_snapshot(report["CandidateSnapshotRef"], repo=self.repo)

    def test_blob_tampering_refuses_verification_and_idempotent_recapture(self):
        self.write("src/text.txt", b"new text")
        self.facts([("src/text.txt", "MODIFY")])
        report = self.capture(["src/text.txt"])
        root = Path(report["CandidateSnapshotRef"]).parent
        blob = root / "blobs" / candidates.sha256(b"new text")
        blob.write_bytes(b"tampered")
        with self.assertRaises(candidates.g.Invalid):
            candidates.verify_snapshot(root)
        with self.assertRaises(candidates.g.Invalid):
            self.capture(["src/text.txt"])
        self.assertEqual(blob.read_bytes(), b"tampered")

    def test_plugin_binding_changes_invalidate_a_captured_candidate(self):
        self.write("src/text.txt", b"new text")
        self.facts([("src/text.txt", "MODIFY")])
        binding = f"docs/requirements/{self.requirement}/plugin-binding.json"
        self.write(binding, b'{"PackageDigest":"original-fixed-release"}')
        report = self.capture(["src/text.txt"])
        self.assertTrue(candidates.verify_snapshot(report["CandidateSnapshotRef"], repo=self.repo)["Valid"])
        self.write(binding, b'{"PackageDigest":"another-release"}')
        with self.assertRaises(candidates.g.Invalid):
            candidates.verify_snapshot(report["CandidateSnapshotRef"], repo=self.repo)

    def test_recomputed_manifest_cannot_hide_a_writer_delivery_when_sources_are_verified(self):
        self.write("src/text.txt", b"new text")
        self.write("src/delete.txt", b"second delivery")
        self.facts([("src/text.txt", "MODIFY"), ("src/delete.txt", "MODIFY")])
        report = self.capture(["src/text.txt", "src/delete.txt"])
        original = Path(report["CandidateSnapshotRef"]).parent
        manifest = json.loads((original / "snapshot.json").read_text(encoding="utf-8"))
        manifest["ScopePaths"] = ["src/text.txt"]
        manifest["Files"] = [item for item in manifest["Files"] if item["Path"] == "src/text.txt"]
        serialized = candidates.canonical(manifest)
        altered = original.parent / candidates.sha256(serialized)
        (altered / "blobs").mkdir(parents=True)
        (altered / "snapshot.json").write_bytes(serialized)
        for label in ("Baseline", "Current"):
            name = manifest["Files"][0][label]["SHA256"]
            (altered / "blobs" / name).write_bytes((original / "blobs" / name).read_bytes())
        with self.assertRaises(candidates.g.Invalid):
            candidates.verify_snapshot(altered, repo=self.repo)
        with self.assertRaises(candidates.g.Invalid):
            candidates.verify_snapshot(altered, expected_snapshot_digest=report["SnapshotDigest"])

    def test_worktree_verification_only_checks_delivery_and_tolerates_later_facts(self):
        self.write("src/text.txt", b"new text")
        self.facts([("src/text.txt", "MODIFY")])
        report = self.capture(["src/text.txt"])
        self.write(f"docs/requirements/{self.requirement}/facts/qa/QA-001.json", b"{}")
        self.assertTrue(candidates.verify_snapshot(report["CandidateSnapshotRef"], repo=self.repo, verify_worktree=True)["Valid"])
        self.write("src/text.txt", b"changed later")
        self.assertTrue(candidates.verify_snapshot(report["CandidateSnapshotRef"])["Valid"])
        with self.assertRaises(candidates.g.Invalid):
            candidates.verify_snapshot(report["CandidateSnapshotRef"], repo=self.repo, verify_worktree=True)

    def test_collection_change_is_detected_before_publication(self):
        self.write("src/text.txt", b"new text")
        self.facts([("src/text.txt", "MODIFY")])
        actual_collect = candidates.collect
        calls = 0
        def changing(*args):
            nonlocal calls
            result = actual_collect(*args)
            calls += 1
            if calls == 1:
                self.write("src/text.txt", b"changed during capture")
            return result
        with mock.patch.object(candidates, "collect", side_effect=changing), self.assertRaises(candidates.g.Invalid):
            self.capture(["src/text.txt"])
        self.assertFalse((self.repo / "docs/requirements" / self.requirement / "snapshots").exists())

    def test_git_environment_redirects_and_traces_are_not_inherited(self):
        self.write("src/text.txt", b"new text")
        self.facts([("src/text.txt", "MODIFY")])
        with mock.patch.dict(os.environ, {"GIT_DIR": str(self.repo / "missing"), "GIT_WORK_TREE": str(self.repo.parent), "GIT_TRACE": "synthetic-private-value"}):
            report = self.capture(["src/text.txt"])
        self.assertTrue(report["Valid"])
        self.assertNotIn("synthetic-private-value", json.dumps(report))

    def test_paths_symbolic_metadata_and_source_tree_output_are_rejected(self):
        self.write("src/text.txt", b"new text")
        self.facts([("src/text.txt", "MODIFY")])
        for name in ("../outside", "src/../../outside", "//host/share/file", "C:\\outside", "src/file:stream"):
            with self.subTest(name=name), self.assertRaises(candidates.g.Invalid):
                candidates.delivery_path(self.repo, name)
        target = self.repo / "src/text.txt"
        with mock.patch.object(Path, "is_symlink", lambda path: path == target), self.assertRaises(candidates.g.Invalid):
            self.capture(["src/text.txt"])
        with self.assertRaises(candidates.g.Invalid):
            self.capture(["src/text.txt"], output_root=self.repo / "unsafe-output")

    def test_external_snapshot_is_explicitly_local_and_not_archivable_as_a_fact(self):
        self.write("src/text.txt", b"new text")
        self.facts([("src/text.txt", "MODIFY")])
        report = self.capture(["src/text.txt"], output_root=Path(self.temp.name) / "external")
        self.assertTrue(report["CandidateFactDraft"]["Payload"]["LocalOnly"])
        self.assertTrue(report["CandidateFactDraft"]["Payload"]["CannotArchiveAsFact"])
        self.assertTrue(candidates.verify_snapshot(report["CandidateSnapshotRef"])["LocalOnly"])

    def test_cli_errors_do_not_echo_untrusted_values(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = candidates.main(["verify", "--snapshot", "password=synthetic-private-value"])
        self.assertEqual(code, 2)
        self.assertNotIn("synthetic-private-value", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
