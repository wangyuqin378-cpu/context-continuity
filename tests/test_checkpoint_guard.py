from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "checkpoint_guard.py"
CONTRACT = """- G001 Goal: Preserve verified task continuity.
- I001 In scope: Markdown checkpoint creation and recovery.
- O001 Out of scope: Global host hook installation.
- S001 Success: A fresh agent can name the next action and proof.
- C001 Constraint: Published checkpoints are append-only."""


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checkpoint(
    task_id: str,
    seq: int,
    previous: str,
    trigger: str,
    *,
    contract: str = CONTRACT,
    contract_version: int = 1,
    status: str = "active",
    primary_decision: str = "- D001 — Select latest by continuous sequence. Why: timestamps can drift. Supersedes: none.",
    extra_decisions: str = "",
    extra_evidence: str = "",
    include_w001: bool = True,
    include_e001: bool = True,
    next_action: str = "- W001 — Run the checkpoint guard once.",
    verification: str = "- E001 — The guard exits successfully and reports the expected sequence.",
    blocker: str = "- B001 — none; no release condition is needed.",
    delta: str = "- Added: initial checkpoint.\n- Changed: none.\n- Superseded: none.\n- Resolved or cancelled: none.\n- Compressed or dropped: none.\n- Authorized by: initial task request.\n- Growth reason: initial checkpoint.",
) -> str:
    remaining = "- Remaining: W001 — Publish and verify the next checkpoint." if include_w001 else "- Remaining: none."
    evidence = "- E001 — `evidence/init.log#sha256=INIT` — supports S001 and W001." if include_e001 else "- E009 — `evidence/replacement.log#sha256=NEW` — supports S001."
    verified_id = "E001" if include_e001 else "E009"
    return f"""---
schema: context-continuity/v1
task_id: {task_id}
seq: {seq}
previous: {previous}
previous_sha256: auto
contract_version: {contract_version}
contract_sha256: auto
status: {status}
trigger: {trigger}
created_at: 2026-07-11T12:00:00+08:00
workspace_revision: fixture-v1
---

# Fixture checkpoint

## Contract

{contract}

## Current State

- Verified: {verified_id} confirms the checkpoint protocol is initialized.
- In progress: checkpoint verification.
{remaining}

## Active Decisions

{primary_decision}
{extra_decisions or '- No additional active decision.'}

## Next Action

{next_action}

## Verification for Next Action

{verification}

## Evidence Pointers

{evidence}
{extra_evidence or '- No additional evidence.'}

## Blockers and Open Questions

{blocker}

## Failed Attempts Not To Repeat

- F001 — none; no failed approach is active.

## Assumptions and Unknowns

- A001 — The host may not provide lifecycle hooks.

## Delta From Previous

{delta}

## Retention Audit

- [x] Goal, scope, non-goals, success criteria, and permissions are preserved.
- [x] User corrections and active decisions retain rationale and provenance.
- [x] Open work, blockers, approvals, and non-repeatable failures are preserved.
- [x] Evidence uses stable pointers instead of raw logs or chat history.
- [x] One next action and its verification check are explicit.
"""


class GuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.task = Path(self.temporary.name) / "fixture-task"
        self.task.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_guard(self, *args: object, ok: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *(str(arg) for arg in args)],
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
        candidate.write_text(checkpoint("fixture-task", 1, "none", "init"), encoding="utf-8")
        self.run_guard("prepare", candidate)
        prepared_hash = digest(candidate)
        self.run_guard("check", candidate)
        self.run_guard("promote", candidate)
        published = self.task / "checkpoints" / "0001-init.md"
        self.assertEqual(prepared_hash, digest(published))
        self.assertFalse(candidate.exists())
        return published

    def test_initial_checkpoint_is_finalized_and_append_only(self) -> None:
        published = self.publish_initial()
        before = digest(published)
        self.run_guard("audit", self.task)
        latest = self.run_guard("latest", self.task).stdout.strip()
        self.assertEqual(str(published), latest)
        self.assertEqual(before, digest(published))
        self.assertFalse((self.task / ".checkpoint-write.lock").exists())

    def test_second_checkpoint_keeps_p0_and_explains_growth(self) -> None:
        previous = self.publish_initial()
        original_hash = digest(previous)
        candidate = self.task / "candidate.md"
        delta = """- Added: D002 and E002.
- Changed: next verification now covers compare-and-swap behavior.
- Superseded: none.
- Resolved or cancelled: none.
- Compressed or dropped: repeated background prose was collapsed.
- Authorized by: current implementation plan.
- Growth reason: D002 and E002 are new durable facts."""
        candidate.write_text(
            checkpoint(
                "fixture-task",
                2,
                previous.name,
                "decision",
                extra_decisions="- D002 — Recheck the parent under lock. Why: prevent stale promotion. Supersedes: none.",
                extra_evidence="- E002 — `evidence/race.log#sha256=RACE` — supports D002.",
                delta=delta,
            ),
            encoding="utf-8",
        )
        self.run_guard("prepare", candidate, "--previous", previous)
        prepared_hash = digest(candidate)
        self.run_guard("promote", candidate)
        published = self.task / "checkpoints" / "0002-decision.md"
        text = published.read_text(encoding="utf-8")
        self.assertEqual(original_hash, digest(previous))
        self.assertEqual(prepared_hash, digest(published))
        for item_id in ("D001", "D002", "W001", "E001", "E002"):
            self.assertIn(item_id, text)

    def test_missing_protected_item_and_contract_drift_are_rejected(self) -> None:
        previous = self.publish_initial()
        candidate = self.task / "candidate.md"
        candidate.write_text(
            checkpoint(
                "fixture-task",
                2,
                previous.name,
                "phase-verified",
                include_w001=False,
                delta="""- Added: none.
- Changed: none.
- Superseded: none.
- Resolved or cancelled: none.
- Compressed or dropped: none.
- Authorized by: none.
- Growth reason: none.""",
            ),
            encoding="utf-8",
        )
        failure = self.run_guard("prepare", candidate, "--previous", previous, ok=False)
        self.assertIn("W001", failure.stderr)

        candidate.write_text(
            checkpoint(
                "fixture-task",
                2,
                previous.name,
                "phase-verified",
                include_e001=False,
                delta="""- Added: E009.
- Changed: none.
- Superseded: none.
- Resolved or cancelled: none.
- Compressed or dropped: none.
- Authorized by: none.
- Growth reason: E009 is new evidence.""",
            ),
            encoding="utf-8",
        )
        failure = self.run_guard("prepare", candidate, "--previous", previous, ok=False)
        self.assertIn("E001", failure.stderr)

        candidate.write_text(
            checkpoint(
                "fixture-task",
                2,
                previous.name,
                "phase-verified",
                contract=CONTRACT.replace("append-only", "mutable"),
            ),
            encoding="utf-8",
        )
        failure = self.run_guard("prepare", candidate, "--previous", previous, ok=False)
        self.assertIn("contract-change", failure.stderr)

    def test_authorized_contract_change_advances_contract_version(self) -> None:
        previous = self.publish_initial()
        candidate = self.task / "candidate.md"
        changed_contract = CONTRACT.replace(
            "C001 Constraint: Published checkpoints are append-only.",
            "C002 Constraint: Published checkpoints remain append-only and hash-linked.",
        )
        delta = """- Added: C002.
- Changed: the publication constraint now requires a hash link.
- Superseded: C001 -> C002 — authorized constraint update.
- Resolved or cancelled: none.
- Compressed or dropped: none.
- Authorized by: E001 — the user's explicit contract correction.
- Growth reason: C002 adds a new hash-link requirement."""
        candidate.write_text(
            checkpoint(
                "fixture-task",
                2,
                previous.name,
                "contract-change",
                contract=changed_contract,
                contract_version=2,
                delta=delta,
            ),
            encoding="utf-8",
        )
        self.run_guard("prepare", candidate, "--previous", previous)
        self.run_guard("check", candidate, "--previous", previous)

    def test_decision_meaning_change_requires_explicit_changed_entry(self) -> None:
        previous = self.publish_initial()
        candidate = self.task / "candidate.md"
        delta = """- Added: none.
- Changed: none.
- Superseded: none.
- Resolved or cancelled: none.
- Compressed or dropped: none.
- Authorized by: none.
- Growth reason: none."""
        candidate.write_text(
            checkpoint(
                "fixture-task",
                2,
                previous.name,
                "decision",
                primary_decision="- D001 — Ignore sequence and trust memory. Why: it is faster. Supersedes: none.",
                delta=delta,
            ),
            encoding="utf-8",
        )
        failure = self.run_guard("prepare", candidate, "--previous", previous, ok=False)
        self.assertIn("D001", failure.stderr)

        candidate.write_text(
            checkpoint(
                "fixture-task",
                2,
                previous.name,
                "decision",
                primary_decision="- D001 — Ignore sequence and trust memory. Why: it is faster. Supersedes: none.",
                delta=delta.replace("Changed: none", "Changed: D001 — explicit reviewed change").replace(
                    "Growth reason: none", "Growth reason: D001's reviewed wording is longer"
                ),
            ),
            encoding="utf-8",
        )
        self.run_guard("prepare", candidate, "--previous", previous)

    def test_changed_text_cannot_hide_removed_open_work(self) -> None:
        previous = self.publish_initial()
        candidate = self.task / "candidate.md"
        candidate.write_text(
            checkpoint(
                "fixture-task",
                2,
                previous.name,
                "phase-verified",
                include_w001=False,
                next_action="- Review the checkpoint.",
                delta="""- Added: none.
- Changed: W001 remains unresolved and must not be removed.
- Superseded: none.
- Resolved or cancelled: none.
- Compressed or dropped: none.
- Authorized by: none.
- Growth reason: none.""",
            ),
            encoding="utf-8",
        )
        failure = self.run_guard("prepare", candidate, "--previous", previous, ok=False)
        self.assertIn("W001", failure.stderr)

    def test_contract_same_id_and_fake_authorization_are_rejected(self) -> None:
        previous = self.publish_initial()
        candidate = self.task / "candidate.md"
        candidate.write_text(
            checkpoint(
                "fixture-task",
                2,
                previous.name,
                "contract-change",
                contract=CONTRACT.replace("append-only", "rewritable when useful"),
                contract_version=2,
                delta="""- Added: none.
- Changed: C001 was relaxed.
- Superseded: C001 -> C001 — inferred preference.
- Resolved or cancelled: none.
- Compressed or dropped: none.
- Authorized by: inferred from user preference.
- Growth reason: none.""",
            ),
            encoding="utf-8",
        )
        failure = self.run_guard("prepare", candidate, "--previous", previous, ok=False)
        self.assertRegex(failure.stderr, r"authorization|new ID")

    def test_waiting_and_complete_status_must_be_self_consistent(self) -> None:
        candidate = self.task / "candidate.md"
        candidate.write_text(
            checkpoint("fixture-task", 1, "none", "init", status="waiting"),
            encoding="utf-8",
        )
        failure = self.run_guard("prepare", candidate, ok=False)
        self.assertIn("B###", failure.stderr)

        candidate.write_text(
            checkpoint("fixture-task", 1, "none", "init", status="complete"),
            encoding="utf-8",
        )
        failure = self.run_guard("prepare", candidate, ok=False)
        self.assertIn("task complete", failure.stderr)

    def test_no_space_cjk_growth_cannot_bypass_size_accounting(self) -> None:
        candidate = self.task / "candidate.md"
        initial_padding = " ".join(["old"] * 4000)
        initial = checkpoint("fixture-task", 1, "none", "init").replace(
            "- A001 — The host may not provide lifecycle hooks.",
            f"- A001 — The host may not provide lifecycle hooks.\n\n{initial_padding}",
        )
        candidate.write_text(initial, encoding="utf-8")
        self.run_guard("prepare", candidate)
        self.run_guard("promote", candidate)
        previous = self.task / "checkpoints" / "0001-init.md"

        cjk_padding = "界" * 6000
        update = checkpoint(
            "fixture-task",
            2,
            previous.name,
            "phase-verified",
            delta="""- Added: none.
- Changed: none.
- Superseded: none.
- Resolved or cancelled: none.
- Compressed or dropped: none.
- Authorized by: none.
- Growth reason: none.""",
        ).replace(
            "- A001 — The host may not provide lifecycle hooks.",
            f"- A001 — The host may not provide lifecycle hooks.\n\n{cjk_padding}",
        )
        candidate.write_text(update, encoding="utf-8")
        failure = self.run_guard("prepare", candidate, "--previous", previous, ok=False)
        self.assertRegex(failure.stderr, r"bytes|characters")

    def test_oversized_initial_capsule_is_rejected(self) -> None:
        candidate = self.task / "candidate.md"
        oversized = checkpoint("fixture-task", 1, "none", "init").replace(
            "- A001 — The host may not provide lifecycle hooks.",
            f"- A001 — The host may not provide lifecycle hooks. {'界' * 50000}",
        )
        candidate.write_text(oversized, encoding="utf-8")
        failure = self.run_guard("prepare", candidate, ok=False)
        self.assertIn("128 KiB", failure.stderr)

    def test_check_reports_multilingual_size_metrics(self) -> None:
        published = self.publish_initial()
        output = self.run_guard("check", published).stdout
        for metric in ("words=", "bytes=", "characters=", "lines="):
            self.assertIn(metric, output)

    def test_stale_and_corrupt_candidates_cannot_replace_latest(self) -> None:
        previous = self.publish_initial()
        delta = """- Added: D002.
- Changed: none.
- Superseded: none.
- Resolved or cancelled: none.
- Compressed or dropped: none.
- Authorized by: current implementation plan.
- Growth reason: D002 is a new durable fact."""
        first = self.task / "candidate-a.md"
        second = self.task / "candidate-b.md"
        payload = checkpoint(
            "fixture-task",
            2,
            previous.name,
            "decision",
            extra_decisions="- D002 — Use a writer lock. Why: prevent concurrent append. Supersedes: none.",
            delta=delta,
        )
        first.write_text(payload, encoding="utf-8")
        second.write_text(payload, encoding="utf-8")
        self.run_guard("prepare", first, "--previous", previous)
        self.run_guard("prepare", second, "--previous", previous)
        self.run_guard("promote", first)
        failure = self.run_guard("promote", second, ok=False)
        self.assertRegex(failure.stderr, r"seq must be 3|previous must be")
        latest = self.task / "checkpoints" / "0002-decision.md"
        stable_hashes = {path.name: digest(path) for path in (previous, latest)}

        corrupt = self.task / "candidate.md"
        corrupt.write_text(
            checkpoint(
                "fixture-task",
                3,
                latest.name,
                "phase-verified",
                extra_decisions="- D002 — Use a writer lock. Why: prevent concurrent append. Supersedes: none.",
                delta=delta,
            ),
            encoding="utf-8",
        )
        self.run_guard("prepare", corrupt, "--previous", latest)
        self.run_guard("check", corrupt, "--previous", latest)
        corrupt.write_text("---\nschema: truncated\n", encoding="utf-8")
        self.run_guard("promote", corrupt, ok=False)
        self.assertEqual(stable_hashes, {path.name: digest(path) for path in (previous, latest)})
        self.assertEqual(2, len(list((self.task / "checkpoints").glob("*.md"))))
        self.assertFalse((self.task / ".checkpoint-write.lock").exists())


if __name__ == "__main__":
    unittest.main()
