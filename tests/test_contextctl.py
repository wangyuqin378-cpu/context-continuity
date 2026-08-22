from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_checkpoint_guard import CONTRACT, checkpoint


CONTEXTCTL = Path(__file__).parents[1] / "scripts" / "contextctl.py"
GUARD = Path(__file__).parents[1] / "scripts" / "checkpoint_guard.py"

LATEST_NEXT_ACTION = "- W001 — Run CONTEXT-RESUME-002."
LATEST_VERIFICATION = "- E001 — Confirm RESUME-CARD-002 is printed exactly."
LATEST_DECISION = (
    "- D002 — Keep recovery output deterministic. "
    "Why: repeated resumes must be diffable. Supersedes: none."
)
CURRENT_STATE = [
    "- Verified: E001 confirms the checkpoint protocol is initialized.",
    "- In progress: checkpoint verification.",
    "- Remaining: W001 — Publish and verify the next checkpoint.",
]


class ContextctlResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.task = Path(self.temporary.name) / "fixture-task"
        self.task.mkdir()
        evidence = self.task / "evidence"
        evidence.mkdir()
        (evidence / "init.log").write_text("checkpoint initialized\n", encoding="utf-8")
        (evidence / "output-contract.md").write_text(
            "resume output contract\n", encoding="utf-8"
        )
        self.publish_checkpoints()

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

    def publish_checkpoints(self) -> None:
        candidate = self.task / "candidate.md"
        initial = checkpoint("fixture-task", 1, "none", "init").replace(
            "evidence/init.log#sha256=INIT", "evidence/init.log#L1"
        )
        candidate.write_text(initial, encoding="utf-8")
        self.run_guard("prepare", candidate)
        self.run_guard("promote", candidate)
        previous = self.task / "checkpoints" / "0001-init.md"

        delta = """- Added: D002 and E002.
- Changed: W001 now names the deterministic resume command and proof.
- Superseded: none.
- Resolved or cancelled: none.
- Compressed or dropped: placeholder decision and evidence lines.
- Authorized by: current resume-facade evaluation.
- Growth reason: D002 and E002 add the durable output contract."""
        latest = checkpoint(
            "fixture-task",
            2,
            previous.name,
            "decision",
            extra_decisions=LATEST_DECISION,
            extra_evidence=(
                "- E002 — `evidence/output-contract.md#L1` — supports D002."
            ),
            next_action=LATEST_NEXT_ACTION,
            verification=LATEST_VERIFICATION,
            delta=delta,
        ).replace("evidence/init.log#sha256=INIT", "evidence/init.log#L1")
        candidate.write_text(latest, encoding="utf-8")
        self.run_guard("prepare", candidate, "--previous", previous)
        self.run_guard("promote", candidate)
        self.latest = self.task / "checkpoints" / "0002-decision.md"

    def test_resume_audits_latest_and_prints_complete_compact_card(self) -> None:
        first = self.run_contextctl("resume", self.task, "--full").stdout
        second = self.run_contextctl("resume", self.task, "--full").stdout

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("READY "))
        self.assertIn("0002-decision.md", first)
        self.assertIn("SUCCESSOR TRANSITIONS:", first)
        self.assertNotIn("- W001 — Run the checkpoint guard once.", first)
        for line in CONTRACT.splitlines():
            self.assertIn(line, first)
        for line in CURRENT_STATE:
            self.assertIn(line, first)
        for line in (
            "- D001 — Select latest by continuous sequence. Why: timestamps can drift. Supersedes: none.",
            LATEST_DECISION,
            LATEST_NEXT_ACTION,
            LATEST_VERIFICATION,
            "- B001 — none; no release condition is needed.",
            "- F001 — none; no failed approach is active.",
            "- A001 — The host may not provide lifecycle hooks.",
        ):
            self.assertIn(line, first)
        self.assertLessEqual(len(first.splitlines()), 30)

    def test_resume_defaults_to_a_bounded_daily_card(self) -> None:
        card = self.run_contextctl("resume", self.task).stdout

        self.assertTrue(card.startswith("READY "))
        self.assertIn("GOAL:", card)
        self.assertIn(LATEST_NEXT_ACTION, card)
        self.assertIn(LATEST_VERIFICATION, card)
        self.assertIn("ACTIVE IDS:", card)
        self.assertIn("HEALTH:", card)
        self.assertIn("resume <task-dir> --full", card)
        self.assertNotIn("IN SCOPE:", card)
        self.assertNotIn(LATEST_DECISION, card)
        self.assertLessEqual(len(card.splitlines()), 12)

    def test_version_reports_the_lean_release(self) -> None:
        result = self.run_contextctl("--version")

        self.assertEqual("2.1.0", result.stdout.strip())

    def test_resume_warns_when_local_evidence_is_missing(self) -> None:
        (self.task / "evidence" / "init.log").unlink()

        result = self.run_contextctl("resume", self.task)
        output = result.stdout + result.stderr

        self.assertIn("WARNING", output)
        self.assertIn("evidence/init.log", output)
        self.assertNotEqual("READY", output.strip())

    def test_resume_rejects_a_corrupt_latest_checkpoint(self) -> None:
        self.latest.write_text(
            "---\nschema: context-continuity/v1\n---\n", encoding="utf-8"
        )

        result = self.run_contextctl("resume", self.task, ok=False)
        output = result.stdout + result.stderr

        self.assertIn("FAIL", output)
        self.assertRegex(output, r"frontmatter|checkpoint|Contract|metadata|audit")
        self.assertNotIn(LATEST_NEXT_ACTION, output)
        self.assertNotIn("READY", output)

    def test_resume_json_has_stable_keys_and_exact_values(self) -> None:
        result = self.run_contextctl("resume", self.task, "--json")
        payload = json.loads(result.stdout)

        self.assertEqual(
            {
                "schema",
                "task_id",
                "checkpoint",
                "seq",
                "status",
                "readiness",
                "warnings",
                "contract",
                "current_state",
                "decisions",
                "transitions",
                "next_action",
                "verification",
                "evidence",
                "source",
                "blockers",
                "failures",
                "unknowns",
                "working_state",
            },
            set(payload),
        )
        self.assertEqual("context-continuity/resume/v1", payload["schema"])
        self.assertEqual("fixture-task", payload["task_id"])
        self.assertEqual("0002-decision.md", payload["checkpoint"])
        self.assertEqual(2, payload["seq"])
        self.assertEqual("active", payload["status"])
        self.assertEqual("READY", payload["readiness"])
        self.assertEqual([], payload["warnings"])
        self.assertEqual(CONTRACT.splitlines(), payload["contract"])
        self.assertEqual(CURRENT_STATE, payload["current_state"])
        self.assertEqual([], payload["transitions"])
        self.assertEqual(LATEST_NEXT_ACTION, payload["next_action"])
        self.assertEqual(LATEST_VERIFICATION, payload["verification"])
        self.assertEqual(
            {
                "candidate": "ABSENT",
                "review": "ABSENT",
                "lock": "ABSENT",
            },
            payload["working_state"],
        )
        self.assertEqual(
            {"status": "NOT_APPLICABLE", "path": None, "historical": False},
            payload["source"],
        )
        for key in ("decisions", "evidence", "blockers", "failures", "unknowns"):
            self.assertIsInstance(payload[key], list)

    def test_resume_warns_and_routes_to_doctor_when_candidate_is_present(self) -> None:
        baseline = json.loads(self.run_contextctl("resume", self.task, "--json").stdout)
        self.run_contextctl("draft", self.task, "--trigger", "phase-verified")

        card = self.run_contextctl("resume", self.task).stdout
        payload = json.loads(self.run_contextctl("resume", self.task, "--json").stdout)

        for key in (
            "schema",
            "task_id",
            "checkpoint",
            "seq",
            "status",
            "contract",
            "current_state",
            "decisions",
            "transitions",
            "next_action",
            "verification",
            "evidence",
            "source",
            "blockers",
            "failures",
            "unknowns",
        ):
            self.assertEqual(baseline[key], payload[key])
        self.assertEqual("WARNING", payload["readiness"])
        self.assertEqual(
            {
                "candidate": "DRAFT",
                "review": "ABSENT",
                "lock": "ABSENT",
            },
            payload["working_state"],
        )
        self.assertTrue(card.startswith("WARNING "))
        self.assertIn("RUN DOCTOR FIRST", card)
        self.assertIn("0002-decision.md", card)
        self.assertIn(LATEST_NEXT_ACTION, card)

    def test_resume_warns_and_routes_to_doctor_for_incomplete_review(self) -> None:
        baseline = json.loads(self.run_contextctl("resume", self.task, "--json").stdout)
        self.run_contextctl("draft", self.task, "--trigger", "phase-verified")
        candidate = self.task / "candidate.md"
        review = self.task / "review.json"
        self.run_contextctl("review-init", candidate, "--output", review)

        card = self.run_contextctl("resume", self.task).stdout
        payload = json.loads(self.run_contextctl("resume", self.task, "--json").stdout)

        for key in (
            "schema",
            "task_id",
            "checkpoint",
            "seq",
            "status",
            "contract",
            "current_state",
            "decisions",
            "transitions",
            "next_action",
            "verification",
            "evidence",
            "source",
            "blockers",
            "failures",
            "unknowns",
        ):
            self.assertEqual(baseline[key], payload[key])
        self.assertEqual("WARNING", payload["readiness"])
        self.assertEqual(
            {
                "candidate": "DRAFT",
                "review": "INCOMPLETE",
                "lock": "ABSENT",
            },
            payload["working_state"],
        )
        self.assertTrue(card.startswith("WARNING "))
        self.assertIn("RUN DOCTOR FIRST", card)
        self.assertIn("0002-decision.md", card)
        self.assertIn(LATEST_NEXT_ACTION, card)


if __name__ == "__main__":
    unittest.main()
