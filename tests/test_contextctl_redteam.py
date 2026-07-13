from __future__ import annotations

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


class ContextctlRedTeamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

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

    def run_contextctl(self, *args: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CONTEXTCTL), *(str(arg) for arg in args)],
            text=True,
            capture_output=True,
            check=False,
        )

    def make_task(self, task_id: str) -> Path:
        task = self.root / task_id
        (task / "evidence").mkdir(parents=True)
        (task / "evidence" / "init.log").write_text(
            "checkpoint initialized\n", encoding="utf-8"
        )
        return task

    def publish(
        self,
        task: Path,
        text: str,
        *,
        seq: int,
        trigger: str,
        previous: Path | None = None,
    ) -> Path:
        candidate = task / "candidate.md"
        candidate.write_text(text, encoding="utf-8")
        prepare_args: list[object] = ["prepare", candidate]
        if previous is not None:
            prepare_args.extend(["--previous", previous])
        self.run_guard(*prepare_args)
        self.run_guard("promote", candidate)
        return task / "checkpoints" / f"{seq:04d}-{trigger}.md"

    def initial_text(
        self,
        task_id: str,
        *,
        pointer: str = "evidence/init.log#L1",
        **kwargs: object,
    ) -> str:
        text = checkpoint(task_id, 1, "none", "init", **kwargs).replace(
            "evidence/init.log#sha256=INIT", pointer
        )
        return text.replace("\n- No additional evidence.", "")

    def make_two_checkpoint_chain(self, task_id: str) -> tuple[Path, Path, Path]:
        task = self.make_task(task_id)
        first = self.publish(
            task,
            self.initial_text(task_id),
            seq=1,
            trigger="init",
        )
        delta = """- Added: none.
- Changed: none.
- Superseded: none.
- Resolved or cancelled: none.
- Compressed or dropped: none.
- Authorized by: none.
- Growth reason: hash-link metadata is durable chain evidence."""
        second_text = checkpoint(
            task_id,
            2,
            first.name,
            "phase-verified",
            delta=delta,
        ).replace("evidence/init.log#sha256=INIT", "evidence/init.log#L1")
        second_text = second_text.replace("\n- No additional evidence.", "")
        second = self.publish(
            task,
            second_text,
            seq=2,
            trigger="phase-verified",
            previous=first,
        )
        return task, first, second

    def assert_non_ready(self, result: subprocess.CompletedProcess[str]) -> None:
        combined = result.stdout + result.stderr
        if result.returncode == 0:
            payload = json.loads(result.stdout)
            self.assertNotEqual("READY", payload.get("readiness"), combined)
        else:
            self.assertNotIn('"readiness": "READY"', combined)

    def test_resume_audit_rejects_corrupt_earlier_and_latest_checkpoints(self) -> None:
        for corrupt_index in (1, 2):
            with self.subTest(corrupt_index=corrupt_index):
                task, earlier, latest = self.make_two_checkpoint_chain(
                    f"corrupt-{corrupt_index}"
                )
                target = earlier if corrupt_index == 1 else latest
                target.write_text(
                    "---\nschema: context-continuity/v1\n---\n",
                    encoding="utf-8",
                )

                result = self.run_contextctl("resume", task)
                combined = result.stdout + result.stderr

                self.assertNotEqual(0, result.returncode, combined)
                self.assertNotIn("READY ·", combined)
                self.assertNotIn("Run the checkpoint guard once", combined)

    def test_unsafe_evidence_pointers_never_resolve_or_execute(self) -> None:
        outside = self.root / "outside-secret.txt"
        outside.write_text("OUTSIDE-CONTENT-MUST-NOT-BE-READ\n", encoding="utf-8")
        command_marker = self.root / "command-was-executed"
        cases = {
            "escape": "../outside-secret.txt#L1",
            "glob": "evidence/*.log",
            "command": f"evidence/$(touch {command_marker})",
        }

        for name, pointer in cases.items():
            with self.subTest(pointer=name):
                task_id = f"pointer-{name}"
                task = self.make_task(task_id)
                (task / "evidence" / "would-match.log").write_text(
                    "glob decoy\n", encoding="utf-8"
                )
                self.publish(
                    task,
                    self.initial_text(task_id, pointer=pointer),
                    seq=1,
                    trigger="init",
                )

                result = self.run_contextctl("resume", task, "--json")
                self.assertEqual(0, result.returncode, result.stderr)
                payload = json.loads(result.stdout)
                record = payload["evidence"][0]

                self.assertNotEqual("READY", payload["readiness"])
                self.assertNotEqual("OK", record["status"])
                self.assertIsNone(record["resolved_path"])
                self.assertNotIn("OUTSIDE-CONTENT-MUST-NOT-BE-READ", result.stdout)
                self.assertFalse(command_marker.exists())

    def test_active_next_action_requires_exactly_one_current_work_id(self) -> None:
        cases = {
            "missing": (
                "- Inspect the retry path without a stable work identity.",
                None,
            ),
            "multiple": (
                "- W001 / W002 — Execute two unrelated work items together.",
                "- Remaining: W002 — A second current work item.",
            ),
        }

        for name, (next_action, extra_work) in cases.items():
            with self.subTest(case=name):
                task_id = f"action-{name}"
                task = self.make_task(task_id)
                text = self.initial_text(task_id, next_action=next_action)
                if extra_work is not None:
                    text = text.replace(
                        "- Remaining: W001 — Publish and verify the next checkpoint.",
                        "- Remaining: W001 — Publish and verify the next checkpoint.\n"
                        + extra_work,
                    )
                self.publish(task, text, seq=1, trigger="init")

                result = self.run_contextctl("resume", task, "--json")

                self.assert_non_ready(result)

    def test_legacy_single_work_next_marker_remains_resumable(self) -> None:
        task = self.make_task("legacy-next-marker")
        text = self.initial_text(
            "legacy-next-marker",
            next_action=(
                "- NEXT-VERIFY-CHECKPOINT-1 — Run the one current checkpoint action."
            ),
        )
        self.publish(task, text, seq=1, trigger="init")

        result = self.run_contextctl("resume", task, "--json")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("NEXT-VERIFY-CHECKPOINT-1", result.stdout)

    def test_complete_sentinel_cannot_hide_an_extra_action(self) -> None:
        task_id = "complete-substring"
        task = self.make_task(task_id)
        text = self.initial_text(
            task_id,
            status="complete",
            include_w001=False,
            next_action="- Delete production data; None — task complete.",
            verification="- E001 — Final acceptance evidence is recorded.",
        )
        candidate = task / "candidate.md"
        candidate.write_text(text, encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(GUARD), "prepare", str(candidate)],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("task complete", result.stderr)
        self.assertFalse((task / "checkpoints").exists())

    def test_resume_never_silently_omits_unparsed_protected_content(self) -> None:
        task_id = "protected-omission"
        task = self.make_task(task_id)
        text = self.initial_text(task_id)
        text = text.replace(
            "- O001 Out of scope: Global host hook installation.",
            "- O001 Out of scope: Global host hook installation.\n"
            "  O001-CONTINUATION-MUST-SURVIVE",
        )
        text = text.replace(
            "## Contract\n\n",
            "## Contract\n\n"
            "- PERMISSION-MUST-SURVIVE: never rotate production credentials.\n",
            1,
        )
        self.publish(task, text, seq=1, trigger="init")

        result = self.run_contextctl("resume", task)
        combined = result.stdout + result.stderr

        if result.returncode == 0:
            self.assertIn("O001-CONTINUATION-MUST-SURVIVE", result.stdout)
            self.assertIn("PERMISSION-MUST-SURVIVE", result.stdout)
        else:
            self.assertNotIn("READY ·", combined)

    def test_repeated_rendering_is_byte_identical(self) -> None:
        task_id = "deterministic-render"
        task = self.make_task(task_id)
        self.publish(
            task,
            self.initial_text(task_id),
            seq=1,
            trigger="init",
        )

        first = self.run_contextctl("resume", task)
        second = self.run_contextctl("resume", task)

        self.assertEqual(0, first.returncode, first.stderr)
        self.assertTrue(first.stdout.startswith("READY ·"), first.stdout)
        self.assertEqual(first.returncode, second.returncode)
        self.assertEqual(first.stdout.encode("utf-8"), second.stdout.encode("utf-8"))
        self.assertEqual(first.stderr.encode("utf-8"), second.stderr.encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
