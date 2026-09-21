"""Local-only developer setup and concurrent ID allocation; all writes use temp repos."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("governance_tools", Path(__file__).parents[1] / "governance_tools.py")
g = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(g)


class RequirementIdTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True).stdout.strip()

    def test_formats_and_calendar(self):
        for value in ("REQ-20260916-002", "REQ-DEV-20260917-01", "REQ-DEV.ONE-20260917-101"):
            self.assertTrue(g.valid_requirement_id(value), value)
        for value in ("REQ-ldh-20260917-01", "REQ-DEV-20260230-01", "REQ-DEV-20260917-00", "REQ-../X-20260917-01", "REQ-DEV-20260917-1", "REQ-DEV..ONE-20260917-01"):
            self.assertFalse(g.valid_requirement_id(value), value)
        for value in ("../bad", "bad/name", "中文", "-start", "end.", "a" * 33):
            with self.assertRaises(g.Invalid):
                g.init_developer(self.repo, value)

    def test_missing_identity_does_not_allocate_or_infer_git_author(self):
        self.git("config", "user.name", "Unrelated Author")
        with self.assertRaisesRegex(g.Invalid, "init-developer"):
            g.reserve_requirement(self.repo)
        self.assertFalse((self.repo / ".git/governance-id-reservations").exists())

    def test_initialization_is_local_idempotent_and_preserves_author(self):
        self.git("config", "user.name", "Original Author")
        self.assertEqual(g.init_developer(self.repo, "dev")["DeveloperId"], "DEV")
        g.init_developer(self.repo, "DEV")
        with self.assertRaises(g.Invalid):
            g.init_developer(self.repo, "OTHER")
        self.assertEqual(self.git("config", "user.name"), "Original Author")
        self.assertEqual(self.git("config", "--local", "--get", "governance.developerId"), "DEV")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_existing_names_and_abandoned_reservations_are_not_reused(self):
        g.init_developer(self.repo, "DEV")
        first = g.reserve_requirement(self.repo)["RequirementId"]
        prefix = first.rsplit("-", 1)[0]
        target = self.repo / f"docs/requirements/{prefix}-02"
        target.mkdir(parents=True)
        self.assertEqual(g.reserve_requirement(self.repo)["RequirementId"], prefix + "-03")
        self.assertEqual(g.reserve_requirement(self.repo)["RequirementId"], prefix + "-04")
        self.assertEqual(list(target.iterdir()), [])

    def test_parallel_worktrees_share_config_and_atomic_reservations(self):
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "fixture")
        linked = Path(self.temp.name) / "linked worktree"
        self.git("worktree", "add", "--detach", str(linked))
        g.init_developer(self.repo, "DEV")
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(g.reserve_requirement, [self.repo, linked] * 6))
        ids = [item["RequirementId"] for item in results]
        self.assertEqual(len(set(ids)), 12)
        self.assertEqual(sorted(int(value.rsplit("-", 1)[1]) for value in ids), list(range(1, 13)))
        self.assertTrue(all(not item["GlobalUniqueness"] for item in results))
        self.assertFalse((linked / "docs/requirements").exists())
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_new_id_is_accepted_by_all_contract_schemas(self):
        repo = Path(__file__).parents[2]
        for path in (repo / "docs/Agent治理/contracts").glob("*.schema.json"):
            schema = g.load_contract(path, "schema")["properties"]["RequirementId"]
            g.validate_schema_instance("REQ-DEV-20260917-01", schema)
            g.validate_schema_instance("REQ-20260916-002", schema)
            with self.assertRaises(g.Invalid):
                g.validate_schema_instance("REQ-../../bad", schema)


if __name__ == "__main__":
    unittest.main()
