from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_checkpoint_guard import CONTRACT, checkpoint


SKILL_ROOT = Path(__file__).parents[1]
CONTEXTCTL = SKILL_ROOT / "scripts" / "contextctl.py"
GUARD = SKILL_ROOT / "scripts" / "checkpoint_guard.py"
REVIEW_SCHEMA = "context-continuity/review/v2"
REQUIRED_IDS = {
    "A001",
    "B001",
    "C001",
    "D001",
    "E001",
    "F001",
    "G001",
    "I001",
    "O001",
    "S001",
    "W001",
}
NEXT_ACTION = "- W001 — Run the checkpoint guard once."
VERIFICATION = (
    "- E001 — The guard exits successfully and reports the expected sequence."
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def document_parts(path: Path) -> tuple[dict[str, str], str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    end = lines.index("---", 1)
    meta = {
        key.strip(): value.strip()
        for key, value in (line.split(":", 1) for line in lines[1:end] if line)
    }
    return meta, "\n".join(lines[end + 1 :]).strip() + "\n"


class ContextctlPublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.task = Path(self.temporary.name) / "fixture-task"
        (self.task / "evidence").mkdir(parents=True)
        (self.task / "evidence" / "init.log").write_text(
            "checkpoint initialized\n", encoding="utf-8"
        )
        self.latest = self.publish_initial()

    def tearDown(self) -> None:
        self.temporary.cleanup()

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

    def publish_initial(self) -> Path:
        candidate = self.task / "candidate.md"
        text = checkpoint("fixture-task", 1, "none", "init").replace(
            "created_at: 2026-07-11T12:00:00+08:00",
            "created_at: 2000-01-01T00:00:00+00:00",
        )
        candidate.write_text(text, encoding="utf-8")
        self.run_guard("prepare", candidate)
        self.run_guard("promote", candidate)
        return self.task / "checkpoints" / "0001-init.md"

    def draft(self) -> Path:
        self.run_contextctl("draft", self.task, "--trigger", "phase-verified")
        candidate = self.task / "candidate.md"
        self.assertTrue(candidate.is_file())
        return candidate

    def review_init(self, candidate: Path) -> Path:
        manifest = self.task / "review.json"
        self.run_contextctl("review-init", candidate, "--output", manifest)
        self.assertTrue(manifest.is_file())
        return manifest

    def complete_review(self, manifest: Path) -> dict[str, object]:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload.update(
            {
                "reviewer": "fresh-agent-test",
                "reviewed_at": "2026-07-11T13:00:00+08:00",
                "required_ids": sorted(REQUIRED_IDS),
                "recovered_ids": sorted(REQUIRED_IDS),
                "missing_ids": [],
                "contradictions": [],
                "ambiguities": [],
                "recovered_next_action": NEXT_ACTION,
                "recovered_verification": VERIFICATION,
                "verdict": "PASS",
            }
        )
        for record in payload["evidence_health"]:
            record["status"] = "OK"
            record["semantic_status"] = "DIRECT"
        manifest.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return payload

    def test_draft_copies_latest_and_updates_only_draft_metadata(self) -> None:
        old_meta, old_body = document_parts(self.latest)

        candidate = self.draft()
        meta, body = document_parts(candidate)

        self.assertEqual("fixture-task", meta["task_id"])
        self.assertEqual("2", meta["seq"])
        self.assertEqual(self.latest.name, meta["previous"])
        self.assertEqual("auto", meta["previous_sha256"])
        self.assertEqual("auto", meta["contract_sha256"])
        self.assertEqual("phase-verified", meta["trigger"])
        self.assertNotEqual(old_meta["created_at"], meta["created_at"])
        self.assertRegex(meta["created_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        self.assertEqual(old_body, body)
        self.assertIn(CONTRACT, body)
        for heading in (
            "## Contract",
            "## Delta From Previous",
            "## Retention Audit",
        ):
            self.assertEqual(1, body.count(heading))

    def test_initial_draft_creates_skeleton_and_cannot_publish_markers(self) -> None:
        task = Path(self.temporary.name) / "new-task"
        task.mkdir()

        self.run_contextctl("draft", task, "--trigger", "init")
        candidate = task / "candidate.md"
        meta, body = document_parts(candidate)

        self.assertEqual("new-task", meta["task_id"])
        self.assertEqual("1", meta["seq"])
        self.assertEqual("none", meta["previous"])
        self.assertEqual("auto", meta["previous_sha256"])
        self.assertEqual("1", meta["contract_version"])
        self.assertEqual("init", meta["trigger"])
        self.assertIn("REPLACE-ME", body)
        for heading in (
            "## Contract",
            "## Current State",
            "## Next Action",
            "## Evidence Pointers",
            "## Delta From Previous",
            "## Retention Audit",
        ):
            self.assertEqual(1, body.count(heading))

        manifest = task / "review.json"
        blocked = self.run_contextctl(
            "review-init", candidate, "--output", manifest, ok=False
        )
        self.assertIn("REPLACE-ME", blocked.stdout + blocked.stderr)
        self.assertFalse(manifest.exists())

    def test_review_init_finalizes_hashes_and_binds_template_to_candidate(self) -> None:
        candidate = self.draft()
        manifest = self.review_init(candidate)
        meta, _ = document_parts(candidate)
        payload = json.loads(manifest.read_text(encoding="utf-8"))

        self.assertNotEqual("auto", meta["previous_sha256"])
        self.assertNotEqual("auto", meta["contract_sha256"])
        self.assertEqual(digest(self.latest), meta["previous_sha256"])
        self.assertEqual(
            hashlib.sha256((CONTRACT + "\n").encode()).hexdigest(),
            meta["contract_sha256"],
        )
        self.assertEqual(REVIEW_SCHEMA, payload["schema"])
        self.assertEqual(digest(candidate), payload["candidate_sha256"])
        self.assertEqual(REQUIRED_IDS, set(payload["required_ids"]))
        self.assertEqual([], payload["recovered_ids"])
        self.assertEqual(REQUIRED_IDS, set(payload["missing_ids"]))
        self.assertEqual("", payload["recovered_next_action"])
        self.assertEqual("", payload["recovered_verification"])
        self.assertEqual("INCOMPLETE", payload["verdict"])
        self.assertTrue(
            any(
                "detected_status" in instruction and "EXTERNAL" in instruction
                for instruction in payload["instructions"]
            )
        )
        self.assertEqual(
            {"E001": "OK"},
            {
                record["id"]: record["detected_status"]
                for record in payload["evidence_health"]
            },
        )
        self.assertEqual(
            {"E001": "UNCHECKED"},
            {record["id"]: record["status"] for record in payload["evidence_health"]},
        )
        self.assertEqual(
            {"E001": "UNCHECKED"},
            {
                record["id"]: record["semantic_status"]
                for record in payload["evidence_health"]
            },
        )

    def test_review_check_rejects_incomplete_or_missing_ids_then_accepts_full(self) -> None:
        candidate = self.draft()
        manifest = self.review_init(candidate)

        incomplete = self.run_contextctl(
            "review-check", candidate, manifest, ok=False
        )
        self.assertRegex(incomplete.stdout + incomplete.stderr, r"INCOMPLETE|review")

        payload = self.complete_review(manifest)
        payload["required_ids"] = [
            item_id for item_id in payload["required_ids"] if item_id != "A001"
        ]
        payload["recovered_ids"] = [
            item_id for item_id in payload["recovered_ids"] if item_id != "A001"
        ]
        manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        missing = self.run_contextctl("review-check", candidate, manifest, ok=False)
        self.assertIn("A001", missing.stdout + missing.stderr)

        self.complete_review(manifest)
        accepted = self.run_contextctl("review-check", candidate, manifest)
        self.assertIn("PASS", accepted.stdout)

    def test_review_accepts_one_terminal_newline_but_no_other_whitespace(self) -> None:
        candidate = self.draft()
        manifest = self.review_init(candidate)
        payload = self.complete_review(manifest)
        payload["recovered_next_action"] += "\n"
        payload["recovered_verification"] += "\n"
        manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        accepted = self.run_contextctl("review-check", candidate, manifest)
        self.assertIn("PASS", accepted.stdout)

        payload["recovered_next_action"] = NEXT_ACTION + " "
        manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        rejected = self.run_contextctl("review-check", candidate, manifest, ok=False)
        self.assertIn("exactly match", rejected.stdout + rejected.stderr)

    def test_candidate_change_invalidates_completed_review(self) -> None:
        candidate = self.draft()
        manifest = self.review_init(candidate)
        self.complete_review(manifest)
        self.run_contextctl("review-check", candidate, manifest)

        candidate.write_text(
            candidate.read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )

        stale = self.run_contextctl("review-check", candidate, manifest, ok=False)
        self.assertRegex(stale.stdout + stale.stderr, r"SHA|hash|changed")

    def test_publish_promotes_exactly_one_reviewed_checkpoint_and_audits(self) -> None:
        candidate = self.draft()
        manifest = self.review_init(candidate)
        self.complete_review(manifest)
        prepared_hash = digest(candidate)
        old_hash = digest(self.latest)

        self.run_contextctl("publish", candidate, "--review", manifest)

        published = self.task / "checkpoints" / "0002-phase-verified.md"
        self.assertTrue(published.is_file())
        self.assertEqual(prepared_hash, digest(published))
        self.assertEqual(old_hash, digest(self.latest))
        self.assertFalse(candidate.exists())
        self.assertEqual(2, len(list((self.task / "checkpoints").glob("*.md"))))
        self.run_guard("audit", self.task)
        self.assertEqual(
            str(published), self.run_guard("latest", self.task).stdout.strip()
        )

    def test_publish_failure_preserves_candidate_and_old_latest(self) -> None:
        candidate = self.draft()
        manifest = self.review_init(candidate)
        candidate_hash = digest(candidate)
        old_hash = digest(self.latest)

        failure = self.run_contextctl(
            "publish", candidate, "--review", manifest, ok=False
        )

        self.assertRegex(failure.stdout + failure.stderr, r"INCOMPLETE|review")
        self.assertTrue(candidate.is_file())
        self.assertEqual(candidate_hash, digest(candidate))
        self.assertEqual(old_hash, digest(self.latest))
        self.assertEqual([self.latest], list((self.task / "checkpoints").glob("*.md")))


if __name__ == "__main__":
    unittest.main()
