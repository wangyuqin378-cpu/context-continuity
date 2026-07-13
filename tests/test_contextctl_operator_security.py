from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_checkpoint_guard import checkpoint


SKILL_ROOT = Path(__file__).parents[1]
CONTEXTCTL = SKILL_ROOT / "scripts" / "contextctl.py"
GUARD = SKILL_ROOT / "scripts" / "checkpoint_guard.py"


def section(text: str, heading: str) -> str:
    start = text.index(heading) + len(heading)
    remainder = text[start:].lstrip("\n")
    end = remainder.find("\n## ")
    return (remainder if end == -1 else remainder[:end]).strip()


class ContextctlOperatorSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        (self.workspace / "evidence").mkdir(parents=True)
        (self.workspace / "evidence" / "init.log").write_text(
            "checkpoint initialized\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_candidate(self, task_id: str) -> tuple[Path, Path]:
        task = self.workspace / ".continuity" / task_id
        task.mkdir(parents=True)
        candidate = task / "candidate.md"
        candidate.write_text(
            checkpoint(task_id, 1, "none", "init"), encoding="utf-8"
        )
        return task, candidate

    def run_contextctl(self, *args: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CONTEXTCTL), *(str(arg) for arg in args)],
            text=True,
            capture_output=True,
            check=False,
        )

    def run_guard(self, *args: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(GUARD), *(str(arg) for arg in args)],
            text=True,
            capture_output=True,
            check=False,
        )

    def complete_review(self, candidate: Path, review: Path) -> None:
        payload = json.loads(review.read_text(encoding="utf-8"))
        required_ids = list(payload["required_ids"])
        payload.update(
            {
                "reviewer": "operator-security-reviewer",
                "reviewed_at": "2026-07-12T03:00:00+08:00",
                "recovered_ids": required_ids,
                "missing_ids": [],
                "contradictions": [],
                "ambiguities": [],
                "recovered_next_action": section(
                    candidate.read_text(encoding="utf-8"), "## Next Action"
                ),
                "recovered_verification": section(
                    candidate.read_text(encoding="utf-8"),
                    "## Verification for Next Action",
                ),
                "verdict": "PASS",
            }
        )
        for record in payload["evidence_health"]:
            record["status"] = record["detected_status"]
            record["semantic_status"] = "DIRECT"
        review.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def test_initial_source_rejects_candidate_and_same_file_aliases(self) -> None:
        for alias_kind in ("direct", "symlink", "hardlink"):
            with self.subTest(alias=alias_kind):
                task, candidate = self.make_candidate(f"self-source-{alias_kind}")
                source = task / f"source-{alias_kind}.md"
                if alias_kind == "direct":
                    source = candidate
                elif alias_kind == "symlink":
                    source.symlink_to(candidate)
                else:
                    os.link(candidate, source)
                candidate_before = candidate.read_bytes()

                result = self.run_contextctl(
                    "review-init",
                    candidate,
                    "--output",
                    task / "review.json",
                    "--source",
                    source,
                )
                output = result.stdout + result.stderr

                self.assertNotEqual(0, result.returncode, output)
                self.assertRegex(output, r"(?i)source.+candidate|same-file")
                self.assertEqual(candidate_before, candidate.read_bytes())
                self.assertFalse((task / "review.json").exists())

    def test_prepare_rejects_symlinked_checkpoint_directory_without_change(self) -> None:
        task, candidate = self.make_candidate("prepare-checkpoints-link")
        outside = self.root / "outside-prepare"
        outside.mkdir()
        (task / "checkpoints").symlink_to(outside, target_is_directory=True)
        candidate_before = candidate.read_bytes()

        result = self.run_guard("prepare", candidate)

        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("symlink", (result.stdout + result.stderr).casefold())
        self.assertEqual(candidate_before, candidate.read_bytes())
        self.assertEqual([], list(outside.iterdir()))

    def test_promote_rejects_symlinked_checkpoint_directory_without_change(self) -> None:
        task, candidate = self.make_candidate("promote-checkpoints-link")
        prepared = self.run_guard("prepare", candidate)
        self.assertEqual(0, prepared.returncode, prepared.stdout + prepared.stderr)
        outside = self.root / "outside-promote"
        outside.mkdir()
        (task / "checkpoints").symlink_to(outside, target_is_directory=True)
        candidate_before = candidate.read_bytes()

        result = self.run_guard("promote", candidate)

        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("symlink", (result.stdout + result.stderr).casefold())
        self.assertEqual(candidate_before, candidate.read_bytes())
        self.assertEqual([], list(outside.iterdir()))
        self.assertFalse((task / ".checkpoint-write.lock").exists())

    def test_high_level_publish_rejects_symlinked_checkpoint_directory(self) -> None:
        task, candidate = self.make_candidate("publish-checkpoints-link")
        review = task / "review.json"
        initialized = self.run_contextctl(
            "review-init",
            candidate,
            "--output",
            review,
            "--no-source-reason",
            "The user request is the only initial source.",
        )
        self.assertEqual(0, initialized.returncode, initialized.stdout + initialized.stderr)
        self.complete_review(candidate, review)
        checked = self.run_contextctl("review-check", candidate, review)
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        outside = self.root / "outside-publish"
        outside.mkdir()
        (task / "checkpoints").symlink_to(outside, target_is_directory=True)
        candidate_before = candidate.read_bytes()
        review_before = review.read_bytes()

        result = self.run_contextctl("publish", candidate, "--review", review)

        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(candidate_before, candidate.read_bytes())
        self.assertEqual(review_before, review.read_bytes())
        self.assertEqual([], list(outside.iterdir()))
        self.assertFalse((task / ".checkpoint-write.lock").exists())
        self.assertFalse((task / "reviews").exists())


if __name__ == "__main__":
    unittest.main()
