from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_checkpoint_guard import checkpoint


SKILL_ROOT = Path(__file__).parents[1]
CONTEXTCTL = SKILL_ROOT / "scripts" / "contextctl.py"
GUARD = SKILL_ROOT / "scripts" / "checkpoint_guard.py"
NEXT_ACTION = "- W001 — Run the checkpoint guard once."
VERIFICATION = (
    "- E001 — The guard exits successfully and reports the expected sequence."
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ContextctlPublishRedTeamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.task = self.make_task(self.root / "workspace-a")
        self.latest = self.publish_initial(self.task)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_guard(
        self, *args: object, ok: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(GUARD), *(str(arg) for arg in args)],
            text=True,
            capture_output=True,
            check=False,
        )
        if ok and result.returncode != 0:
            self.fail(result.stderr)
        if not ok and result.returncode == 0:
            self.fail(result.stdout)
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

    def make_task(self, workspace: Path) -> Path:
        task = workspace / "fixture-task"
        (task / "evidence").mkdir(parents=True)
        (task / "evidence" / "init.log").write_text(
            "checkpoint initialized\n", encoding="utf-8"
        )
        return task

    def publish_initial(self, task: Path) -> Path:
        candidate = task / "candidate.md"
        text = checkpoint("fixture-task", 1, "none", "init").replace(
            "created_at: 2026-07-11T12:00:00+08:00",
            "created_at: 2000-01-01T00:00:00+00:00",
        )
        candidate.write_text(text, encoding="utf-8")
        self.run_guard("prepare", candidate)
        self.run_guard("promote", candidate)
        return task / "checkpoints" / "0001-init.md"

    def draft_and_review(self, task: Path) -> tuple[Path, Path]:
        self.run_contextctl("draft", task, "--trigger", "phase-verified")
        candidate = task / "candidate.md"
        manifest = task / "review.json"
        self.run_contextctl("review-init", candidate, "--output", manifest)
        return candidate, manifest

    def complete_review(self, manifest: Path) -> dict[str, object]:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        required_ids = list(payload["required_ids"])
        payload.update(
            {
                "reviewer": "fresh-agent-redteam",
                "reviewed_at": "2026-07-11T13:00:00+08:00",
                "recovered_ids": required_ids,
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
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return payload

    def assert_unchanged_failure(
        self,
        *,
        candidate: Path,
        candidate_hash: str,
        latest: Path,
        latest_hash: str,
        checkpoint_count: int = 1,
    ) -> None:
        self.assertTrue(candidate.is_file())
        self.assertEqual(candidate_hash, digest(candidate))
        self.assertEqual(latest_hash, digest(latest))
        self.assertEqual(
            checkpoint_count,
            len(list((latest.parent).glob("*.md"))),
        )
        self.assertFalse((candidate.parent / ".checkpoint-write.lock").exists())
        self.assertEqual([], list(candidate.parent.rglob(".publishing-*")))

    def test_review_manifest_cannot_replay_across_canonical_task_roots(self) -> None:
        candidate_a, manifest_a = self.draft_and_review(self.task)
        self.complete_review(manifest_a)

        task_b = self.make_task(self.root / "workspace-b")
        latest_b = self.publish_initial(task_b)
        candidate_b = task_b / "candidate.md"
        candidate_b.write_bytes(candidate_a.read_bytes())
        manifest_b = task_b / "review.json"
        manifest_b.write_bytes(manifest_a.read_bytes())
        candidate_hash = digest(candidate_b)
        latest_hash = digest(latest_b)

        replay = self.run_contextctl(
            "publish", candidate_b, "--review", manifest_b, ok=False
        )

        self.assertIn("canonical task root", replay.stdout + replay.stderr)
        self.assert_unchanged_failure(
            candidate=candidate_b,
            candidate_hash=candidate_hash,
            latest=latest_b,
            latest_hash=latest_hash,
        )

    def test_duplicate_json_keys_are_rejected_without_publication(self) -> None:
        candidate, manifest = self.draft_and_review(self.task)
        self.complete_review(manifest)
        raw = manifest.read_text(encoding="utf-8")
        manifest.write_text(
            raw.replace("{\n", '{\n  "verdict": "PASS",\n', 1),
            encoding="utf-8",
        )
        candidate_hash = digest(candidate)
        latest_hash = digest(self.latest)

        duplicate = self.run_contextctl(
            "publish", candidate, "--review", manifest, ok=False
        )

        self.assertIn("duplicate key", duplicate.stdout + duplicate.stderr)
        self.assert_unchanged_failure(
            candidate=candidate,
            candidate_hash=candidate_hash,
            latest=self.latest,
            latest_hash=latest_hash,
        )

    def test_candidate_byte_edit_after_pass_invalidates_publication(self) -> None:
        candidate, manifest = self.draft_and_review(self.task)
        self.complete_review(manifest)
        self.run_contextctl("review-check", candidate, manifest)
        candidate.write_text(
            candidate.read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        edited_hash = digest(candidate)
        latest_hash = digest(self.latest)

        stale = self.run_contextctl(
            "publish", candidate, "--review", manifest, ok=False
        )

        self.assertRegex(stale.stdout + stale.stderr, r"SHA-256|changed after review")
        self.assert_unchanged_failure(
            candidate=candidate,
            candidate_hash=edited_hash,
            latest=self.latest,
            latest_hash=latest_hash,
        )

    def test_reviewed_candidate_is_stale_after_another_publish_wins(self) -> None:
        candidate, manifest = self.draft_and_review(self.task)
        self.complete_review(manifest)
        reviewed_hash = digest(candidate)

        competitor = self.task / "competitor.md"
        competitor.write_bytes(candidate.read_bytes())
        self.run_guard("promote", competitor)
        winner = self.task / "checkpoints" / "0002-phase-verified.md"
        winner_hash = digest(winner)

        stale = self.run_contextctl(
            "publish", candidate, "--review", manifest, ok=False
        )

        self.assertRegex(
            stale.stdout + stale.stderr,
            r"seq must be 3|previous must be|missing its sidecar",
        )
        self.assert_unchanged_failure(
            candidate=candidate,
            candidate_hash=reviewed_hash,
            latest=winner,
            latest_hash=winner_hash,
            checkpoint_count=2,
        )
        self.run_guard("audit", self.task)

    def test_expected_sha_promote_mismatch_preserves_candidate_and_latest(self) -> None:
        candidate, _ = self.draft_and_review(self.task)
        candidate_hash = digest(candidate)
        latest_hash = digest(self.latest)

        mismatch = self.run_guard(
            "promote",
            candidate,
            "--expected-sha256",
            "0" * 64,
            ok=False,
        )

        self.assertIn("changed after review", mismatch.stdout + mismatch.stderr)
        self.assert_unchanged_failure(
            candidate=candidate,
            candidate_hash=candidate_hash,
            latest=self.latest,
            latest_hash=latest_hash,
        )

    def test_archived_review_sidecar_matches_published_checkpoint_hash(self) -> None:
        candidate, manifest = self.draft_and_review(self.task)
        self.complete_review(manifest)
        reviewed_manifest = manifest.read_bytes()
        reviewed_hash = digest(candidate)

        self.run_contextctl("publish", candidate, "--review", manifest)

        published = self.task / "checkpoints" / "0002-phase-verified.md"
        sidecar = self.task / "reviews" / "0002-phase-verified.json"
        self.assertTrue(sidecar.is_file())
        self.assertEqual(reviewed_manifest, sidecar.read_bytes())
        archived = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(reviewed_hash, digest(published))
        self.assertEqual(digest(published), archived["candidate_sha256"])
        self.assertEqual(str(self.task.resolve()), archived["canonical_task_root"])
        self.assertFalse(candidate.exists())
        self.run_guard("audit", self.task)

    def test_empty_reviewer_and_wrong_review_mode_fail_closed(self) -> None:
        candidate, manifest = self.draft_and_review(self.task)
        payload = self.complete_review(manifest)
        candidate_hash = digest(candidate)
        latest_hash = digest(self.latest)

        payload["reviewer"] = ""
        manifest.write_text(
            json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        empty = self.run_contextctl(
            "publish", candidate, "--review", manifest, ok=False
        )
        self.assertIn("reviewer", empty.stdout + empty.stderr)

        payload["reviewer"] = "same-permission-declared-reviewer"
        payload["review_mode"] = "self-review"
        manifest.write_text(
            json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        wrong_mode = self.run_contextctl(
            "publish", candidate, "--review", manifest, ok=False
        )
        self.assertIn("fresh-agent-source-first", wrong_mode.stdout + wrong_mode.stderr)
        self.assert_unchanged_failure(
            candidate=candidate,
            candidate_hash=candidate_hash,
            latest=self.latest,
            latest_hash=latest_hash,
        )


if __name__ == "__main__":
    unittest.main()
