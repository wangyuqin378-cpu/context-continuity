from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_checkpoint_guard import checkpoint


SKILL_ROOT = Path(__file__).parents[1]
CONTEXTCTL = SKILL_ROOT / "scripts" / "contextctl.py"
GUARD = SKILL_ROOT / "scripts" / "checkpoint_guard.py"
SOURCE_SCHEMA = "context-continuity/initial-source/v1"
NEXT_ACTION = "- W001 — Run the checkpoint guard once."
VERIFICATION = (
    "- E001 — The guard exits successfully and reports the expected sequence."
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def counts(text: str) -> dict[str, int]:
    return {
        "words": len(re.findall(r"\S+", text)),
        "utf8_bytes": len(text.encode("utf-8")),
    }


def compression_basis_points(source: int, candidate: int) -> int:
    return ((source - candidate) * 10000) // source


class ContextctlInitialSourceGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.task = self.make_task("source-gate-task")
        evidence = self.workspace / "evidence"
        evidence.mkdir(parents=True)
        (evidence / "init.log").write_text("verified\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_task(self, task_id: str) -> Path:
        task = self.workspace / ".continuity" / task_id
        task.mkdir(parents=True)
        return task

    def make_initial_candidate(self, task: Path | None = None) -> Path:
        task = task or self.task
        candidate = task / "candidate.md"
        candidate.write_text(
            checkpoint(task.name, 1, "none", "init"), encoding="utf-8"
        )
        return candidate

    def write_source(self, name: str, text: str) -> Path:
        source = self.workspace / name
        source.write_text(text, encoding="utf-8")
        return source

    def run_contextctl(
        self, *args: object, ok: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(CONTEXTCTL), *(str(arg) for arg in args)],
            text=True,
            capture_output=True,
            check=False,
        )
        if ok and result.returncode != 0:
            self.fail(result.stderr)
        if not ok and result.returncode == 0:
            self.fail(result.stdout)
        return result

    def run_guard(self, *args: object) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(GUARD), *(str(arg) for arg in args)],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            self.fail(result.stderr)
        return result

    def review_init(
        self, candidate: Path, source: Path | None = None, *, ok: bool = True
    ) -> tuple[Path, subprocess.CompletedProcess[str]]:
        manifest = candidate.parent / "review.json"
        args: list[object] = [
            "review-init",
            candidate,
            "--output",
            manifest,
        ]
        if source is not None:
            args.extend(["--source", source])
        result = self.run_contextctl(*args, ok=ok)
        return manifest, result

    def complete_review(
        self, manifest: Path, payload: dict[str, object]
    ) -> None:
        required_ids = list(payload["required_ids"])
        payload.update(
            {
                "reviewer": "fresh-agent-source-gate",
                "reviewed_at": "2026-07-11T16:00:00+08:00",
                "recovered_ids": required_ids,
                "missing_ids": [],
                "contradictions": [],
                "ambiguities": [],
                "recovered_next_action": NEXT_ACTION,
                "recovered_verification": VERIFICATION,
                "verdict": "PASS",
            }
        )
        evidence_health = payload["evidence_health"]
        self.assertIsInstance(evidence_health, list)
        for record in evidence_health:
            self.assertIsInstance(record, dict)
            record["status"] = record["detected_status"]
            record["semantic_status"] = "DIRECT"
        source_review = payload.get("source_review")
        if isinstance(source_review, dict) and source_review.get("coverage_status") != "NOT_APPLICABLE":
            required_source_ids = list(source_review["required_ids"])
            source_review.update(
                {
                    "recovered_ids": required_source_ids,
                    "missing_ids": [],
                    "coverage_status": "COMPLETE",
                    "omissions": [],
                }
            )
        manifest.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def assert_no_write_debris(self, task: Path) -> None:
        self.assertFalse((task / ".checkpoint-write.lock").exists())
        self.assertEqual([], list(task.rglob(".publishing-*")))

    def test_large_source_pass_binds_path_hash_counts_and_integer_compression(
        self,
    ) -> None:
        candidate = self.make_initial_candidate()
        source = self.write_source("large-source.md", "sourceword " * 2000)

        manifest, _ = self.review_init(candidate, source)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        binding = payload["initial_source"]

        self.assertIsInstance(binding, dict)
        self.assertEqual(SOURCE_SCHEMA, binding["schema"])
        self.assertEqual(str(source.resolve()), binding["canonical_path"])
        self.assertEqual(digest(source), binding["sha256"])
        source_counts = counts(source.read_text(encoding="utf-8"))
        candidate_counts = counts(candidate.read_text(encoding="utf-8"))
        self.assertEqual(source_counts, binding["source_counts"])
        self.assertEqual(candidate_counts, binding["candidate_counts"])
        expected_compression = {
            key: compression_basis_points(source_counts[key], candidate_counts[key])
            for key in ("words", "utf8_bytes")
        }
        self.assertEqual(expected_compression, binding["compression_basis_points"])
        self.assertTrue(binding["gate_required"])
        self.assertTrue(binding["gate_passed"])
        for value in binding["compression_basis_points"].values():
            self.assertIsInstance(value, int)
            self.assertGreaterEqual(value, 3000)

        self.complete_review(manifest, payload)
        checked = self.run_contextctl("review-check", candidate, manifest)
        self.assertIn("PASS", checked.stdout)

    def test_large_source_gate_requires_thirty_percent_in_both_metrics(self) -> None:
        cases = {
            "many-short-words": "x " * 600,
            "many-bytes-one-word": "界" * 5000,
        }
        for index, (label, source_text) in enumerate(cases.items(), start=1):
            with self.subTest(label=label):
                task = self.make_task(f"and-gate-{index}")
                candidate = self.make_initial_candidate(task)
                before = candidate.read_bytes()
                source = self.write_source(f"{label}.md", source_text)

                manifest, failure = self.review_init(candidate, source, ok=False)

                self.assertRegex(failure.stdout + failure.stderr, r"30%|3000|compression")
                self.assertEqual(before, candidate.read_bytes())
                self.assertFalse(manifest.exists())
                self.assertFalse((task / "checkpoints").exists())
                self.assert_no_write_debris(task)

    def test_review_check_recomputes_source_and_rejects_source_edit(self) -> None:
        candidate = self.make_initial_candidate()
        source = self.write_source("editable-source.md", "sourceword " * 2000)
        manifest, _ = self.review_init(candidate, source)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        self.complete_review(manifest, payload)
        candidate_before = candidate.read_bytes()
        source.write_text(source.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")

        failure = self.run_contextctl(
            "review-check", candidate, manifest, ok=False
        )

        self.assertRegex(failure.stdout + failure.stderr, r"source|SHA|binding|changed")
        self.assertEqual(candidate_before, candidate.read_bytes())
        self.assertFalse((self.task / "checkpoints").exists())
        self.assert_no_write_debris(self.task)

    def test_review_check_rejects_each_tampered_source_binding_field(self) -> None:
        candidate = self.make_initial_candidate()
        source = self.write_source("bound-source.md", "sourceword " * 2000)
        manifest, _ = self.review_init(candidate, source)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        self.complete_review(manifest, payload)
        original = manifest.read_text(encoding="utf-8")
        candidate_before = candidate.read_bytes()

        tamper_cases = (
            ("canonical_path", lambda item: item.__setitem__("canonical_path", str(self.workspace / "other.md"))),
            ("sha256", lambda item: item.__setitem__("sha256", "0" * 64)),
            ("source_counts", lambda item: item["source_counts"].__setitem__("words", item["source_counts"]["words"] + 1)),
            ("candidate_counts", lambda item: item["candidate_counts"].__setitem__("utf8_bytes", item["candidate_counts"]["utf8_bytes"] + 1)),
            ("compression", lambda item: item["compression_basis_points"].__setitem__("words", item["compression_basis_points"]["words"] + 1)),
            ("gate_required", lambda item: item.__setitem__("gate_required", False)),
            ("gate_passed", lambda item: item.__setitem__("gate_passed", False)),
        )
        for label, mutate in tamper_cases:
            with self.subTest(field=label):
                altered = json.loads(original)
                binding = altered["initial_source"]
                self.assertIsInstance(binding, dict)
                mutate(binding)
                manifest.write_text(
                    json.dumps(altered, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8",
                )

                failure = self.run_contextctl(
                    "review-check", candidate, manifest, ok=False
                )

                self.assertRegex(
                    failure.stdout + failure.stderr,
                    r"source|binding|compression|review",
                )
                self.assertEqual(candidate_before, candidate.read_bytes())
                self.assertFalse((self.task / "checkpoints").exists())
                self.assert_no_write_debris(self.task)

    def test_non_initial_candidate_rejects_source_without_changing_old_chain(
        self,
    ) -> None:
        initial = self.make_initial_candidate()
        self.run_guard("prepare", initial)
        self.run_guard("promote", initial)
        latest = self.task / "checkpoints" / "0001-init.md"
        latest_before = latest.read_bytes()
        self.run_contextctl("draft", self.task, "--trigger", "phase-verified")
        candidate = self.task / "candidate.md"
        candidate_before = candidate.read_bytes()
        source = self.write_source("non-initial-source.md", "sourceword " * 2000)

        manifest, failure = self.review_init(candidate, source, ok=False)

        self.assertRegex(failure.stdout + failure.stderr, r"initial|seq(?:uence)? 1")
        self.assertEqual(candidate_before, candidate.read_bytes())
        self.assertEqual(latest_before, latest.read_bytes())
        self.assertEqual(1, len(list((self.task / "checkpoints").glob("*.md"))))
        self.assertFalse(manifest.exists())
        self.assert_no_write_debris(self.task)

    def test_small_source_is_bound_without_enforcing_thirty_percent(self) -> None:
        candidate = self.make_initial_candidate()
        source = self.write_source("small-source.md", "tiny source\n")

        manifest, _ = self.review_init(candidate, source)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        binding = payload["initial_source"]

        self.assertEqual(str(source.resolve()), binding["canonical_path"])
        self.assertEqual(digest(source), binding["sha256"])
        self.assertFalse(binding["gate_required"])
        self.assertTrue(binding["gate_passed"])
        self.assertLess(binding["compression_basis_points"]["words"], 3000)
        self.assertLess(binding["compression_basis_points"]["utf8_bytes"], 3000)

    def test_review_without_source_requires_explicit_provenance(self) -> None:
        candidate = self.make_initial_candidate()

        manifest = candidate.parent / "review.json"
        self.run_contextctl(
            "review-init",
            candidate,
            "--output",
            manifest,
            "--no-source-reason",
            "initial contract entered directly",
        )
        payload = json.loads(manifest.read_text(encoding="utf-8"))

        self.assertIn("initial_source", payload)
        self.assertFalse(payload["initial_source"]["available"])
        self.assertEqual(
            "initial contract entered directly", payload["initial_source"]["reason"]
        )

    def test_low_compression_failure_has_stable_safe_actionable_envelope(self) -> None:
        candidate = self.make_initial_candidate()
        before = candidate.read_bytes()
        source = self.write_source("low-compression-source.md", "x " * 600)

        manifest, failure = self.review_init(candidate, source, ok=False)
        output = failure.stdout + failure.stderr

        self.assertIn("FAIL CTX302 REVIEW-INIT_FAILED:", output)
        self.assertIn("SAFETY: UNCHANGED", output)
        self.assertRegex(output, r"NEXT: .*(?:compress|compression|reduce).*review-init")
        self.assertEqual(before, candidate.read_bytes())
        self.assertFalse(manifest.exists())
        self.assertFalse((self.task / "checkpoints").exists())
        self.assert_no_write_debris(self.task)


if __name__ == "__main__":
    unittest.main()
