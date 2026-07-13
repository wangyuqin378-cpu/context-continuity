from __future__ import annotations

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


def frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    end = lines.index("---", 1)
    return {
        key.strip(): value.strip()
        for key, value in (line.split(":", 1) for line in lines[1:end] if line)
    }


def next_instruction(result: subprocess.CompletedProcess[str]) -> str:
    output = result.stdout + result.stderr
    return next(line for line in output.splitlines() if line.startswith("NEXT:"))


def complete_checkpoint(task_id: str, seq: int, previous: str) -> str:
    return checkpoint(
        task_id,
        seq,
        previous,
        "completion",
        status="complete",
        include_w001=False,
        next_action="- None — task complete.",
        verification="- E001 — The guard exits successfully and reports the expected sequence.",
        blocker="- B001 — None — task complete; no blocker remains.",
        delta="""- Added: none.
- Changed: task status became complete.
- Superseded: none.
- Resolved or cancelled: W001 — completion verified by E001.
- Compressed or dropped: none.
- Authorized by: E001 and the explicit completion command.
- Growth reason: terminal checkpoint.""",
    )


class TerminalInvariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

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
            self.fail(result.stdout + result.stderr)
        if not ok and result.returncode == 0:
            self.fail(result.stdout + result.stderr)
        return result

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
            self.fail(result.stdout + result.stderr)
        if not ok and result.returncode == 0:
            self.fail(result.stdout + result.stderr)
        return result

    def publish_initial(self, task_id: str) -> tuple[Path, Path]:
        task = self.root / task_id
        task.mkdir()
        candidate = task / "candidate.md"
        candidate.write_text(
            checkpoint(task_id, 1, "none", "init"), encoding="utf-8"
        )
        self.run_guard("prepare", candidate)
        self.run_guard("promote", candidate)
        return task, task / "checkpoints" / "0001-init.md"

    def publish_complete(self, task_id: str) -> tuple[Path, Path]:
        task, initial = self.publish_initial(task_id)
        candidate = task / "candidate.md"
        candidate.write_text(
            complete_checkpoint(task_id, 2, initial.name), encoding="utf-8"
        )
        self.run_guard("prepare", candidate, "--previous", initial)
        self.run_guard("promote", candidate)
        return task, task / "checkpoints" / "0002-completion.md"

    def publish_drafted(self, task: Path, previous: Path, trigger: str) -> Path:
        self.run_contextctl("draft", task, "--trigger", trigger)
        candidate = task / "candidate.md"
        self.run_guard("prepare", candidate, "--previous", previous)
        self.run_guard("promote", candidate)
        return task / "checkpoints" / f"{int(previous.name[:4]) + 1:04d}-{trigger}.md"

    def test_new_task_id_is_validated_before_directory_creation(self) -> None:
        for index, task_id in enumerate(
            ("Bad", "bad task", ".bad", "bad\\name", "bad\nname"), start=1
        ):
            with self.subTest(task_id=task_id):
                task = self.root / f"case-{index}" / task_id
                result = self.run_contextctl(
                    "draft", task, "--trigger", "init", ok=False
                )
                self.assertRegex(result.stdout + result.stderr, r"(?i)task.?id")
                self.assertFalse(task.exists())

        for task_id in ("a", "a-1", "a_b", "a.b"):
            with self.subTest(valid=task_id):
                task = self.root / task_id
                self.run_contextctl("draft", task, "--trigger", "init")
                self.assertTrue((task / "candidate.md").is_file())

    def test_trigger_is_validated_before_candidate_creation(self) -> None:
        invalid = ("Bad", "bad trigger", "bad_trigger", "bad.trigger", "-bad", "bad\nextra: value")
        for index, trigger in enumerate(invalid, start=1):
            with self.subTest(trigger=trigger):
                task, latest = self.publish_initial(f"trigger-case-{index}")
                before = latest.read_bytes()
                result = self.run_contextctl(
                    "draft", task, "--trigger", trigger, ok=False
                )
                self.assertIn("trigger", (result.stdout + result.stderr).casefold())
                self.assertFalse((task / "candidate.md").exists())
                self.assertEqual(before, latest.read_bytes())

    def test_init_trigger_errors_give_the_actual_next_action(self) -> None:
        new_task = self.root / "new-task-wrong-trigger"
        result = self.run_contextctl(
            "draft", new_task, "--trigger", "phase-verified", ok=False
        )
        output = (result.stdout + result.stderr).casefold()
        self.assertIn("--trigger init", output)
        self.assertNotIn("existing candidate", output)
        self.assertFalse(new_task.exists())

        task, latest = self.publish_initial("second-init")
        before = latest.read_bytes()
        result = self.run_contextctl("draft", task, "--trigger", "init", ok=False)
        output = (result.stdout + result.stderr).casefold()
        self.assertIn("non-init", output)
        self.assertNotIn("existing candidate", output)
        self.assertFalse((task / "candidate.md").exists())
        self.assertEqual(before, latest.read_bytes())

    def test_unknown_frontmatter_key_is_rejected(self) -> None:
        task = self.root / "extra-meta"
        task.mkdir()
        candidate = task / "candidate.md"
        candidate.write_text(
            checkpoint("extra-meta", 1, "none", "init").replace(
                "status: active", "status: active\nx-extra: injected"
            ),
            encoding="utf-8",
        )
        result = self.run_guard("prepare", candidate, ok=False)
        self.assertRegex(result.stderr, r"(?i)unexpected|unknown")

    def test_identifier_length_limits_fail_early_and_boundary_publishes(self) -> None:
        oversized_task = self.root / ("t" * 201)
        result = self.run_contextctl(
            "draft", oversized_task, "--trigger", "init", ok=False
        )
        self.assertIn("200", result.stdout + result.stderr)
        self.assertFalse(oversized_task.exists())

        task, latest = self.publish_initial("length-boundary")
        oversized_trigger = "a" * 201
        result = self.run_contextctl(
            "draft", task, "--trigger", oversized_trigger, ok=False
        )
        self.assertIn("200", result.stdout + result.stderr)
        self.assertFalse((task / "candidate.md").exists())

        boundary_trigger = "a" * 200
        self.run_contextctl("draft", task, "--trigger", boundary_trigger)
        candidate = task / "candidate.md"
        self.run_guard("prepare", candidate, "--previous", latest)
        self.run_guard("promote", candidate)
        self.assertTrue(
            (task / "checkpoints" / f"0002-{boundary_trigger}.md").is_file()
        )

    def test_complete_parent_blocks_ordinary_drafts_without_artifacts(self) -> None:
        triggers = (
            "phase-verified",
            "decision",
            "contract-change",
            "manual-request",
            "completion",
        )
        for index, trigger in enumerate(triggers, start=1):
            with self.subTest(trigger=trigger):
                task, latest = self.publish_complete(f"complete-block-{index}")
                before = latest.read_bytes()
                result = self.run_contextctl(
                    "draft", task, "--trigger", trigger, ok=False
                )
                output = result.stdout + result.stderr
                self.assertRegex(output, r"(?i)complete|terminal")
                self.assertFalse((task / "candidate.md").exists())
                self.assertFalse((task / "review.json").exists())
                self.assertFalse((task / ".checkpoint-write.lock").exists())
                self.assertEqual([], list(task.rglob("*.publishing-*")))
                self.assertEqual(before, latest.read_bytes())

    def test_allowed_complete_maintenance_drafts_remain_complete(self) -> None:
        for index, trigger in enumerate(
            ("postcommit-receipt", "evidence-repair"), start=1
        ):
            with self.subTest(trigger=trigger):
                task, _ = self.publish_complete(f"complete-allow-{index}")
                result = self.run_contextctl("draft", task, "--trigger", trigger)
                output = result.stdout.casefold()
                self.assertIn("keep status complete", output)
                self.assertIn("terminal next action", output)
                meta = frontmatter(task / "candidate.md")
                self.assertEqual("complete", meta["status"])
                self.assertEqual(trigger, meta["trigger"])

    def test_guard_rejects_reactivation_and_ordinary_trigger_edits(self) -> None:
        task, latest = self.publish_complete("complete-edit")
        self.run_contextctl("draft", task, "--trigger", "postcommit-receipt")
        candidate = task / "candidate.md"
        original = candidate.read_text(encoding="utf-8")

        candidate.write_text(
            original.replace("status: complete", "status: active", 1),
            encoding="utf-8",
        )
        result = self.run_guard(
            "prepare", candidate, "--previous", latest, ok=False
        )
        self.assertRegex(result.stderr, r"(?i)complete|terminal")

        candidate.write_text(
            original.replace(
                "trigger: postcommit-receipt", "trigger: phase-verified", 1
            ),
            encoding="utf-8",
        )
        result = self.run_guard(
            "prepare", candidate, "--previous", latest, ok=False
        )
        self.assertRegex(result.stderr, r"(?i)complete|terminal")

    def test_locked_promote_rejects_candidate_changed_after_prepare(self) -> None:
        task, latest = self.publish_complete("complete-promote-edit")
        self.run_contextctl("draft", task, "--trigger", "postcommit-receipt")
        candidate = task / "candidate.md"
        self.run_guard("prepare", candidate, "--previous", latest)
        candidate.write_text(
            candidate.read_text(encoding="utf-8").replace(
                "status: complete", "status: active", 1
            ),
            encoding="utf-8",
        )
        result = self.run_guard("promote", candidate, ok=False)
        self.assertRegex(result.stderr, r"(?i)complete|terminal")
        self.assertEqual(2, len(list((task / "checkpoints").glob("*.md"))))

    def test_receipt_is_single_and_evidence_repair_can_follow(self) -> None:
        task, completion = self.publish_complete("receipt-once")
        receipt = self.publish_drafted(task, completion, "postcommit-receipt")

        result = self.run_contextctl(
            "draft", task, "--trigger", "postcommit-receipt", ok=False
        )
        self.assertRegex(result.stdout + result.stderr, r"(?i)receipt|complete")
        self.assertFalse((task / "candidate.md").exists())

        self.run_contextctl("draft", task, "--trigger", "evidence-repair")
        meta = frontmatter(task / "candidate.md")
        self.assertEqual("complete", meta["status"])
        self.assertEqual(receipt.name, meta["previous"])

    def test_terminal_status_and_completion_trigger_are_bound(self) -> None:
        task, latest = self.publish_initial("terminal-trigger-binding")
        candidate = task / "candidate.md"

        candidate.write_text(
            complete_checkpoint(task.name, 2, latest.name).replace(
                "trigger: completion", "trigger: phase-verified", 1
            ),
            encoding="utf-8",
        )
        result = self.run_guard(
            "prepare", candidate, "--previous", latest, ok=False
        )
        self.assertRegex(result.stderr, r"(?i)completion|complete")

        candidate.write_text(
            checkpoint(task.name, 2, latest.name, "completion"),
            encoding="utf-8",
        )
        result = self.run_guard(
            "prepare", candidate, "--previous", latest, ok=False
        )
        self.assertRegex(result.stderr, r"(?i)completion|complete")

    def test_terminal_error_routes_match_the_actual_state(self) -> None:
        active, _ = self.publish_initial("route-active-completion")
        result = self.run_contextctl(
            "draft", active, "--trigger", "completion", ok=False
        )
        instruction = next_instruction(result).casefold()
        self.assertIn("contextctl complete", instruction)
        self.assertNotIn("evidence-repair", instruction)

        complete, completion = self.publish_complete("route-completed-ordinary")
        result = self.run_contextctl(
            "draft", complete, "--trigger", "phase-verified", ok=False
        )
        instruction = next_instruction(result).casefold()
        self.assertIn("evidence-repair", instruction)
        self.assertIn("--trigger init", instruction)
        self.assertNotIn("use complete", instruction)

        receipt = self.publish_drafted(
            complete, completion, "postcommit-receipt"
        )
        self.assertTrue(receipt.is_file())
        result = self.run_contextctl(
            "draft", complete, "--trigger", "postcommit-receipt", ok=False
        )
        instruction = next_instruction(result).casefold()
        self.assertIn("receipt slot", instruction)
        self.assertIn("evidence-repair", instruction)
        self.assertIn("--trigger init", instruction)
        self.assertNotIn("use complete", instruction)

    def test_complete_future_evidence_has_append_only_recovery_instruction(self) -> None:
        task = self.root / "future-completion-evidence"
        task.mkdir()
        candidate = task / "candidate.md"
        candidate.write_text(
            checkpoint(
                task.name,
                1,
                "none",
                "init",
                verification="- E999 — A future receipt will record completion.",
            ),
            encoding="utf-8",
        )
        self.run_guard("prepare", candidate)
        self.run_guard("promote", candidate)

        result = self.run_contextctl("complete", task, ok=False)
        instruction = next_instruction(result).casefold()
        self.assertIn("phase-verified", instruction)
        self.assertIn("defined final e", instruction)
        self.assertIn("rerun complete", instruction)
        self.assertFalse(candidate.exists())

    def test_draft_help_exposes_identifier_and_terminal_rules(self) -> None:
        result = self.run_contextctl("draft", "--help")
        output = re.sub(r"\s+", " ", result.stdout.casefold())
        self.assertIn("task id", output)
        self.assertIn("lowercase", output)
        self.assertIn("trigger", output)
        self.assertIn("postcommit-receipt", output)
        self.assertIn("evidence-repair", output)
        self.assertIn("200", output)


if __name__ == "__main__":
    unittest.main()
