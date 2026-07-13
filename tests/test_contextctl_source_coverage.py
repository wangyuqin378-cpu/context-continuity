from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from test_checkpoint_guard import checkpoint


SKILL_ROOT = Path(__file__).parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
CONTEXTCTL = SCRIPTS / "contextctl.py"
GUARD = SCRIPTS / "checkpoint_guard.py"
REVIEW_SCHEMA_V2 = "context-continuity/review/v2"
NEXT_ACTION = "- W001 — Run the checkpoint guard once."
VERIFICATION = (
    "- E001 — The guard exits successfully and reports the expected sequence."
)
NO_SOURCE_REASON = "The initial contract was entered directly; no prior source artifact exists."

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import contextctl as contextctl_module  # noqa: E402


def section(text: str, heading: str) -> str:
    start = text.index(heading) + len(heading)
    match = re.search(r"^## ", text[start:], re.MULTILINE)
    end = start + match.start() if match else len(text)
    return text[start:end].strip()


def frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    end = lines.index("---", 1)
    return {
        key.strip(): value.strip()
        for key, value in (line.split(":", 1) for line in lines[1:end] if line)
    }


class ContextctlSourceCoverageTests(unittest.TestCase):
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

    def make_task(self, task_id: str) -> Path:
        task = self.workspace / ".continuity" / task_id
        task.mkdir(parents=True)
        return task

    def make_initial_candidate(self, task_id: str = "source-coverage-task") -> Path:
        task = self.make_task(task_id)
        candidate = task / "candidate.md"
        candidate.write_text(
            checkpoint(task_id, 1, "none", "init"), encoding="utf-8"
        )
        return candidate

    def write_source(self, name: str, *, source_only_id: str | None = None) -> Path:
        extra = f"\n- {source_only_id} Constraint: preserve the source-only boundary." if source_only_id else ""
        source = self.workspace / name
        source.write_text(
            "# Stable initial source\n\n"
            "- G001 Goal: Create one safe checkpoint.\n"
            "- C001 Constraint: Preserve the exact task boundary.\n"
            "- W001 Work: Run the checkpoint guard once.\n"
            "- E001 Evidence: evidence/init.log supports the current state."
            f"{extra}\n",
            encoding="utf-8",
        )
        return source

    def run_contextctl(self, *args: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CONTEXTCTL), *(str(arg) for arg in args)],
            text=True,
            capture_output=True,
            check=False,
        )

    def run_guard(self, *args: object) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(GUARD), *(str(arg) for arg in args)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result

    def review_init(
        self,
        candidate: Path,
        *,
        source: Path | None = None,
        no_source_reason: str | None = None,
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
        if no_source_reason is not None:
            args.extend(["--no-source-reason", no_source_reason])
        return manifest, self.run_contextctl(*args)

    def complete_review(
        self,
        candidate: Path,
        manifest: Path,
        *,
        complete_source_review: bool = True,
    ) -> dict[str, object]:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        candidate_text = candidate.read_text(encoding="utf-8")
        required_ids = list(payload["required_ids"])
        payload.update(
            {
                "reviewer": "fresh-source-coverage-reviewer",
                "reviewed_at": "2026-07-12T00:00:00+08:00",
                "recovered_ids": required_ids,
                "missing_ids": [],
                "contradictions": [],
                "ambiguities": [],
                "recovered_next_action": section(candidate_text, "## Next Action"),
                "recovered_verification": section(
                    candidate_text, "## Verification for Next Action"
                ),
                "verdict": "PASS",
            }
        )
        for record in payload["evidence_health"]:
            record["status"] = record["detected_status"]
            record["semantic_status"] = "DIRECT"
        source_review = payload.get("source_review")
        if complete_source_review and isinstance(source_review, dict):
            source_ids = list(source_review["required_ids"])
            source_review.update(
                {
                    "recovered_ids": source_ids,
                    "missing_ids": [],
                    "coverage_status": "COMPLETE",
                    "omissions": [],
                }
            )
        manifest.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return payload

    def publish_initial_with_guard(self, task_id: str) -> tuple[Path, Path]:
        candidate = self.make_initial_candidate(task_id)
        self.run_guard("prepare", candidate)
        self.run_guard("promote", candidate)
        return candidate.parent, candidate.parent / "checkpoints" / "0001-init.md"

    def assert_source_failure(self, result: subprocess.CompletedProcess[str]) -> None:
        output = result.stdout + result.stderr
        self.assertNotEqual(0, result.returncode, output)
        self.assertRegex(output, r"(?i)source")

    def test_initial_review_requires_exactly_one_source_provenance_mode(self) -> None:
        candidate = self.make_initial_candidate("missing-source-mode")

        manifest, missing = self.review_init(candidate)

        self.assert_source_failure(missing)
        missing_output = missing.stdout + missing.stderr
        self.assertIn("--source <path>", missing_output)
        self.assertIn("--no-source-reason <reason>", missing_output)
        self.assertFalse(manifest.exists())

        candidate = self.make_initial_candidate("duplicate-source-mode")
        source = self.write_source("duplicate-source.md")
        manifest, duplicate = self.review_init(
            candidate,
            source=source,
            no_source_reason=NO_SOURCE_REASON,
        )

        self.assert_source_failure(duplicate)
        self.assertRegex(
            duplicate.stdout + duplicate.stderr,
            r"(?i)(exactly one|mutually exclusive|either.+or|not allowed with)",
        )
        self.assertFalse(manifest.exists())

    def test_source_manifest_v2_detects_ids_and_orders_reviewer_work(self) -> None:
        candidate = self.make_initial_candidate()
        source = self.write_source("bound-source.md", source_only_id="C009")

        manifest, result = self.review_init(candidate, source=source)

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(REVIEW_SCHEMA_V2, payload["schema"])
        self.assertIn("source_detected_ids", payload)
        self.assertEqual(
            ["C001", "C009", "E001", "G001", "W001"],
            payload["source_detected_ids"],
        )
        self.assertEqual(
            payload["source_detected_ids"], payload["source_review"]["required_ids"]
        )
        self.assertEqual([], payload["source_review"]["recovered_ids"])
        self.assertEqual(
            payload["source_detected_ids"], payload["source_review"]["missing_ids"]
        )
        self.assertIn(
            payload["source_review"]["coverage_status"],
            {"INCOMPLETE", "UNCHECKED"},
        )
        instructions = " ".join(payload["instructions"]).casefold()
        self.assertLess(instructions.index("bound source"), instructions.index("candidate"))
        self.assertIn("source-only", instructions)
        self.assertIn("durable", instructions)

    def test_review_check_requires_complete_source_coverage(self) -> None:
        cases = (
            {
                "recovered_ids": ["G001"],
                "missing_ids": ["C001"],
                "coverage_status": "INCOMPLETE",
                "omissions": [],
            },
            {
                "recovered_ids": ["C001", "G001"],
                "missing_ids": [],
                "coverage_status": "COMPLETE",
                "omissions": ["C009 source-only constraint is absent from candidate"],
            },
        )
        for index, source_review in enumerate(cases, start=1):
            with self.subTest(case=index):
                candidate = self.make_initial_candidate(f"incomplete-source-review-{index}")
                source = self.write_source(f"incomplete-source-{index}.md")
                manifest, initialized = self.review_init(candidate, source=source)
                self.assertEqual(0, initialized.returncode, initialized.stderr)
                payload = self.complete_review(
                    candidate, manifest, complete_source_review=False
                )
                required_source_ids = list(payload["source_review"]["required_ids"])
                source_review = dict(source_review)
                if source_review["coverage_status"] == "INCOMPLETE":
                    source_review["recovered_ids"] = required_source_ids[:-1]
                    source_review["missing_ids"] = required_source_ids[-1:]
                else:
                    source_review["recovered_ids"] = required_source_ids
                    source_review["missing_ids"] = []
                payload["source_review"] = {
                    "required_ids": required_source_ids,
                    **source_review,
                }
                manifest.write_text(
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
                    + "\n",
                    encoding="utf-8",
                )

                checked = self.run_contextctl("review-check", candidate, manifest)
                output = checked.stdout + checked.stderr

                self.assertNotEqual(0, checked.returncode, output)
                self.assertRegex(
                    output, r"(?i)source.+(coverage|missing|omission|recover)"
                )

    def test_review_check_accepts_complete_source_coverage(self) -> None:
        candidate = self.make_initial_candidate("complete-source-review")
        source = self.write_source("complete-source.md")
        manifest, initialized = self.review_init(candidate, source=source)
        self.assertEqual(0, initialized.returncode, initialized.stderr)
        payload = self.complete_review(candidate, manifest)

        self.assertEqual(REVIEW_SCHEMA_V2, payload["schema"])
        checked = self.run_contextctl("review-check", candidate, manifest)

        self.assertEqual(0, checked.returncode, checked.stderr)
        self.assertIn("PASS", checked.stdout)

    def test_no_source_reason_is_bound_as_explicit_provenance(self) -> None:
        candidate = self.make_initial_candidate("explicit-no-source")

        manifest, initialized = self.review_init(
            candidate, no_source_reason=NO_SOURCE_REASON
        )

        self.assertEqual(0, initialized.returncode, initialized.stderr)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(REVIEW_SCHEMA_V2, payload["schema"])
        self.assertIsInstance(payload["initial_source"], dict)
        self.assertFalse(payload["initial_source"]["available"])
        self.assertEqual(NO_SOURCE_REASON, payload["initial_source"]["reason"])
        self.assertEqual([], payload.get("source_detected_ids", []))

    def test_non_initial_review_rejects_both_source_modes(self) -> None:
        task, _ = self.publish_initial_with_guard("non-initial-source-mode")
        drafted = self.run_contextctl("draft", task, "--trigger", "phase-verified")
        self.assertEqual(0, drafted.returncode, drafted.stderr)
        candidate = task / "candidate.md"
        source = self.write_source("non-initial-source.md")

        for label, args in (
            ("source", ["--source", source]),
            ("reason", ["--no-source-reason", NO_SOURCE_REASON]),
        ):
            with self.subTest(mode=label):
                manifest = task / f"review-{label}.json"
                result = self.run_contextctl(
                    "review-init", candidate, "--output", manifest, *args
                )
                output = result.stdout + result.stderr
                self.assertNotEqual(0, result.returncode, output)
                self.assertRegex(output, r"(?i)(initial|sequence|seq)")
                self.assertFalse(manifest.exists())

    def test_review_check_rejects_candidate_that_resume_cannot_project(self) -> None:
        candidate = self.make_initial_candidate("unresumable-candidate")
        candidate.write_text(
            candidate.read_text(encoding="utf-8").replace(
                "- O001 Out of scope: Global host hook installation.",
                "- O001 Out of scope: Global host hook installation.\n"
                "  PERMISSION-MUST-SURVIVE: never rotate production credentials.",
            ),
            encoding="utf-8",
        )
        source = self.write_source("unresumable-source.md")
        manifest, initialized = self.review_init(candidate, source=source)
        output = initialized.stdout + initialized.stderr

        self.assertNotEqual(0, initialized.returncode, output)
        self.assertFalse(manifest.exists())
        self.assertRegex(output, r"(?i)(resume|projection|cannot project)")

    def test_complete_command_creates_exact_resumable_candidate(self) -> None:
        task, _ = self.publish_initial_with_guard("high-level-complete")

        completed = self.run_contextctl("complete", task)

        self.assertEqual(0, completed.returncode, completed.stderr)
        candidate = task / "candidate.md"
        text = candidate.read_text(encoding="utf-8")
        self.assertEqual("complete", frontmatter(text)["status"])
        self.assertEqual("- None — task complete.", section(text, "## Next Action"))
        self.assertNotIn("W001", section(text, "## Current State"))
        blockers = section(text, "## Blockers and Open Questions")
        self.assertIn("B002", blockers)
        self.assertNotIn("B001", blockers)
        self.assertIn("no blocker or open question remains", blockers)
        delta = section(text, "## Delta From Previous")
        self.assertIn("W001", delta)
        self.assertIn("B001 -> B002", delta)
        self.assertIn("task status became complete", delta)
        self.assertNotIn("initial checkpoint", delta)
        self.assertRegex(
            delta,
            r"Resolved or cancelled:.*\bW001\b",
        )
        self.assertIn("E001", section(text, "## Verification for Next Action"))

        manifest, initialized = self.review_init(candidate)
        self.assertEqual(0, initialized.returncode, initialized.stderr)
        self.complete_review(candidate, manifest)
        checked = self.run_contextctl("review-check", candidate, manifest)
        self.assertEqual(0, checked.returncode, checked.stderr)
        published = self.run_contextctl("publish", candidate, "--review", manifest)
        self.assertEqual(0, published.returncode, published.stderr)
        resumed = self.run_contextctl("resume", task, "--json")
        self.assertEqual(0, resumed.returncode, resumed.stderr)
        self.assertEqual("complete", json.loads(resumed.stdout)["status"])

    def test_complete_allocator_uses_full_history_and_clean_none_transition(self) -> None:
        task = self.make_task("complete-history")
        first = task / "candidate.md"
        first.write_text(
            checkpoint(
                task.name,
                1,
                "none",
                "init",
                blocker=(
                    "- B001 — current blocker placeholder.\n"
                    "- B009 — historical blocker that will be resolved."
                ),
            ),
            encoding="utf-8",
        )
        self.run_guard("prepare", first)
        self.run_guard("promote", first)
        second = task / "candidate.md"
        second.write_text(
            checkpoint(
                task.name,
                2,
                "0001-init.md",
                "phase-verified",
                blocker="- B001 — current blocker placeholder.",
                delta=(
                    "- Added: none.\n"
                    "- Changed: none.\n"
                    "- Superseded: none.\n"
                    "- Resolved or cancelled: B009 — historical blocker closed.\n"
                    "- Compressed or dropped: none.\n"
                    "- Authorized by: E001.\n"
                    "- Growth reason: Retained the explicit B009 resolution record."
                ),
            ),
            encoding="utf-8",
        )
        self.run_guard(
            "prepare",
            second,
            "--previous",
            task / "checkpoints/0001-init.md",
        )
        self.run_guard("promote", second)

        completed = self.run_contextctl("complete", task)

        self.assertEqual(0, completed.returncode, completed.stderr)
        text = (task / "candidate.md").read_text(encoding="utf-8")
        self.assertIn("B010", section(text, "## Blockers and Open Questions"))
        self.assertIn("B001 -> B010", section(text, "## Delta From Previous"))
        self.assertNotIn("B009 -> B010", section(text, "## Delta From Previous"))

        no_blocker_task = self.make_task("complete-no-active-blocker")
        no_blocker = no_blocker_task / "candidate.md"
        no_blocker.write_text(
            checkpoint(
                no_blocker_task.name,
                1,
                "none",
                "init",
                blocker="- No blocker or open question is active.",
            ),
            encoding="utf-8",
        )
        self.run_guard("prepare", no_blocker)
        self.run_guard("promote", no_blocker)

        completed = self.run_contextctl("complete", no_blocker_task)

        self.assertEqual(0, completed.returncode, completed.stderr)
        text = (no_blocker_task / "candidate.md").read_text(encoding="utf-8")
        self.assertIn("B001", section(text, "## Blockers and Open Questions"))
        self.assertIn("- Superseded: none.", section(text, "## Delta From Previous"))
        self.assertNotIn("none ->", section(text, "## Delta From Previous"))

    def test_complete_b999_fails_before_creating_candidate(self) -> None:
        task = self.make_task("complete-b999")
        first = task / "candidate.md"
        first.write_text(
            checkpoint(
                task.name,
                1,
                "none",
                "init",
                blocker="- B999 — final available blocker identity.",
            ),
            encoding="utf-8",
        )
        self.run_guard("prepare", first)
        self.run_guard("promote", first)
        second = task / "candidate.md"
        second.write_text(
            checkpoint(
                task.name,
                2,
                "0001-init.md",
                "phase-verified",
                blocker="- No blocker or open question is active.",
                delta=(
                    "- Added: none.\n"
                    "- Changed: none.\n"
                    "- Superseded: none.\n"
                    "- Resolved or cancelled: B999 — historical blocker closed.\n"
                    "- Compressed or dropped: none.\n"
                    "- Authorized by: E001.\n"
                    "- Growth reason: Retained the explicit B999 resolution record."
                ),
            ),
            encoding="utf-8",
        )
        self.run_guard(
            "prepare",
            second,
            "--previous",
            task / "checkpoints/0001-init.md",
        )
        self.run_guard("promote", second)

        completed = self.run_contextctl("complete", task)

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("B999", completed.stdout + completed.stderr)
        self.assertFalse((task / "candidate.md").exists())

    def test_complete_rejects_checkpoint_added_between_allocator_audit_and_draft(
        self,
    ) -> None:
        task, first_checkpoint = self.publish_initial_with_guard(
            "complete-allocation-race"
        )
        real_audit = contextctl_module.audited_continuity_chain
        audit_count = 0

        def audit_with_competing_checkpoint(task_root: Path):
            nonlocal audit_count
            audit_count += 1
            if audit_count == 2:
                competitor = task_root / "candidate.md"
                competitor.write_text(
                    checkpoint(
                        task_root.name,
                        2,
                        first_checkpoint.name,
                        "phase-verified",
                        blocker="- B002 — competing writer's active blocker.",
                        delta=(
                            "- Added: none.\n"
                            "- Changed: none.\n"
                            "- Superseded: B001 -> B002 — competing writer advanced the blocker.\n"
                            "- Resolved or cancelled: none.\n"
                            "- Compressed or dropped: none.\n"
                            "- Authorized by: E001.\n"
                            "- Growth reason: Preserve the concurrent state transition."
                        ),
                    ),
                    encoding="utf-8",
                )
                self.run_guard(
                    "prepare", competitor, "--previous", first_checkpoint
                )
                self.run_guard("promote", competitor)
            return real_audit(task_root)

        failure: Exception | None = None
        with mock.patch.object(
            contextctl_module,
            "audited_continuity_chain",
            side_effect=audit_with_competing_checkpoint,
        ):
            try:
                contextctl_module.complete_candidate(task)
            except (
                contextctl_module.ContextctlError,
                contextctl_module.GuardError,
            ) as exc:
                failure = exc

        self.assertIsNotNone(failure, "a chain change must abort completion")
        self.assertFalse(
            (task / "candidate.md").exists(),
            "a failed completion transaction must remove its candidate",
        )
        latest = task / "checkpoints" / "0002-phase-verified.md"
        self.assertTrue(latest.is_file())
        self.assertNotIn("B002 -> B002", latest.read_text(encoding="utf-8"))

    def test_complete_without_verification_evidence_rolls_back_truthfully(self) -> None:
        task = self.make_task("complete-verification-without-evidence-id")
        candidate = task / "candidate.md"
        candidate.write_text(
            checkpoint(
                task.name,
                1,
                "none",
                "init",
                verification="- Confirm the final state manually.",
            ),
            encoding="utf-8",
        )
        self.run_guard("prepare", candidate)
        self.run_guard("promote", candidate)
        checkpoint_path = task / "checkpoints" / "0001-init.md"
        checkpoint_before = checkpoint_path.read_bytes()

        completed = self.run_contextctl("complete", task)
        output = completed.stdout + completed.stderr

        self.assertNotEqual(0, completed.returncode, output)
        self.assertIn("SAFETY: UNCHANGED", output)
        self.assertFalse(
            candidate.exists(),
            "SAFETY: UNCHANGED is truthful only when the failed draft is absent",
        )
        self.assertEqual(checkpoint_before, checkpoint_path.read_bytes())

    def test_complete_rejects_an_already_complete_latest_checkpoint(self) -> None:
        task, first_checkpoint = self.publish_initial_with_guard(
            "complete-already-complete"
        )
        first_completion = self.run_contextctl("complete", task)
        self.assertEqual(0, first_completion.returncode, first_completion.stderr)
        candidate = task / "candidate.md"
        self.run_guard("prepare", candidate, "--previous", first_checkpoint)
        self.run_guard("promote", candidate)
        completed_checkpoint = task / "checkpoints" / "0002-completion.md"
        completed_before = completed_checkpoint.read_bytes()

        repeated = self.run_contextctl("complete", task)
        output = repeated.stdout + repeated.stderr

        self.assertNotEqual(0, repeated.returncode, output)
        self.assertRegex(output, r"(?i)already.+complete|complete.+already")
        self.assertIn("SAFETY: UNCHANGED", output)
        self.assertFalse(candidate.exists())
        self.assertEqual(completed_before, completed_checkpoint.read_bytes())

    def test_concurrent_draft_creators_cannot_overwrite_each_other(self) -> None:
        task, _ = self.publish_initial_with_guard("concurrent-draft-creators")
        start = threading.Barrier(2)
        after_absence_check = threading.Barrier(2)
        real_scan = contextctl_module.scan_checkpoints
        synchronized_threads: set[int] = set()
        synchronized_threads_lock = threading.Lock()

        def synchronize_scan(task_root: Path) -> list[Path]:
            thread_id = threading.get_ident()
            with synchronized_threads_lock:
                first_scan = thread_id not in synchronized_threads
                synchronized_threads.add(thread_id)
            if first_scan:
                try:
                    after_absence_check.wait(timeout=1)
                except threading.BrokenBarrierError:
                    pass
            return real_scan(task_root)

        def create(trigger: str) -> tuple[str, str]:
            start.wait(timeout=2)
            try:
                contextctl_module.draft_candidate(task, trigger)
            except (
                contextctl_module.ContextctlError,
                contextctl_module.GuardError,
                OSError,
            ) as exc:
                return "error", str(exc)
            return "created", trigger

        with mock.patch.object(
            contextctl_module, "scan_checkpoints", side_effect=synchronize_scan
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(
                    executor.map(create, ("phase-verified", "manual-request"))
                )

        created = [value for status, value in outcomes if status == "created"]
        self.assertEqual(
            1,
            len(created),
            f"exactly one exclusive creator may succeed: {outcomes}",
        )
        candidate = task / "candidate.md"
        self.assertTrue(candidate.is_file())
        self.assertEqual(
            created[0],
            frontmatter(candidate.read_text(encoding="utf-8"))["trigger"],
        )

    def test_complete_never_deletes_a_concurrently_changed_candidate(self) -> None:
        task, _ = self.publish_initial_with_guard("complete-candidate-owner-race")
        real_atomic_publish = contextctl_module.atomic_publish

        def publish_then_change(target: Path, text: str) -> None:
            real_atomic_publish(target, text)
            if target.name == "candidate.md":
                target.write_text(text + "\n", encoding="utf-8")

        failure: Exception | None = None
        with mock.patch.object(
            contextctl_module,
            "atomic_publish",
            side_effect=publish_then_change,
        ):
            try:
                contextctl_module.complete_candidate(task)
            except (
                contextctl_module.ContextctlError,
                contextctl_module.GuardError,
            ) as exc:
                failure = exc

        self.assertIsNotNone(failure)
        self.assertIn("rollback incomplete", str(failure).casefold())
        self.assertTrue((task / "candidate.md").is_file())

    def test_complete_rolls_back_post_link_cleanup_artifact_on_postcheck_failure(
        self,
    ) -> None:
        task, _ = self.publish_initial_with_guard("complete-post-link-rollback")
        real_atomic_publish = contextctl_module.atomic_publish
        real_audit = contextctl_module.audited_continuity_chain
        real_unlink = contextctl_module.os.unlink
        audit_count = 0

        def fail_temp_cleanup(path, *args, **kwargs):
            if Path(path).name.startswith(".publishing-"):
                raise OSError("injected post-link cleanup failure")
            return real_unlink(path, *args, **kwargs)

        def publish_with_cleanup_warning(target: Path, text: str):
            with mock.patch.object(
                contextctl_module.os,
                "unlink",
                side_effect=fail_temp_cleanup,
            ):
                return real_atomic_publish(target, text)

        def audit_then_fail(task_root: Path):
            nonlocal audit_count
            audit_count += 1
            if audit_count == 3:
                raise contextctl_module.ContextctlError("injected postcheck failure")
            return real_audit(task_root)

        stderr = io.StringIO()
        with mock.patch.object(
            contextctl_module,
            "atomic_publish",
            side_effect=publish_with_cleanup_warning,
        ):
            with mock.patch.object(
                contextctl_module,
                "audited_continuity_chain",
                side_effect=audit_then_fail,
            ):
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaisesRegex(
                        contextctl_module.ContextctlError, "postcheck"
                    ):
                        contextctl_module.complete_candidate(task)

        self.assertFalse((task / "candidate.md").exists())
        self.assertEqual([], list(task.glob(".publishing-*")))
        self.assertIn("WARNING", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
