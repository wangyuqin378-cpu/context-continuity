from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_checkpoint_guard import checkpoint


SKILL_ROOT = Path(__file__).parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
CONTEXTCTL = SCRIPTS / "contextctl.py"
GUARD = SCRIPTS / "checkpoint_guard.py"
LOCK_SCHEMA = "context-continuity/lock/v1"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ContextctlDoctorRedTeamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.external_temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.external = Path(self.external_temporary.name)
        (self.workspace / ".continuity").mkdir()
        (self.workspace / "evidence").mkdir()
        (self.workspace / "evidence" / "init.log").write_text(
            "checkpoint initialized\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.external_temporary.cleanup()
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

    def prepared_candidate(
        self, task_id: str, pointer: str = "evidence/init.log#L1"
    ) -> tuple[Path, Path]:
        task = self.workspace / ".continuity" / task_id
        task.mkdir()
        candidate = task / "candidate.md"
        text = checkpoint(task_id, 1, "none", "init").replace(
            "evidence/init.log#sha256=INIT", pointer
        )
        candidate.write_text(text, encoding="utf-8")
        self.run_guard("prepare", candidate)
        return task, candidate

    def publish_task(
        self, task_id: str, pointer: str = "evidence/init.log#L1"
    ) -> tuple[Path, Path]:
        task, candidate = self.prepared_candidate(task_id, pointer)
        self.run_guard("promote", candidate)
        return task, task / "checkpoints" / "0001-init.md"

    def dead_pid(self) -> int:
        process = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        process.wait(timeout=5)
        return process.pid

    def owner_record(
        self,
        task: Path,
        *,
        pid: int | None = None,
        hostname: str | None = None,
        candidate_sha256: str | None = None,
    ) -> dict[str, object]:
        candidate = task / "candidate.md"
        return {
            "schema": LOCK_SCHEMA,
            "pid": self.dead_pid() if pid is None else pid,
            "hostname": socket.gethostname() if hostname is None else hostname,
            "created_at": "2026-07-11T12:00:00+08:00",
            "operation": "publish",
            "process_start": None,
            "nonce": "a" * 32,
            "task_root": str(task.resolve()),
            "candidate_path": str(candidate.resolve()),
            "candidate_sha256": candidate_sha256,
        }

    def write_lock(self, task: Path, owner: dict[str, object] | str) -> Path:
        lock = task / ".checkpoint-write.lock"
        lock.mkdir()
        text = owner if isinstance(owner, str) else json.dumps(owner, sort_keys=True) + "\n"
        (lock / "owner.json").write_text(text, encoding="utf-8")
        return lock

    def doctor_lock_status(self, task: Path) -> str:
        result = self.run_contextctl("doctor", task, "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        return str(json.loads(result.stdout)["checks"]["lock"]["status"])

    def assert_unlock_refused(self, task: Path) -> subprocess.CompletedProcess[str]:
        diagnosis = self.run_contextctl("doctor", task, "--json")
        lock = json.loads(diagnosis.stdout)["checks"]["lock"]
        args: list[object] = ["unlock", task, "--stale"]
        if lock.get("lock_id"):
            args.extend(["--lock-id", lock["lock_id"]])
        result = self.run_contextctl(*args)
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("SAFETY: UNCHANGED", result.stderr)
        return result

    def test_lock_and_owner_symlinks_are_never_followed_or_removed(self) -> None:
        lock_task, _ = self.publish_task("lock-symlink")
        outside_lock = self.external / "outside-lock"
        outside_lock.mkdir()
        outside_sentinel = outside_lock / "sentinel.txt"
        outside_sentinel.write_text("DO-NOT-TOUCH\n", encoding="utf-8")
        lock_link = lock_task / ".checkpoint-write.lock"
        lock_link.symlink_to(outside_lock, target_is_directory=True)
        sentinel_hash = digest(outside_sentinel)

        self.assertEqual("UNKNOWN", self.doctor_lock_status(lock_task))
        self.assert_unlock_refused(lock_task)
        self.assertTrue(lock_link.is_symlink())
        self.assertEqual(outside_lock.resolve(), lock_link.resolve())
        self.assertEqual(sentinel_hash, digest(outside_sentinel))

        owner_task, _ = self.publish_task("owner-symlink")
        owner_lock = owner_task / ".checkpoint-write.lock"
        owner_lock.mkdir()
        outside_owner = self.external / "owner.json"
        outside_owner.write_text(
            json.dumps(self.owner_record(owner_task), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        owner_link = owner_lock / "owner.json"
        owner_link.symlink_to(outside_owner)
        owner_hash = digest(outside_owner)

        self.assertEqual("UNKNOWN", self.doctor_lock_status(owner_task))
        self.assert_unlock_refused(owner_task)
        self.assertTrue(owner_link.is_symlink())
        self.assertEqual(outside_owner.resolve(), owner_link.resolve())
        self.assertEqual(owner_hash, digest(outside_owner))

    def test_foreign_malformed_and_duplicate_owner_metadata_refuse_unlock(self) -> None:
        dead_pid = self.dead_pid()
        duplicate = (
            '{"schema":"context-continuity/lock/v1",'
            f'"pid":{dead_pid},"pid":{dead_pid},'
            f'"hostname":{json.dumps(socket.gethostname())}}}\n'
        )
        cases: tuple[tuple[str, dict[str, object] | str], ...] = (
            (
                "foreign-owner",
                self.owner_record(
                    self.workspace / ".continuity" / "foreign-owner",
                    pid=dead_pid,
                    hostname="foreign-host.invalid",
                ),
            ),
            ("malformed-owner", "{not-json\n"),
            ("duplicate-owner", duplicate),
        )

        for task_id, owner in cases:
            with self.subTest(task_id=task_id):
                task, latest = self.publish_task(task_id)
                lock = self.write_lock(task, owner)
                owner_path = lock / "owner.json"
                owner_hash = digest(owner_path)
                checkpoint_hash = digest(latest)

                self.assertEqual("UNKNOWN", self.doctor_lock_status(task))
                self.assert_unlock_refused(task)
                self.assertTrue(lock.is_dir())
                self.assertEqual(owner_hash, digest(owner_path))
                self.assertEqual(checkpoint_hash, digest(latest))

    def test_candidate_without_matching_owner_hash_refuses_unlock(self) -> None:
        for mode in ("missing", "mismatch"):
            with self.subTest(mode=mode):
                task, latest = self.publish_task(f"candidate-{mode}")
                candidate = task / "candidate.md"
                candidate.write_text("candidate still being edited\n", encoding="utf-8")
                owner = self.owner_record(task)
                if mode == "missing":
                    owner.pop("candidate_sha256")
                else:
                    owner["candidate_sha256"] = hashlib.sha256(b"different").hexdigest()
                lock = self.write_lock(task, owner)
                candidate_hash = digest(candidate)
                checkpoint_hash = digest(latest)

                self.assertEqual("STALE", self.doctor_lock_status(task))
                refused = self.assert_unlock_refused(task)
                self.assertIn("candidate", refused.stderr.casefold())
                self.assertTrue(lock.is_dir())
                self.assertEqual(candidate_hash, digest(candidate))
                self.assertEqual(checkpoint_hash, digest(latest))

    def test_corrupt_checkpoint_chain_refuses_unlock(self) -> None:
        task, latest = self.publish_task("corrupt-chain")
        latest.write_text(
            "---\nschema: context-continuity/v1\n---\n", encoding="utf-8"
        )
        lock = self.write_lock(task, self.owner_record(task))
        owner_hash = digest(lock / "owner.json")
        corrupt_hash = digest(latest)

        self.assertEqual("STALE", self.doctor_lock_status(task))
        self.assert_unlock_refused(task)
        self.assertTrue(lock.is_dir())
        self.assertEqual(owner_hash, digest(lock / "owner.json"))
        self.assertEqual(corrupt_hash, digest(latest))

    def test_list_tasks_skips_symlinks_and_continues_past_broken_tasks(self) -> None:
        self.publish_task("healthy-task")
        broken = self.workspace / ".continuity" / "broken-task"
        (broken / "checkpoints").mkdir(parents=True)
        (broken / "checkpoints" / "0001-init.md").write_text(
            "not a checkpoint\n", encoding="utf-8"
        )
        outside_task = self.external / "outside-task"
        outside_task.mkdir()
        outside_sentinel = outside_task / "sentinel.txt"
        outside_sentinel.write_text("DO-NOT-SCAN\n", encoding="utf-8")
        task_link = self.workspace / ".continuity" / "linked-task"
        task_link.symlink_to(outside_task, target_is_directory=True)
        sentinel_hash = digest(outside_sentinel)

        result = self.run_contextctl("list-tasks", self.workspace, "--json")

        self.assertEqual(0, result.returncode, result.stderr)
        tasks = {item["task_id"]: item for item in json.loads(result.stdout)["tasks"]}
        self.assertEqual({"broken-task", "healthy-task"}, set(tasks))
        self.assertEqual("BROKEN", tasks["broken-task"]["readiness"])
        self.assertIn("frontmatter", tasks["broken-task"]["error"])
        self.assertEqual("READY", tasks["healthy-task"]["readiness"])
        self.assertTrue(task_link.is_symlink())
        self.assertEqual(sentinel_hash, digest(outside_sentinel))

    def test_evidence_symlink_escape_is_never_ready_or_read(self) -> None:
        secret = self.external / "secret.txt"
        secret.write_text("OUTSIDE-SECRET-MUST-NOT-APPEAR\n", encoding="utf-8")
        evidence_link = self.workspace / "evidence" / "escape.log"
        evidence_link.symlink_to(secret)
        task, _ = self.publish_task(
            "evidence-symlink", pointer="evidence/escape.log#L1"
        )

        result = self.run_contextctl("resume", task, "--json")

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertNotEqual("READY", payload["readiness"])
        self.assertEqual("UNSAFE", payload["evidence"][0]["status"])
        self.assertIsNone(payload["evidence"][0]["resolved_path"])
        self.assertNotIn("OUTSIDE-SECRET-MUST-NOT-APPEAR", result.stdout)

    def test_guard_created_lock_owner_is_bound_and_cleaned_after_publish(self) -> None:
        task, candidate = self.prepared_candidate("guard-owner")
        candidate_hash = digest(candidate)
        helper = "\n".join(
            (
                "import sys",
                "from pathlib import Path",
                f"sys.path.insert(0, {str(SCRIPTS)!r})",
                "import checkpoint_guard as guard",
                "original = guard.atomic_publish",
                "def paused(target, text):",
                "    print('LOCK_READY', flush=True)",
                "    sys.stdin.readline()",
                "    return original(target, text)",
                "guard.atomic_publish = paused",
                "guard.promote(Path(sys.argv[1]))",
            )
        )
        process = subprocess.Popen(
            [sys.executable, "-c", helper, str(candidate)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None
        try:
            ready = process.stdout.readline()
            self.assertEqual("LOCK_READY\n", ready)
            lock = task / ".checkpoint-write.lock"
            owner_path = lock / "owner.json"
            self.assertTrue(lock.is_dir())
            self.assertTrue(owner_path.is_file())
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
            self.assertEqual(LOCK_SCHEMA, owner["schema"])
            self.assertEqual(process.pid, owner["pid"])
            self.assertRegex(str(owner["nonce"]), r"\A[0-9a-f]{32}\Z")
            self.assertEqual(candidate_hash, owner["candidate_sha256"])
            self.assertEqual(str(task.resolve()), owner["task_root"])
            self.assertEqual(str(candidate.resolve()), owner["candidate_path"])
            self.assertIn("process_start", owner)
            self.assertTrue(
                owner["process_start"] is None
                or isinstance(owner["process_start"], str)
            )
        finally:
            remaining_stdout, stderr = process.communicate(input="\n", timeout=10)

        self.assertEqual(
            0,
            process.returncode,
            ready + remaining_stdout + stderr,
        )
        self.assertFalse((task / ".checkpoint-write.lock").exists())
        self.assertFalse(candidate.exists())
        self.assertTrue((task / "checkpoints" / "0001-init.md").is_file())


if __name__ == "__main__":
    unittest.main()
