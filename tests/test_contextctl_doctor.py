from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_checkpoint_guard import checkpoint


SKILL_ROOT = Path(__file__).parents[1]
CONTEXTCTL = SKILL_ROOT / "scripts" / "contextctl.py"
GUARD = SKILL_ROOT / "scripts" / "checkpoint_guard.py"
LOCK_SCHEMA = "context-continuity/lock/v1"
DOCTOR_SCHEMA = "context-continuity/doctor/v1"
TASK_LIST_SCHEMA = "context-continuity/tasks/v1"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ContextctlDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        (self.workspace / ".continuity").mkdir()
        (self.workspace / "evidence").mkdir()
        (self.workspace / "evidence" / "init.log").write_text(
            "checkpoint initialized\n", encoding="utf-8"
        )

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

    def publish_task(
        self, task_id: str, **checkpoint_options: object
    ) -> tuple[Path, Path]:
        task = self.workspace / ".continuity" / task_id
        task.mkdir()
        candidate = task / "candidate.md"
        text = checkpoint(
            task_id, 1, "none", "init", **checkpoint_options
        ).replace("evidence/init.log#sha256=INIT", "evidence/init.log#L1")
        candidate.write_text(text, encoding="utf-8")
        self.run_guard("prepare", candidate)
        self.run_guard("promote", candidate)
        return task, task / "checkpoints" / "0001-init.md"

    def write_lock(self, task: Path, pid: int | None) -> Path:
        lock = task / ".checkpoint-write.lock"
        lock.mkdir()
        if pid is not None:
            metadata = {
                "schema": LOCK_SCHEMA,
                "pid": pid,
                "hostname": socket.gethostname(),
                "created_at": "2026-07-11T12:00:00+08:00",
                "operation": "publish",
            }
            (lock / "owner.json").write_text(
                json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8"
            )
        return lock

    def dead_pid(self) -> int:
        process = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        process.wait(timeout=5)
        return process.pid

    def assert_actionable_failure(
        self, result: subprocess.CompletedProcess[str]
    ) -> str:
        combined = result.stdout + result.stderr
        self.assertNotEqual(0, result.returncode, combined)
        match = re.search(r"\bFAIL (CTX\d{3})\b", combined)
        self.assertIsNotNone(match, combined)
        self.assertIn("SAFETY:", combined)
        self.assertRegex(combined, r"SAFETY:\s*UNCHANGED\b")
        self.assertRegex(combined, r"NEXT:\s*\S.+")
        assert match is not None
        return match.group(1)

    def test_doctor_reports_all_health_dimensions_in_human_and_json_output(
        self,
    ) -> None:
        task, latest = self.publish_task("healthy-task")

        human = self.run_contextctl("doctor", task)
        self.assertEqual(0, human.returncode, human.stderr)
        for line in (
            "READY · healthy-task",
            "CHAIN: OK",
            "LATEST: OK",
            "SOURCE: NOT_APPLICABLE",
            "EVIDENCE: OK",
            "CANDIDATE: ABSENT",
            "REVIEW: ABSENT",
            "LOCK: ABSENT",
        ):
            self.assertIn(line, human.stdout)

        result = self.run_contextctl("doctor", task, "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(DOCTOR_SCHEMA, payload["schema"])
        self.assertEqual("healthy-task", payload["task_id"])
        self.assertEqual("READY", payload["readiness"])
        self.assertEqual(
            {
                "chain",
                "latest",
                "source",
                "evidence",
                "candidate",
                "review",
                "lock",
            },
            set(payload["checks"]),
        )
        checks = payload["checks"]
        self.assertEqual("OK", checks["chain"]["status"])
        self.assertEqual(1, checks["chain"]["checkpoints"])
        self.assertEqual("OK", checks["latest"]["status"])
        self.assertEqual(latest.name, checks["latest"]["checkpoint"])
        self.assertEqual(1, checks["latest"]["seq"])
        self.assertEqual("OK", checks["evidence"]["status"])
        self.assertEqual(1, checks["evidence"]["ok"])
        self.assertEqual(0, checks["evidence"]["missing"])
        self.assertEqual("NOT_APPLICABLE", checks["source"]["status"])
        self.assertFalse(checks["source"]["historical"])
        self.assertEqual("ABSENT", checks["candidate"]["status"])
        self.assertEqual("ABSENT", checks["review"]["status"])
        self.assertEqual("ABSENT", checks["lock"]["status"])

    def test_doctor_routes_draft_review_and_publish_lifecycle(self) -> None:
        task, _ = self.publish_task("working-lifecycle")
        drafted = self.run_contextctl("draft", task, "--trigger", "phase-verified")
        self.assertEqual(0, drafted.returncode, drafted.stderr)
        candidate = task / "candidate.md"

        draft_health = json.loads(
            self.run_contextctl("doctor", task, "--json").stdout
        )
        self.assertEqual("DRAFTING", draft_health["readiness"])
        self.assertEqual("DRAFT", draft_health["checks"]["candidate"]["status"])
        self.assertEqual("ABSENT", draft_health["checks"]["review"]["status"])
        self.assertIn("review-init", draft_health["next"])

        review = task / "review.json"
        initialized = self.run_contextctl(
            "review-init", candidate, "--output", review
        )
        self.assertEqual(0, initialized.returncode, initialized.stderr)
        review_health = json.loads(
            self.run_contextctl("doctor", task, "--json").stdout
        )
        self.assertEqual("REVIEWING", review_health["readiness"])
        self.assertEqual("INCOMPLETE", review_health["checks"]["review"]["status"])
        self.assertIn("review-check", review_health["next"])

        payload = json.loads(review.read_text(encoding="utf-8"))
        candidate_text = candidate.read_text(encoding="utf-8")
        next_action = re.search(
            r"## Next Action\n\n(- .+)", candidate_text
        )
        verification = re.search(
            r"## Verification for Next Action\n\n(- .+)", candidate_text
        )
        assert next_action is not None and verification is not None
        payload.update(
            {
                "reviewer": "fresh-doctor-test",
                "reviewed_at": "2026-07-11T13:00:00+08:00",
                "recovered_ids": payload["required_ids"],
                "missing_ids": [],
                "recovered_next_action": next_action.group(1),
                "recovered_verification": verification.group(1),
                "verdict": "PASS",
            }
        )
        for record in payload["evidence_health"]:
            record["status"] = record["detected_status"]
            record["semantic_status"] = "DIRECT"
        review.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        reviewed_health = json.loads(
            self.run_contextctl("doctor", task, "--json").stdout
        )
        self.assertEqual("REVIEWED", reviewed_health["readiness"])
        self.assertEqual("PREPARED", reviewed_health["checks"]["candidate"]["status"])
        self.assertEqual("PASS", reviewed_health["checks"]["review"]["status"])
        self.assertIn("publish", reviewed_health["next"])

    def test_doctor_distinguishes_active_stale_and_unknown_locks(self) -> None:
        dead_pid = self.dead_pid()
        cases = (
            ("active-lock", os.getpid(), "ACTIVE"),
            ("stale-lock", dead_pid, "STALE"),
            ("unknown-lock", None, "UNKNOWN"),
        )

        for task_id, owner_pid, expected_status in cases:
            with self.subTest(status=expected_status):
                task, _ = self.publish_task(task_id)
                lock = self.write_lock(task, owner_pid)

                result = self.run_contextctl("doctor", task, "--json")
                payload = json.loads(result.stdout)
                lock_health = payload["checks"]["lock"]

                self.assertEqual(expected_status, lock_health["status"])
                if owner_pid is not None:
                    self.assertEqual(owner_pid, lock_health["pid"])
                self.assertTrue(lock.is_dir(), "doctor must never mutate a lock")

    def test_unlock_stale_removes_only_a_proven_stale_lock(self) -> None:
        stale_task, stale_latest = self.publish_task("stale-unlock")
        stale_lock = self.write_lock(stale_task, self.dead_pid())
        checkpoint_hash = digest(stale_latest)

        unlocked = self.run_contextctl("unlock", stale_task, "--stale")

        self.assertEqual(0, unlocked.returncode, unlocked.stderr)
        self.assertIn("UNLOCKED", unlocked.stdout)
        self.assertFalse(stale_lock.exists())
        self.assertEqual(checkpoint_hash, digest(stale_latest))

        refusal_codes: list[str] = []
        for task_id, owner_pid in (
            ("active-unlock", os.getpid()),
            ("unknown-unlock", None),
        ):
            with self.subTest(task_id=task_id):
                task, latest = self.publish_task(task_id)
                lock = self.write_lock(task, owner_pid)
                before = digest(latest)

                refused = self.run_contextctl("unlock", task, "--stale")

                refusal_codes.append(self.assert_actionable_failure(refused))
                self.assertTrue(lock.is_dir())
                self.assertEqual(before, digest(latest))

        self.assertEqual(refusal_codes[0], refusal_codes[1])

    def test_list_tasks_uses_checkpoint_state_not_mtime_as_authority(self) -> None:
        alpha, alpha_latest = self.publish_task("alpha")
        zeta, zeta_latest = self.publish_task(
            "zeta",
            status="complete",
            include_w001=False,
            next_action="- None — task complete.",
            verification="- E001 — Final acceptance evidence is recorded.",
        )

        human = self.run_contextctl("list-tasks", self.workspace)
        self.assertEqual(0, human.returncode, human.stderr)
        self.assertIn("alpha", human.stdout)
        self.assertIn("zeta", human.stdout)
        self.assertIn("W001 — Run the checkpoint guard once.", human.stdout)

        before = self.run_contextctl("list-tasks", self.workspace, "--json")
        self.assertEqual(0, before.returncode, before.stderr)
        before_payload = json.loads(before.stdout)
        self.assertEqual(TASK_LIST_SCHEMA, before_payload["schema"])
        self.assertEqual(
            ["alpha", "zeta"],
            [task["task_id"] for task in before_payload["tasks"]],
        )
        for task in before_payload["tasks"]:
            self.assertTrue(
                {"task_id", "status", "seq", "next_action", "readiness"}
                <= set(task)
            )
        alpha_payload, zeta_payload = before_payload["tasks"]
        self.assertEqual("active", alpha_payload["status"])
        self.assertEqual(1, alpha_payload["seq"])
        self.assertEqual(
            "- W001 — Run the checkpoint guard once.",
            alpha_payload["next_action"],
        )
        self.assertEqual("READY", alpha_payload["readiness"])
        self.assertEqual("complete", zeta_payload["status"])
        self.assertEqual(1, zeta_payload["seq"])
        self.assertEqual("- None — task complete.", zeta_payload["next_action"])

        os.utime(alpha, (2_000_000_000, 2_000_000_000))
        os.utime(alpha_latest, (2_000_000_000, 2_000_000_000))
        os.utime(zeta, (1, 1))
        os.utime(zeta_latest, (1, 1))

        after = self.run_contextctl("list-tasks", self.workspace, "--json")
        self.assertEqual(0, after.returncode, after.stderr)
        self.assertEqual(before_payload, json.loads(after.stdout))

    def test_doctor_failure_has_stable_code_safety_and_next_step(self) -> None:
        task, latest = self.publish_task("corrupt-task")
        latest.write_text(
            "---\nschema: context-continuity/v1\n---\n", encoding="utf-8"
        )
        corrupt_hash = digest(latest)

        first = self.run_contextctl("doctor", task)
        second = self.run_contextctl("doctor", task)

        first_code = self.assert_actionable_failure(first)
        second_code = self.assert_actionable_failure(second)
        self.assertEqual(first_code, second_code)
        self.assertEqual(corrupt_hash, digest(latest))


if __name__ == "__main__":
    unittest.main()
