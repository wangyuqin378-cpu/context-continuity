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
DEFAULT_EVIDENCE = (
    "- E001 — `evidence/init.log#sha256=INIT` — supports S001 and W001."
)
NEXT_ACTION = "- W001 — Run the checkpoint guard once."
VERIFICATION = (
    "- E001 — The guard exits successfully and reports the expected sequence."
)


class ContextctlReviewEvidenceRedTeamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.task = self.make_task("evidence-review-task")
        evidence_dir = self.workspace / "evidence"
        evidence_dir.mkdir(parents=True)
        (evidence_dir / "local.log").write_text("verified\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_task(self, task_id: str) -> Path:
        task = self.workspace / ".continuity" / task_id
        task.mkdir(parents=True)
        return task

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

    def initialize_review(
        self, evidence_lines: list[str], task: Path | None = None
    ) -> tuple[Path, Path, dict[str, object]]:
        task = task or self.task
        candidate = task / "candidate.md"
        text = checkpoint(task.name, 1, "none", "init").replace(
            DEFAULT_EVIDENCE, "\n".join(evidence_lines)
        )
        candidate.write_text(text, encoding="utf-8")
        manifest = task / "review.json"
        self.run_contextctl(
            "review-init",
            candidate,
            "--output",
            manifest,
            "--no-source-reason",
            "synthetic evidence red-team fixture",
        )
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        return candidate, manifest, payload

    def write_completed_review(
        self,
        manifest: Path,
        payload: dict[str, object],
        *,
        reviewer_status: str | None = None,
    ) -> None:
        required_ids = list(payload["required_ids"])
        payload.update(
            {
                "reviewer": "fresh-agent-evidence-redteam",
                "reviewed_at": "2026-07-11T15:00:00+08:00",
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
            detected = record.get("detected_status")
            record["status"] = reviewer_status or (
                detected if detected in {"OK", "EXTERNAL"} else "EXTERNAL"
            )
            record["semantic_status"] = "DIRECT"
        manifest.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def evidence_by_id(self, payload: dict[str, object]) -> dict[str, dict[str, object]]:
        evidence_health = payload["evidence_health"]
        self.assertIsInstance(evidence_health, list)
        records: dict[str, dict[str, object]] = {}
        for record in evidence_health:
            self.assertIsInstance(record, dict)
            item_id = record.get("id")
            self.assertIsInstance(item_id, str)
            records[item_id] = record
        return records

    def test_review_init_exposes_machine_detected_status_for_every_evidence_id(
        self,
    ) -> None:
        _, _, payload = self.initialize_review(
            [
                "- E001 — `evidence/local.log` — supports S001.",
                "- E002 — `https://example.invalid/run/2` — supports S001.",
                "- E003 — `evidence/missing.log` — supports S001.",
            ]
        )

        records = self.evidence_by_id(payload)

        self.assertEqual({"E001", "E002", "E003"}, set(records))
        for record in records.values():
            self.assertIn("detected_status", record)

    def test_existing_workspace_relative_pointer_is_machine_detected_ok(self) -> None:
        _, _, payload = self.initialize_review(
            ["- E001 — `evidence/local.log#L1` — supports S001."]
        )

        record = self.evidence_by_id(payload)["E001"]

        self.assertEqual("OK", record["detected_status"])

    def test_uri_and_outside_absolute_pointer_are_machine_detected_external(
        self,
    ) -> None:
        outside = self.root / "outside.log"
        outside.write_text("outside workspace\n", encoding="utf-8")
        _, _, payload = self.initialize_review(
            [
                "- E001 — `https://example.invalid/run/1` — supports S001.",
                f"- E002 — `{outside}` — supports S001.",
            ]
        )

        records = self.evidence_by_id(payload)

        self.assertEqual("EXTERNAL", records["E001"]["detected_status"])
        self.assertEqual("EXTERNAL", records["E002"]["detected_status"])

    def test_relative_and_symlink_escape_are_machine_detected_unsafe(self) -> None:
        outside = self.root / "outside.log"
        outside.write_text("outside workspace\n", encoding="utf-8")
        link = self.workspace / "evidence" / "outside-link.log"
        link.symlink_to(outside)
        _, _, payload = self.initialize_review(
            [
                "- E001 — `../outside.log` — supports S001.",
                "- E002 — `evidence/outside-link.log` — supports S001.",
            ]
        )

        records = self.evidence_by_id(payload)

        self.assertEqual("UNSAFE", records["E001"]["detected_status"])
        self.assertEqual("UNSAFE", records["E002"]["detected_status"])

    def test_missing_relative_pointer_cannot_be_overridden_to_external_pass(
        self,
    ) -> None:
        candidate, manifest, payload = self.initialize_review(
            ["- E001 — `evidence/missing.log` — supports S001."]
        )
        self.assertEqual(
            "MISSING",
            self.evidence_by_id(payload)["E001"]["detected_status"],
        )
        self.write_completed_review(
            manifest, payload, reviewer_status="EXTERNAL"
        )

        blocked = self.run_contextctl(
            "review-check", candidate, manifest, ok=False
        )

        self.assertIn("MISSING", blocked.stdout + blocked.stderr)

    def test_reviewer_cannot_tamper_with_machine_detected_status(self) -> None:
        candidate, manifest, payload = self.initialize_review(
            ["- E001 — `evidence/local.log` — supports S001."]
        )
        self.assertEqual(
            "OK", self.evidence_by_id(payload)["E001"]["detected_status"]
        )
        self.write_completed_review(manifest, payload, reviewer_status="EXTERNAL")
        self.evidence_by_id(payload)["E001"]["detected_status"] = "EXTERNAL"
        manifest.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

        blocked = self.run_contextctl(
            "review-check", candidate, manifest, ok=False
        )

        self.assertRegex(
            blocked.stdout + blocked.stderr,
            r"detected(?:_status| evidence status).+expected OK",
        )

    def test_candidate_edit_and_missing_second_pointer_cannot_bypass_detection(
        self,
    ) -> None:
        candidate, manifest, payload = self.initialize_review(
            ["- E001 — `evidence/local.log` — supports S001."]
        )
        self.write_completed_review(manifest, payload)
        candidate.write_text(
            candidate.read_text(encoding="utf-8") + "\n", encoding="utf-8"
        )

        changed = self.run_contextctl(
            "review-check", candidate, manifest, ok=False
        )
        self.assertRegex(changed.stdout + changed.stderr, r"SHA-256|changed")

        multi_task = self.make_task("multi-pointer-task")
        candidate, manifest, payload = self.initialize_review(
            [
                "- E001 — `evidence/local.log` and `evidence/missing.log` — supports S001."
            ],
            task=multi_task,
        )
        self.assertEqual(
            "MISSING",
            self.evidence_by_id(payload)["E001"]["detected_status"],
        )
        self.write_completed_review(
            manifest, payload, reviewer_status="EXTERNAL"
        )

        blocked = self.run_contextctl(
            "review-check", candidate, manifest, ok=False
        )
        self.assertIn("MISSING", blocked.stdout + blocked.stderr)

    def test_evidence_failure_has_stable_safe_actionable_error_envelope(self) -> None:
        candidate, manifest, payload = self.initialize_review(
            ["- E001 — `evidence/missing.log` — supports S001."]
        )
        self.write_completed_review(
            manifest, payload, reviewer_status="EXTERNAL"
        )

        failure = self.run_contextctl(
            "review-check", candidate, manifest, ok=False
        )
        output = failure.stdout + failure.stderr

        self.assertIn("FAIL CTX301 REVIEW-CHECK_FAILED:", output)
        self.assertIn("SAFETY: UNCHANGED", output)
        self.assertRegex(output, r"NEXT: .+review-init")

    def test_reachable_evidence_with_unsupported_semantics_cannot_pass(self) -> None:
        candidate, manifest, payload = self.initialize_review(
            ["- E001 — `evidence/local.log` — supports S001."]
        )
        self.write_completed_review(manifest, payload)
        self.evidence_by_id(payload)["E001"]["semantic_status"] = "UNSUPPORTED"
        manifest.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

        blocked = self.run_contextctl("review-check", candidate, manifest, ok=False)

        self.assertIn("semantic_status", blocked.stdout + blocked.stderr)


if __name__ == "__main__":
    unittest.main()
