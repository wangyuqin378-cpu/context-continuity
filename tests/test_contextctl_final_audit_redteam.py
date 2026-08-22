from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_checkpoint_guard import checkpoint


SKILL_ROOT = Path(__file__).parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
CONTEXTCTL = SCRIPTS / "contextctl.py"
GUARD = SCRIPTS / "checkpoint_guard.py"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import contextctl as contextctl_module  # noqa: E402
import checkpoint_guard as checkpoint_guard_module  # noqa: E402


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_snapshot(root):
    """Capture bytes, directory entries, and link targets without following links."""

    captured = {}

    def visit(directory):
        entries = sorted(os.scandir(str(directory)), key=lambda entry: entry.name)
        for entry in entries:
            path = Path(entry.path)
            relative = str(path.relative_to(root))
            if entry.is_symlink():
                captured[relative] = ("symlink", os.readlink(entry.path))
            elif entry.is_dir(follow_symlinks=False):
                captured[relative] = ("directory", None)
                visit(path)
            else:
                captured[relative] = ("file", path.read_bytes())

    visit(root)
    return captured


class ContextctlFinalAuditRedTeamTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.continuity = self.workspace / ".continuity"
        self.evidence_dir = self.workspace / "evidence"
        self.external = self.root / "external"
        self.continuity.mkdir(parents=True)
        self.evidence_dir.mkdir()
        self.external.mkdir()
        (self.evidence_dir / "init.log").write_text(
            "checkpoint initialized and verified\n", encoding="utf-8"
        )
        self.source = self.workspace / "initial-source.md"
        self.source.write_text(
            ("durable source fact for compression and binding\n" * 1500),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_canonical_root_mismatch_routes_to_safe_migration(self):
        code, next_step = contextctl_module.error_code(
            "resume", "review belongs to a different canonical task root"
        )

        self.assertEqual("CTX304", code)
        self.assertIn("copied or moved", next_step)
        self.assertIn("new task ID", next_step)

    def run_contextctl(self, *args):
        return subprocess.run(
            [sys.executable, str(CONTEXTCTL)] + [str(arg) for arg in args],
            text=True,
            capture_output=True,
            check=False,
        )

    def run_guard(self, *args):
        return subprocess.run(
            [sys.executable, str(GUARD)] + [str(arg) for arg in args],
            text=True,
            capture_output=True,
            check=False,
        )

    def make_task(self, task_id):
        task = self.continuity / task_id
        task.mkdir()
        return task

    def make_candidate(self, task, text=None, evidence_name=None):
        if text is None:
            text = checkpoint(task.name, 1, "none", "init")
        if evidence_name is not None:
            evidence_path = self.evidence_dir / evidence_name
            evidence_path.write_text(
                "direct evidence supports the checkpoint claim\n", encoding="utf-8"
            )
            text = text.replace(
                "evidence/init.log#sha256=INIT",
                "evidence/%s#L1" % evidence_name,
            )
        candidate = task / "candidate.md"
        candidate.write_text(text, encoding="utf-8")
        return candidate

    def review_init(self, candidate, output=None, source=True):
        if output is None:
            output = candidate.parent / "review.json"
        args = ["review-init", candidate, "--output", output]
        if source:
            args.extend(["--source", self.source])
        return self.run_contextctl(*args), output

    def complete_review(self, candidate, manifest):
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        snapshot = contextctl_module.Snapshot.load(candidate)
        required_ids = list(payload["required_ids"])
        payload.update(
            {
                "reviewer": "fresh-final-audit-reviewer",
                "reviewed_at": "2026-07-12T00:00:00+08:00",
                "recovered_ids": required_ids,
                "missing_ids": [],
                "contradictions": [],
                "ambiguities": [],
                "recovered_next_action": contextctl_module.section_lines(
                    snapshot, "## Next Action"
                )[0],
                "recovered_verification": contextctl_module.section_lines(
                    snapshot, "## Verification for Next Action"
                )[0],
                "verdict": "PASS",
            }
        )
        for record in payload["evidence_health"]:
            record["status"] = record["detected_status"]
            record["semantic_status"] = "DIRECT"
        source_review = payload.get("source_review")
        if isinstance(source_review, dict):
            source_review["recovered_ids"] = list(source_review["required_ids"])
            source_review["missing_ids"] = []
            source_review["coverage_status"] = "COMPLETE"
            source_review["omissions"] = []
        manifest.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return payload

    def make_reviewed_candidate(self, task_id, evidence_name=None):
        task = self.make_task(task_id)
        candidate = self.make_candidate(task, evidence_name=evidence_name)
        result, manifest = self.review_init(candidate)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.complete_review(candidate, manifest)
        checked = self.run_contextctl("review-check", candidate, manifest)
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        return task, candidate, manifest

    def publish_reviewed_task(self, task_id):
        task, candidate, manifest = self.make_reviewed_candidate(task_id)
        published = self.run_contextctl("publish", candidate, "--review", manifest)
        self.assertEqual(0, published.returncode, published.stdout + published.stderr)
        checkpoint_path = task / "checkpoints" / "0001-init.md"
        sidecar = task / "reviews" / "0001-init.json"
        self.assertTrue(checkpoint_path.is_file())
        self.assertTrue(sidecar.is_file())
        resumed = self.run_contextctl("resume", task, "--json")
        diagnosed = self.run_contextctl("doctor", task, "--json")
        self.assertEqual(0, resumed.returncode, resumed.stdout + resumed.stderr)
        self.assertEqual(0, diagnosed.returncode, diagnosed.stdout + diagnosed.stderr)
        return task, checkpoint_path, sidecar

    def publish_later_checkpoint(self, task, trigger="phase-verified"):
        drafted = self.run_contextctl("draft", task, "--trigger", trigger)
        self.assertEqual(0, drafted.returncode, drafted.stdout + drafted.stderr)
        candidate = task / "candidate.md"
        initialized, manifest = self.review_init(candidate, source=False)
        self.assertEqual(
            0, initialized.returncode, initialized.stdout + initialized.stderr
        )
        self.complete_review(candidate, manifest)
        checked = self.run_contextctl("review-check", candidate, manifest)
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        published = self.run_contextctl("publish", candidate, "--review", manifest)
        self.assertEqual(0, published.returncode, published.stdout + published.stderr)
        return task / "checkpoints" / ("0002-%s.md" % trigger)

    def assert_output_rejected_without_change(
        self, candidate, output, source=None
    ):
        before = tree_snapshot(self.root)
        args = ["review-init", candidate, "--output", output]
        if source is not None:
            args.extend(["--source", source])

        result = self.run_contextctl(*args)
        after = tree_snapshot(self.root)

        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(before, after, result.stdout + result.stderr)

    def test_post_link_temp_cleanup_is_warning_only_for_draft(self):
        task = self.make_task("post-link-draft-cleanup")
        real_unlink = os.unlink

        def fail_temp_cleanup(path, *args, **kwargs):
            if Path(path).name.startswith(".publishing-"):
                raise OSError("injected post-link cleanup failure")
            return real_unlink(path, *args, **kwargs)

        stderr = io.StringIO()
        with mock.patch.object(
            checkpoint_guard_module.os, "unlink", side_effect=fail_temp_cleanup
        ):
            with contextlib.redirect_stderr(stderr):
                candidate = contextctl_module.draft_candidate(task, "init")

        self.assertTrue(candidate.is_file())
        self.assertIn("WARNING", stderr.getvalue())

    def test_post_link_checkpoint_cleanup_keeps_bound_publication(self):
        task, candidate, manifest = self.make_reviewed_candidate(
            "post-link-checkpoint-cleanup"
        )
        real_atomic_publish = checkpoint_guard_module.atomic_publish
        real_unlink = os.unlink

        def fail_temp_cleanup(path, *args, **kwargs):
            if Path(path).name.startswith(".publishing-"):
                raise OSError("injected post-link cleanup failure")
            return real_unlink(path, *args, **kwargs)

        def publish_with_checkpoint_cleanup_failure(target, text):
            if target.parent.name == "checkpoints":
                with mock.patch.object(
                    checkpoint_guard_module.os,
                    "unlink",
                    side_effect=fail_temp_cleanup,
                ):
                    return real_atomic_publish(target, text)
            return real_atomic_publish(target, text)

        stderr = io.StringIO()
        with mock.patch.object(
            checkpoint_guard_module,
            "atomic_publish",
            side_effect=publish_with_checkpoint_cleanup_failure,
        ):
            with contextlib.redirect_stderr(stderr):
                published = contextctl_module.publish_reviewed(candidate, manifest)

        checkpoint_path = task / "checkpoints" / "0001-init.md"
        sidecar = task / "reviews" / "0001-init.json"
        marker = task / "reviews" / "0001-init.required.json"
        self.assertEqual(checkpoint_path.resolve(), published.path.resolve())
        self.assertTrue(checkpoint_path.is_file())
        self.assertTrue(sidecar.is_file())
        self.assertTrue(marker.is_file())
        self.assertIn("WARNING", stderr.getvalue())
        self.assertEqual("active", contextctl_module.resume_payload(task)["status"])

    def test_pre_link_cleanup_failure_reports_unknown_partial(self):
        task = self.make_task("pre-link-cleanup")
        real_unlink = os.unlink

        def fail_temp_cleanup(path, *args, **kwargs):
            if Path(path).name.startswith(".publishing-"):
                raise OSError("injected pre-link cleanup failure")
            return real_unlink(path, *args, **kwargs)

        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = [
            str(CONTEXTCTL),
            "draft",
            str(task),
            "--trigger",
            "init",
        ]
        with mock.patch.object(
            checkpoint_guard_module.os, "link", side_effect=OSError("injected link failure")
        ):
            with mock.patch.object(
                checkpoint_guard_module.os,
                "unlink",
                side_effect=fail_temp_cleanup,
            ):
                with mock.patch.object(sys, "argv", argv):
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                        stderr
                    ):
                        returncode = contextctl_module.main()

        output = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(1, returncode)
        self.assertIn("rollback incomplete", output)
        self.assertIn("SAFETY: UNKNOWN/PARTIAL", output)
        self.assertFalse((task / "candidate.md").exists())

    def test_post_link_cleanup_artifact_is_included_in_later_rollback(self):
        task, candidate, manifest = self.make_reviewed_candidate(
            "post-link-cleanup-rollback"
        )
        before = tree_snapshot(self.root)
        real_atomic_publish = checkpoint_guard_module.atomic_publish
        real_unlink = os.unlink
        publish_count = 0

        def fail_temp_cleanup(path, *args, **kwargs):
            if Path(path).name.startswith(".publishing-"):
                raise OSError("injected post-link cleanup failure")
            return real_unlink(path, *args, **kwargs)

        def publish_then_fail(target, text):
            nonlocal publish_count
            publish_count += 1
            if publish_count == 1:
                with mock.patch.object(
                    checkpoint_guard_module.os,
                    "unlink",
                    side_effect=fail_temp_cleanup,
                ):
                    return real_atomic_publish(target, text)
            raise OSError("injected later publication failure")

        stderr = io.StringIO()
        with mock.patch.object(
            checkpoint_guard_module,
            "atomic_publish",
            side_effect=publish_then_fail,
        ):
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(OSError):
                    contextctl_module.publish_reviewed(candidate, manifest)

        self.assertEqual(before, tree_snapshot(self.root))
        self.assertIn("WARNING", stderr.getvalue())
        self.assertFalse((task / ".checkpoint-write.lock").exists())

    def test_review_init_rejects_checkpoint_candidate_source_reserved_symlink_and_existing_outputs(
        self,
    ):
        task = self.make_task("output-checkpoint")
        initial = self.make_candidate(task)
        prepared = self.run_guard("prepare", initial)
        self.assertEqual(0, prepared.returncode, prepared.stdout + prepared.stderr)
        promoted = self.run_guard("promote", initial)
        self.assertEqual(0, promoted.returncode, promoted.stdout + promoted.stderr)
        latest = task / "checkpoints" / "0001-init.md"
        drafted = self.run_contextctl("draft", task, "--trigger", "phase-verified")
        self.assertEqual(0, drafted.returncode, drafted.stdout + drafted.stderr)
        self.assert_output_rejected_without_change(task / "candidate.md", latest)

        task = self.make_task("output-candidate")
        candidate = self.make_candidate(task)
        self.assert_output_rejected_without_change(
            candidate, candidate, source=self.source
        )

        task = self.make_task("output-source")
        candidate = self.make_candidate(task)
        source = task / "task-source.md"
        source.write_text(self.source.read_text(encoding="utf-8"), encoding="utf-8")
        self.assert_output_rejected_without_change(candidate, source, source=source)

        task = self.make_task("output-reviews")
        candidate = self.make_candidate(task)
        (task / "reviews").mkdir()
        reserved = task / "reviews" / "0001-init.json"
        self.assert_output_rejected_without_change(
            candidate, reserved, source=self.source
        )

    def test_review_init_race_preserves_winning_output_and_candidate_bytes(self):
        task = self.make_task("review-init-race")
        candidate = self.make_candidate(task)
        original_candidate = candidate.read_bytes()
        review = task / "review.json"
        winning_bytes = b'{"winner":"other-review-init"}\n'
        original_template = contextctl_module.review_template

        def competing_review_wins(*args, **kwargs):
            payload = original_template(*args, **kwargs)
            review.write_bytes(winning_bytes)
            return payload

        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = [
            str(CONTEXTCTL),
            "review-init",
            str(candidate),
            "--output",
            str(review),
            "--source",
            str(self.source),
        ]
        with mock.patch.object(
            contextctl_module,
            "review_template",
            side_effect=competing_review_wins,
        ):
            with mock.patch.object(sys, "argv", argv):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = contextctl_module.main()

        output = stdout.getvalue() + stderr.getvalue()
        self.assertNotEqual(0, code, output)
        self.assertIn("SAFETY: UNCHANGED", output)
        self.assertEqual(winning_bytes, review.read_bytes())
        self.assertEqual(original_candidate, candidate.read_bytes())

    def test_review_init_incomplete_rollback_reports_unknown_partial(self):
        task = self.make_task("review-init-rollback")
        candidate = self.make_candidate(task)
        original_candidate = candidate.read_bytes()
        real_atomic_replace = contextctl_module.atomic_replace
        state = {"output_failed": False}

        def fail_output(*args, **kwargs):
            state["output_failed"] = True
            raise OSError("injected review output failure")

        def fail_candidate_restore(path, text):
            if state["output_failed"]:
                raise OSError("injected candidate rollback failure")
            return real_atomic_replace(path, text)

        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = [
            str(CONTEXTCTL),
            "review-init",
            str(candidate),
            "--output",
            str(task / "review.json"),
            "--source",
            str(self.source),
        ]
        with mock.patch.object(
            contextctl_module, "atomic_publish", side_effect=fail_output
        ), mock.patch.object(
            contextctl_module, "atomic_replace", side_effect=fail_candidate_restore
        ):
            with mock.patch.object(sys, "argv", argv):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = contextctl_module.main()

        output = stdout.getvalue() + stderr.getvalue()
        self.assertNotEqual(0, code, output)
        self.assertIn("rollback incomplete", output)
        self.assertIn("SAFETY: UNKNOWN/PARTIAL", output)
        self.assertNotEqual(original_candidate, candidate.read_bytes())
        self.assertFalse((task / "review.json").exists())

        task = self.make_task("output-symlink")
        candidate = self.make_candidate(task)
        outside = self.external / "important.json"
        outside.write_text("IMPORTANT-BYTES\n", encoding="utf-8")
        linked_output = task / "linked-review.json"
        linked_output.symlink_to(outside)
        self.assert_output_rejected_without_change(
            candidate, linked_output, source=self.source
        )

        task = self.make_task("output-existing")
        candidate = self.make_candidate(task)
        existing = task / "operator-notes.md"
        existing.write_text("KEEP-THIS-CONTENT\n", encoding="utf-8")
        self.assert_output_rejected_without_change(
            candidate, existing, source=self.source
        )

    def test_only_new_canonical_review_json_is_allowed_and_existing_manifest_is_refused(
        self,
    ):
        task = self.make_task("canonical-output")
        candidate = self.make_candidate(task)
        manifest = task / "review.json"

        first, _ = self.review_init(candidate, output=manifest)
        self.assertEqual(0, first.returncode, first.stdout + first.stderr)
        before = tree_snapshot(self.root)

        second, _ = self.review_init(candidate, output=manifest)

        self.assertNotEqual(0, second.returncode, second.stdout + second.stderr)
        self.assertEqual(before, tree_snapshot(self.root))

    def test_review_init_rejects_malformed_complete_and_unprojectable_content_without_change(
        self,
    ):
        complete_task = self.make_task("malformed-complete")
        complete_text = checkpoint(
            complete_task.name,
            1,
            "none",
            "init",
            status="complete",
            include_w001=False,
            next_action="- Delete production data; None — task complete.",
            verification="- E001 — Final acceptance evidence is recorded.",
        )
        complete_candidate = self.make_candidate(complete_task, complete_text)
        self.assert_output_rejected_without_change(
            complete_candidate,
            complete_task / "review.json",
            source=self.source,
        )

        projection_task = self.make_task("unprojectable-content")
        projection_text = checkpoint(projection_task.name, 1, "none", "init").replace(
            "## Current State\n\n",
            "## Current State\n\nUNPROJECTABLE-PROTECTED-CONTENT\n",
            1,
        )
        projection_candidate = self.make_candidate(projection_task, projection_text)
        self.assert_output_rejected_without_change(
            projection_candidate,
            projection_task / "review.json",
            source=self.source,
        )

    def forged_review_for_prepared_candidate(self, candidate):
        prepared = self.run_guard("prepare", candidate)
        if prepared.returncode != 0:
            return None
        snapshot = contextctl_module.Snapshot.load(candidate)
        payload = contextctl_module.review_template(snapshot, None, None)
        required_ids = list(payload["required_ids"])
        payload.update(
            {
                "reviewer": "forged-projection-review",
                "reviewed_at": "2026-07-12T00:00:00+08:00",
                "recovered_ids": required_ids,
                "missing_ids": [],
                "contradictions": [],
                "ambiguities": [],
                "recovered_next_action": contextctl_module.section_lines(
                    snapshot, "## Next Action"
                )[0],
                "recovered_verification": contextctl_module.section_lines(
                    snapshot, "## Verification for Next Action"
                )[0],
                "verdict": "PASS",
            }
        )
        for record in payload["evidence_health"]:
            record["status"] = record["detected_status"]
            record["semantic_status"] = "DIRECT"
        manifest = candidate.parent / "forged-review.json"
        manifest.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest

    def malformed_complete_candidate(self, task):
        text = checkpoint(
            task.name,
            1,
            "none",
            "init",
            status="complete",
            include_w001=False,
            next_action="- Perform an extra action; None — task complete.",
            verification="- E001 — Final acceptance evidence is recorded.",
        )
        return self.make_candidate(task, text)

    def test_review_check_and_publish_reject_forged_review_of_malformed_projection(
        self,
    ):
        review_task = self.make_task("forged-review-check")
        review_candidate = self.malformed_complete_candidate(review_task)
        review_manifest = self.forged_review_for_prepared_candidate(review_candidate)
        if review_manifest is not None:
            result = self.run_contextctl(
                "review-check", review_candidate, review_manifest
            )
            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

        publish_task = self.make_task("forged-publish")
        publish_candidate = self.malformed_complete_candidate(publish_task)
        publish_manifest = self.forged_review_for_prepared_candidate(publish_candidate)
        if publish_manifest is not None:
            before = tree_snapshot(self.root)
            result = self.run_contextctl(
                "publish", publish_candidate, "--review", publish_manifest
            )
            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(before, tree_snapshot(self.root))
            self.assertFalse((publish_task / "checkpoints").exists())

    def test_high_level_publication_marks_checkpoint_as_review_required(self):
        task, checkpoint_path, sidecar = self.publish_reviewed_task("review-required")
        marker = task / "reviews" / "0001-init.required.json"
        self.assertTrue(marker.is_file())
        self.assertFalse(marker.is_symlink())
        payload = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(task.name, payload["task_id"])
        self.assertRegex(str(payload["schema"]), r"review-required")
        self.assertEqual(checkpoint_path.name, payload["checkpoint"])
        self.assertEqual(digest(checkpoint_path), payload["checkpoint_sha256"])
        self.assertEqual(sidecar.name, payload["review"])
        self.assertEqual(digest(sidecar), payload["review_sha256"])

    def test_first_publish_rejects_symlinked_review_directory(self):
        task, candidate, manifest = self.make_reviewed_candidate(
            "symlinked-review-directory"
        )
        internal_target = task / "review-target"
        internal_target.mkdir()
        review_link = task / "reviews"
        review_link.symlink_to(internal_target, target_is_directory=True)

        result = self.run_contextctl("publish", candidate, "--review", manifest)

        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("SAFETY: UNCHANGED", result.stdout + result.stderr)
        self.assertTrue(review_link.is_symlink())
        self.assertEqual([], list(internal_target.iterdir()))
        self.assertFalse((task / "checkpoints" / "0001-init.md").exists())

    def test_precommit_failure_is_all_or_none(self):
        task, candidate, manifest = self.make_reviewed_candidate("precommit-failure")
        before = tree_snapshot(self.root)
        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = [
            str(CONTEXTCTL),
            "publish",
            str(candidate),
            "--review",
            str(manifest),
        ]

        with mock.patch.object(
            contextctl_module,
            "promote",
            side_effect=contextctl_module.GuardError("injected precommit failure"),
        ):
            with mock.patch.object(sys, "argv", argv):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = contextctl_module.main()

        output = stdout.getvalue() + stderr.getvalue()
        self.assertNotEqual(0, code, output)
        self.assertIn("SAFETY: UNCHANGED", output)
        self.assertEqual(before, tree_snapshot(self.root), output)
        self.assertFalse((task / "checkpoints").exists())
        self.assertFalse((task / "reviews" / "0001-init.json").exists())
        self.assertFalse((task / "reviews" / "0001-init.required.json").exists())

    def test_postcommit_concurrent_writer_never_reports_unchanged(self):
        task, candidate, manifest = self.make_reviewed_candidate("postcommit-race")
        original_promote = contextctl_module.promote

        def promote_then_competitor_lock(*args, **kwargs):
            published_path = original_promote(*args, **kwargs)
            candidate_path = Path(args[0])
            (candidate_path.parent / ".checkpoint-write.lock").mkdir()
            return published_path

        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = [
            str(CONTEXTCTL),
            "publish",
            str(candidate),
            "--review",
            str(manifest),
        ]
        with mock.patch.object(
            contextctl_module, "promote", side_effect=promote_then_competitor_lock
        ):
            with mock.patch.object(sys, "argv", argv):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = contextctl_module.main()

        output = stdout.getvalue() + stderr.getvalue()
        checkpoint_path = task / "checkpoints" / "0001-init.md"
        sidecar = task / "reviews" / "0001-init.json"
        marker = task / "reviews" / "0001-init.required.json"
        self.assertTrue(checkpoint_path.is_file(), output)
        self.assertTrue(sidecar.is_file(), output)
        self.assertTrue(marker.is_file(), output)
        self.assertFalse(candidate.exists(), output)
        self.assertNotIn("SAFETY: UNCHANGED", output)
        if code == 0:
            self.assertIn("PUBLISHED", output)
        else:
            self.assertRegex(output, r"(?i)committed|postcommit|recovery|doctor")

    def mutate_archived_review(self, sidecar, mutation):
        if mutation == "deleted":
            sidecar.unlink()
            return
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        if mutation == "semantic":
            payload["evidence_health"][0]["semantic_status"] = "UNSUPPORTED"
        elif mutation == "reviewer":
            payload["reviewer"] = ""
        elif mutation == "issues":
            payload["contradictions"] = ["injected contradiction"]
        elif mutation == "source":
            payload["initial_source"]["sha256"] = "0" * 64
        elif mutation == "evidence":
            payload["evidence_health"][0]["detected_status"] = "MISSING"
            payload["evidence_health"][0]["status"] = "MISSING"
        elif mutation == "schema-downgrade":
            payload["schema"] = "context-continuity/review/v1"
            payload["review_mode"] = "fresh-agent-candidate-only"
        else:
            raise AssertionError("unknown mutation: %s" % mutation)
        sidecar.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def test_deleted_or_tampered_archived_review_makes_resume_and_doctor_fail_closed(
        self,
    ):
        for mutation in (
            "deleted",
            "semantic",
            "reviewer",
            "issues",
            "source",
            "evidence",
            "schema-downgrade",
        ):
            with self.subTest(mutation=mutation):
                task, _, sidecar = self.publish_reviewed_task(
                    "sidecar-%s" % mutation
                )
                self.mutate_archived_review(sidecar, mutation)

                resume = self.run_contextctl("resume", task, "--json")
                doctor = self.run_contextctl("doctor", task, "--json")

                self.assertNotEqual(
                    0, resume.returncode, resume.stdout + resume.stderr
                )
                self.assertNotEqual(
                    0, doctor.returncode, doctor.stdout + doctor.stderr
                )
                self.assertNotIn('"readiness": "READY"', resume.stdout)
                self.assertNotIn('"readiness": "READY"', doctor.stdout)

    def test_historical_v2_attestation_is_required_by_later_operations(self):
        task, _, first_sidecar = self.publish_reviewed_task(
            "historical-attestation"
        )
        drafted = self.run_contextctl("draft", task, "--trigger", "phase-verified")
        self.assertEqual(0, drafted.returncode, drafted.stdout + drafted.stderr)
        candidate = task / "candidate.md"
        initialized, manifest = self.review_init(candidate, source=False)
        self.assertEqual(
            0, initialized.returncode, initialized.stdout + initialized.stderr
        )
        self.complete_review(candidate, manifest)
        checked = self.run_contextctl("review-check", candidate, manifest)
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        published = self.run_contextctl("publish", candidate, "--review", manifest)
        self.assertEqual(0, published.returncode, published.stdout + published.stderr)
        self.assertTrue((task / "checkpoints" / "0002-phase-verified.md").is_file())

        first_sidecar.unlink()

        for command in (
            ("resume", task, "--json"),
            ("doctor", task, "--json"),
            ("draft", task, "--trigger", "phase-verified"),
        ):
            with self.subTest(command=command[0]):
                result = self.run_contextctl(*command)
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertNotIn('"readiness": "READY"', result.stdout)

    def test_locked_precommit_rechecks_historical_v2_attestation(self):
        task, _, first_sidecar = self.publish_reviewed_task(
            "historical-precommit"
        )
        drafted = self.run_contextctl("draft", task, "--trigger", "phase-verified")
        self.assertEqual(0, drafted.returncode, drafted.stdout + drafted.stderr)
        candidate = task / "candidate.md"
        initialized, manifest = self.review_init(candidate, source=False)
        self.assertEqual(
            0, initialized.returncode, initialized.stdout + initialized.stderr
        )
        self.complete_review(candidate, manifest)
        checked = self.run_contextctl("review-check", candidate, manifest)
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)

        original_promote = contextctl_module.promote

        def delete_history_before_locked_precommit(*args, **kwargs):
            first_sidecar.unlink()
            return original_promote(*args, **kwargs)

        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = [
            str(CONTEXTCTL),
            "publish",
            str(candidate),
            "--review",
            str(manifest),
        ]
        with mock.patch.object(
            contextctl_module,
            "promote",
            side_effect=delete_history_before_locked_precommit,
        ):
            with mock.patch.object(sys, "argv", argv):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = contextctl_module.main()

        output = stdout.getvalue() + stderr.getvalue()
        self.assertNotEqual(0, code, output)
        self.assertIn("SAFETY: UNCHANGED", output)
        self.assertFalse((task / "checkpoints" / "0002-phase-verified.md").exists())
        self.assertFalse((task / "reviews" / "0002-phase-verified.json").exists())
        self.assertFalse(
            (task / "reviews" / "0002-phase-verified.required.json").exists()
        )

    def test_local_evidence_drift_before_review_check_is_rejected(self):
        task = self.make_task("evidence-drift-review")
        candidate = self.make_candidate(task, evidence_name="drift-review.log")
        initialized, manifest = self.review_init(candidate)
        self.assertEqual(
            0, initialized.returncode, initialized.stdout + initialized.stderr
        )
        self.complete_review(candidate, manifest)
        evidence = self.evidence_dir / "drift-review.log"
        evidence.write_text("content changed after semantic review\n", encoding="utf-8")

        result = self.run_contextctl("review-check", candidate, manifest)

        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse((task / "checkpoints").exists())

    def test_local_evidence_drift_after_review_check_blocks_publish_without_change(
        self,
    ):
        task, candidate, manifest = self.make_reviewed_candidate(
            "evidence-drift-publish", evidence_name="drift-publish.log"
        )
        evidence = self.evidence_dir / "drift-publish.log"
        evidence.write_text("content changed after review-check\n", encoding="utf-8")
        before = tree_snapshot(self.root)

        result = self.run_contextctl("publish", candidate, "--review", manifest)

        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(before, tree_snapshot(self.root), result.stdout + result.stderr)
        self.assertFalse((task / "checkpoints").exists())

    def test_archived_evidence_drift_warns_and_allows_repair_draft(self):
        task, _, _ = self.publish_reviewed_task("archived-evidence-changed")
        evidence = self.evidence_dir / "init.log"
        evidence.write_text("changed after publication\n", encoding="utf-8")

        changed = self.run_contextctl("resume", task, "--json")
        self.assertEqual(0, changed.returncode, changed.stdout + changed.stderr)
        changed_payload = json.loads(changed.stdout)
        self.assertEqual("WARNING", changed_payload["readiness"])
        self.assertEqual("CHANGED", changed_payload["evidence"][0]["status"])

        task, _, _ = self.publish_reviewed_task("archived-evidence-missing")
        evidence.unlink()

        missing = self.run_contextctl("resume", task, "--json")
        doctor = self.run_contextctl("doctor", task, "--json")
        drafted = self.run_contextctl(
            "draft", task, "--trigger", "evidence-repair"
        )
        self.assertEqual(0, missing.returncode, missing.stdout + missing.stderr)
        self.assertEqual(0, doctor.returncode, doctor.stdout + doctor.stderr)
        self.assertEqual(0, drafted.returncode, drafted.stdout + drafted.stderr)
        self.assertEqual(
            "MISSING", json.loads(missing.stdout)["evidence"][0]["status"]
        )
        doctor_payload = json.loads(doctor.stdout)
        self.assertEqual("WARNING", doctor_payload["readiness"])
        self.assertIn("evidence-repair draft", doctor_payload["next"])
        self.assertTrue((task / "candidate.md").is_file())

    def test_archived_source_drift_warns_and_allows_repair_draft(self):
        task, _, _ = self.publish_reviewed_task("archived-source-missing")
        self.source.unlink()

        resumed = self.run_contextctl("resume", task, "--json")
        doctor = self.run_contextctl("doctor", task, "--json")
        drafted = self.run_contextctl(
            "draft", task, "--trigger", "evidence-repair"
        )

        self.assertEqual(0, resumed.returncode, resumed.stdout + resumed.stderr)
        self.assertEqual(0, doctor.returncode, doctor.stdout + doctor.stderr)
        self.assertEqual(0, drafted.returncode, drafted.stdout + drafted.stderr)
        self.assertEqual("WARNING", json.loads(resumed.stdout)["readiness"])
        self.assertEqual("MISSING", json.loads(resumed.stdout)["source"]["status"])
        self.assertIn("evidence-repair draft", json.loads(doctor.stdout)["next"])
        self.assertTrue((task / "candidate.md").is_file())

    def test_historical_source_health_remains_visible_without_blocking_latest(self):
        task, _, _ = self.publish_reviewed_task("historical-source-health")
        self.publish_later_checkpoint(task)
        self.source.unlink()

        resumed = self.run_contextctl("resume", task, "--json")
        human = self.run_contextctl("resume", task)
        doctor = self.run_contextctl("doctor", task, "--json")

        self.assertEqual(0, resumed.returncode, resumed.stdout + resumed.stderr)
        self.assertEqual(0, human.returncode, human.stdout + human.stderr)
        self.assertEqual(0, doctor.returncode, doctor.stdout + doctor.stderr)
        payload = json.loads(resumed.stdout)
        self.assertEqual("READY", payload["readiness"])
        self.assertEqual("MISSING", payload["source"]["status"])
        self.assertTrue(payload["source"]["historical"])
        self.assertIn("HEALTH: source=HISTORICAL MISSING", human.stdout)
        doctor_payload = json.loads(doctor.stdout)
        self.assertEqual("READY", doctor_payload["readiness"])
        self.assertTrue(doctor_payload["checks"]["source"]["historical"])


if __name__ == "__main__":
    unittest.main()
