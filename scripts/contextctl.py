#!/usr/bin/env python3
"""Low-friction operator commands for context-continuity checkpoints."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import socket
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from checkpoint_guard import (
    GuardError,
    Snapshot,
    atomic_publish,
    atomic_replace,
    audited_chain,
    prepare,
    process_start_token,
    promote,
    replace_meta,
    scan_checkpoints,
    section,
    sha256_file,
    validate_pair,
    validate_task_id,
    validate_trigger,
    check,
)


SCHEMA = "context-continuity/resume/v1"
VERSION = "2.1.0"
REVIEW_SCHEMA_V1 = "context-continuity/review/v1"
REVIEW_SCHEMA = "context-continuity/review/v2"
REVIEW_REQUIRED_SCHEMA = "context-continuity/review-required/v1"
DOCTOR_SCHEMA = "context-continuity/doctor/v1"
TASK_LIST_SCHEMA = "context-continuity/tasks/v1"
LOCK_SCHEMA = "context-continuity/lock/v1"
CARD_LINE_LIMIT = 30
BRIEF_CARD_LINE_LIMIT = 12
BRIEF_ATTENTION_LIMIT = 3
INITIAL_SOURCE_WORDS = 500
INITIAL_SOURCE_BYTES = 4 * 1024
MINIMUM_COMPRESSION_BASIS_POINTS = 3000
UNSAFE_POINTER = re.compile(r"[\x00-\x1f\x7f]|\$\(|[`*?\[\]{};|]")
URI_SCHEMES = {"http", "https", "s3", "gs", "app", "notion", "lark"}
PROJECTED_SECTIONS = (
    "## Contract",
    "## Current State",
    "## Active Decisions",
    "## Next Action",
    "## Verification for Next Action",
    "## Evidence Pointers",
    "## Blockers and Open Questions",
    "## Failed Attempts Not To Repeat",
    "## Assumptions and Unknowns",
)


class ContextctlError(Exception):
    pass


def section_lines(snapshot: Snapshot, heading: str) -> list[str]:
    return [
        line.strip()
        for line in section(snapshot.body, heading).splitlines()
        if line.strip().startswith("- ")
    ]


def task_workspace(task_root: Path) -> Path:
    if task_root.parent.name == ".continuity":
        return task_root.parent.parent
    return task_root


def within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def evidence_record_for_pointer(
    item_id: str, pointer: str | None, base: Path
) -> dict[str, str | None]:
    record: dict[str, str | None] = {
        "id": item_id,
        "pointer": pointer,
        "status": "UNSAFE",
        "resolved_path": None,
    }
    if pointer is None:
        return record

    file_part = pointer.split("#", 1)[0]
    parsed = urlparse(file_part)
    if parsed.scheme.casefold() in URI_SCHEMES or (
        parsed.scheme and len(parsed.scheme) > 1
    ):
        record["status"] = "EXTERNAL"
        return record
    if not file_part or file_part.startswith("~") or UNSAFE_POINTER.search(file_part):
        return record

    candidate = Path(file_part)
    base_resolved = base.resolve(strict=False)
    joined = candidate if candidate.is_absolute() else base_resolved / candidate
    lexical = Path(os.path.abspath(str(joined)))
    resolved = joined.resolve(strict=False)
    if not candidate.is_absolute() and not within(lexical, base_resolved):
        record["status"] = "UNSAFE"
        return record
    if within(lexical, base_resolved) and not within(resolved, base_resolved):
        record["status"] = "UNSAFE"
        return record
    if not within(resolved, base_resolved):
        record["status"] = "EXTERNAL"
        return record
    record["resolved_path"] = str(resolved)
    record["status"] = "OK" if resolved.is_file() else "MISSING"
    return record


def evidence_records(line: str, base: Path) -> list[dict[str, str | None]]:
    item_match = re.search(r"\b(E\d{3})\b", line)
    item_id = item_match.group(1) if item_match else "E???"
    pointers = re.findall(r"`([^`]+)`", line)
    if not pointers:
        return [evidence_record_for_pointer(item_id, None, base)]
    return [evidence_record_for_pointer(item_id, pointer, base) for pointer in pointers]


def aggregate_evidence_status(records: list[dict[str, str | None]]) -> str:
    statuses = {str(record["status"]) for record in records}
    if "UNSAFE" in statuses:
        return "UNSAFE"
    if "UNCHECKED" in statuses:
        return "UNCHECKED"
    if "MISSING" in statuses:
        return "MISSING"
    if "EXTERNAL" in statuses:
        return "EXTERNAL"
    return "OK"


def whitespace_word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def compression_basis_points(source_count: int, candidate_count: int) -> int:
    if source_count == 0:
        return 0 if candidate_count == 0 else -10000
    return ((source_count - candidate_count) * 10000) // source_count


def canonical_initial_source(candidate_path: Path, source_path: Path) -> Path:
    canonical = source_path.expanduser().resolve(strict=True)
    if not canonical.is_file():
        raise ContextctlError(f"initial source is not a regular file: {canonical}")
    canonical_candidate = candidate_path.expanduser().resolve(strict=True)
    if os.path.samefile(canonical, canonical_candidate):
        raise ContextctlError(
            "initial source must not be the candidate or a same-file alias; "
            "use a distinct stable source or --no-source-reason"
        )
    return canonical


def initial_source_binding(snapshot: Snapshot, source_path: Path) -> dict[str, object]:
    canonical = canonical_initial_source(snapshot.path, source_path)
    source_text = canonical.read_text(encoding="utf-8")
    source_counts = {
        "words": whitespace_word_count(source_text),
        "utf8_bytes": len(source_text.encode("utf-8")),
    }
    candidate_counts = {
        "words": whitespace_word_count(snapshot.text),
        "utf8_bytes": len(snapshot.text.encode("utf-8")),
    }
    compression = {
        key: compression_basis_points(source_counts[key], candidate_counts[key])
        for key in ("words", "utf8_bytes")
    }
    gate_required = (
        source_counts["words"] > INITIAL_SOURCE_WORDS
        or source_counts["utf8_bytes"] > INITIAL_SOURCE_BYTES
    )
    gate_passed = not gate_required or all(
        value >= MINIMUM_COMPRESSION_BASIS_POINTS for value in compression.values()
    )
    binding: dict[str, object] = {
        "schema": "context-continuity/initial-source/v1",
        "available": True,
        "canonical_path": str(canonical),
        "sha256": sha256_file(canonical),
        "source_counts": source_counts,
        "candidate_counts": candidate_counts,
        "compression_basis_points": compression,
        "gate_required": gate_required,
        "gate_passed": gate_passed,
        "detected_ids": sorted(set(re.findall(r"\b[GIOSCDWEBFA]\d{3}\b", source_text))),
    }
    if not gate_passed:
        raise ContextctlError(
            "initial source compression gate failed: "
            f"words={compression['words']}bp utf8_bytes={compression['utf8_bytes']}bp "
            f"required={MINIMUM_COMPRESSION_BASIS_POINTS}bp"
        )
    return binding


def no_source_binding(reason: str) -> dict[str, object]:
    reason = reason.strip()
    if not reason:
        raise ContextctlError("--no-source-reason must be non-empty")
    return {
        "schema": "context-continuity/initial-source/v1",
        "available": False,
        "reason": reason,
    }


def validate_frozen_source_binding(
    snapshot: Snapshot, binding: dict[str, object], require_v2: bool
) -> None:
    if binding.get("schema") != "context-continuity/initial-source/v1":
        raise ContextctlError("archived initial source schema is invalid")
    if binding.get("available") is False:
        if binding != no_source_binding(str(binding.get("reason", ""))):
            raise ContextctlError("archived no-source provenance is invalid")
        return
    if require_v2 and binding.get("available") is not True:
        raise ContextctlError("archived initial source availability is invalid")

    canonical_path = binding.get("canonical_path")
    source_hash = binding.get("sha256")
    source_counts = binding.get("source_counts")
    candidate_counts = binding.get("candidate_counts")
    compression = binding.get("compression_basis_points")
    if (
        not isinstance(canonical_path, str)
        or not Path(canonical_path).is_absolute()
        or not isinstance(source_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", source_hash)
        or not isinstance(source_counts, dict)
        or not isinstance(candidate_counts, dict)
        or not isinstance(compression, dict)
    ):
        raise ContextctlError("archived initial source binding is malformed")
    current_candidate_counts = {
        "words": whitespace_word_count(snapshot.text),
        "utf8_bytes": len(snapshot.text.encode("utf-8")),
    }
    if candidate_counts != current_candidate_counts:
        raise ContextctlError("archived initial source candidate counts changed")
    for key in ("words", "utf8_bytes"):
        if (
            not isinstance(source_counts.get(key), int)
            or int(source_counts[key]) < 0
            or not isinstance(compression.get(key), int)
            or compression[key]
            != compression_basis_points(
                int(source_counts[key]), int(candidate_counts[key])
            )
        ):
            raise ContextctlError("archived initial source counts are invalid")
    gate_required = (
        int(source_counts["words"]) > INITIAL_SOURCE_WORDS
        or int(source_counts["utf8_bytes"]) > INITIAL_SOURCE_BYTES
    )
    gate_passed = not gate_required or all(
        int(compression[key]) >= MINIMUM_COMPRESSION_BASIS_POINTS
        for key in ("words", "utf8_bytes")
    )
    if binding.get("gate_required") is not gate_required or binding.get(
        "gate_passed"
    ) is not gate_passed:
        raise ContextctlError("archived initial source compression gate changed")
    if require_v2:
        detected_ids = binding.get("detected_ids")
        if (
            not isinstance(detected_ids, list)
            or detected_ids != sorted(set(detected_ids))
            or any(
                not isinstance(item_id, str)
                or not re.fullmatch(r"[GIOSCDWEBFA]\d{3}", item_id)
                for item_id in detected_ids
            )
        ):
            raise ContextctlError("archived initial source IDs are invalid")


def detected_review_evidence(snapshot: Snapshot) -> list[dict[str, object]]:
    base = task_workspace(snapshot_task_root(snapshot))
    detected: list[dict[str, object]] = []
    for line in section_lines(snapshot, "## Evidence Pointers"):
        if not re.search(r"\bE\d{3}\b", line):
            continue
        records = evidence_records(line, base)
        item_id = str(records[0]["id"])
        content_bindings: list[dict[str, str | None]] = []
        for record in records:
            binding = {
                "pointer": record["pointer"],
                "detected_status": record["status"],
                "canonical_path": record["resolved_path"],
                "sha256": None,
            }
            if record["status"] == "OK" and record["resolved_path"]:
                binding["sha256"] = sha256_file(Path(str(record["resolved_path"])))
            content_bindings.append(binding)
        detected.append(
            {
                "id": item_id,
                "status": "UNCHECKED",
                "detected_status": aggregate_evidence_status(records),
                "semantic_status": "UNCHECKED",
                "content_bindings": content_bindings,
            }
        )
    return sorted(detected, key=lambda record: record["id"])


def validate_frozen_evidence_bindings(
    snapshot: Snapshot, evidence_health: object
) -> None:
    if not isinstance(evidence_health, list):
        raise ContextctlError("archived review evidence_health must be a list")
    expected_pointers: dict[str, list[str | None]] = {}
    for line in section_lines(snapshot, "## Evidence Pointers"):
        item_match = re.search(r"\b(E\d{3})\b", line)
        if not item_match:
            continue
        pointers = re.findall(r"`([^`]+)`", line)
        expected_pointers[item_match.group(1)] = pointers or [None]
    records = [record for record in evidence_health if isinstance(record, dict)]
    if len(records) != len(evidence_health):
        raise ContextctlError("archived evidence record must be an object")
    if sorted(str(record.get("id")) for record in records) != sorted(
        expected_pointers
    ):
        raise ContextctlError("archived review evidence coverage is incomplete")
    for record in records:
        item_id = str(record.get("id"))
        bindings = record.get("content_bindings")
        if not isinstance(bindings, list) or [
            binding.get("pointer") if isinstance(binding, dict) else None
            for binding in bindings
        ] != expected_pointers[item_id]:
            raise ContextctlError(
                f"archived evidence pointers changed for {item_id}"
            )
        statuses: set[str] = set()
        for binding in bindings:
            if not isinstance(binding, dict):
                raise ContextctlError("archived evidence binding must be an object")
            status = str(binding.get("detected_status"))
            statuses.add(status)
            canonical_path = binding.get("canonical_path")
            content_hash = binding.get("sha256")
            if status == "OK":
                if (
                    not isinstance(canonical_path, str)
                    or not Path(canonical_path).is_absolute()
                    or not isinstance(content_hash, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", content_hash)
                ):
                    raise ContextctlError(
                        f"archived local evidence binding is invalid for {item_id}"
                    )
            elif status == "EXTERNAL":
                if canonical_path is not None or content_hash is not None:
                    raise ContextctlError(
                        f"archived external evidence binding is invalid for {item_id}"
                    )
            else:
                raise ContextctlError(
                    f"archived evidence status is invalid for {item_id}"
                )
        detected_status = "EXTERNAL" if "EXTERNAL" in statuses else "OK"
        if (
            record.get("detected_status") != detected_status
            or record.get("status") != detected_status
            or record.get("semantic_status") not in {"DIRECT", "REPORTED"}
        ):
            raise ContextctlError(
                f"archived evidence review is incomplete for {item_id}"
            )


def transition_records(snapshot: Snapshot) -> list[str]:
    delta = section(snapshot.body, "## Delta From Previous")
    return [
        f"{source} -> {target}"
        for source, target in re.findall(
            r"\b([GIOSCDWEBFA]\d{3})\s*(?:->|→)\s*([GIOSCDWEBFA]\d{3})\b",
            delta,
        )
    ]


def validate_resume_projection(
    snapshot: Snapshot, allow_legacy_action_marker: bool = False
) -> None:
    for heading in PROJECTED_SECTIONS:
        for line in section(snapshot.body, heading).splitlines():
            if line.strip() and not line.startswith("- "):
                raise ContextctlError(
                    f"{heading} contains content that the Resume Card cannot project: {line.strip()}"
                )
    for line in section_lines(snapshot, "## Contract"):
        if not re.match(r"^- [GIOSC]\d{3}\b", line):
            raise ContextctlError(f"Contract item lacks a stable G/I/O/S/C ID: {line}")

    next_action = section_lines(snapshot, "## Next Action")[0]
    if snapshot.meta["status"] in {"active", "verifying"}:
        current_work = {item_id for item_id in snapshot.state_ids if item_id.startswith("W")}
        cited_work = set(re.findall(r"\bW\d{3}\b", next_action))
        legacy_markers = set(
            re.findall(r"\bNEXT-[A-Z0-9]+(?:-[A-Z0-9]+)+\b", next_action)
        )
        legacy_single_work = (
            allow_legacy_action_marker
            and not cited_work
            and len(current_work) == 1
            and len(legacy_markers) == 1
        )
        if not legacy_single_work and (
            len(cited_work) != 1 or not cited_work <= current_work
        ):
            raise ContextctlError(
                "active or verifying Next Action must cite exactly one current W###"
            )
    if snapshot.meta["status"] == "complete" and next_action.casefold() != "- none — task complete.":
        raise ContextctlError("complete Next Action must be exactly 'None — task complete.'")


def validate_published_attestation(
    task_root: Path, snapshot: Snapshot, previous: Snapshot | None
) -> None:
    review_dir = task_root / "reviews"
    if review_dir.exists() and (review_dir.is_symlink() or not review_dir.is_dir()):
        raise ContextctlError("published review directory is unsafe")
    review_path = review_dir / f"{snapshot.seq:04d}-{snapshot.meta['trigger']}.json"
    marker = review_path.with_name(f"{review_path.stem}.required.json")
    if not review_path.exists():
        if (
            snapshot.meta.get("review_required") == REVIEW_SCHEMA
            or marker.exists()
        ):
            raise ContextctlError("review-required checkpoint is missing its sidecar")
        return
    if review_path.is_symlink() or not review_path.is_file():
        raise ContextctlError("published review sidecar is unsafe")
    manifest, _ = load_manifest(review_path)
    requires_v2 = (
        snapshot.meta.get("review_required") == REVIEW_SCHEMA or marker.exists()
    )
    if requires_v2:
        if manifest.get("schema") != REVIEW_SCHEMA:
            raise ContextctlError(
                "review-required checkpoint cannot downgrade its sidecar schema"
            )
        if not marker.is_file() or marker.is_symlink():
            raise ContextctlError("current review-required marker is missing or unsafe")
        marker_payload, _ = load_manifest(marker)
        expected_marker = {
            "schema": REVIEW_REQUIRED_SCHEMA,
            "task_id": snapshot.meta["task_id"],
            "checkpoint": snapshot.path.name,
            "checkpoint_sha256": sha256_file(snapshot.path),
            "review": review_path.name,
            "review_sha256": sha256_file(review_path),
        }
        if marker_payload != expected_marker:
            raise ContextctlError("current review-required marker binding is invalid")
        validate_review_snapshot(
            snapshot, previous, manifest, live_bindings=False
        )
    elif manifest.get("schema") == REVIEW_SCHEMA_V1:
        validate_legacy_review_snapshot(snapshot, previous, manifest)
    else:
        raise ContextctlError("published review attestation schema is invalid")


def validate_attested_chain(task_root: Path, chain: list[Snapshot]) -> None:
    for index, snapshot in enumerate(chain):
        previous = chain[index - 1] if index else None
        validate_published_attestation(task_root, snapshot, previous)


def audited_continuity_chain(task_root: Path) -> list[Snapshot]:
    chain = audited_chain(task_root)
    validate_attested_chain(task_root, chain)
    return chain


def archived_review_manifest(
    task_root: Path, snapshot: Snapshot
) -> dict[str, object] | None:
    path = (
        task_root
        / "reviews"
        / f"{snapshot.seq:04d}-{snapshot.meta['trigger']}.json"
    )
    if not path.is_file() or path.is_symlink():
        return None
    manifest, _ = load_manifest(path)
    return manifest


def apply_bound_evidence_health(
    records: list[dict[str, str | None]], manifest: dict[str, object] | None
) -> None:
    if not manifest:
        return
    health = manifest.get("evidence_health")
    if not isinstance(health, list):
        return
    bindings: dict[tuple[str, str | None], dict[str, object]] = {}
    for item in health:
        if not isinstance(item, dict) or not isinstance(
            item.get("content_bindings"), list
        ):
            continue
        for binding in item["content_bindings"]:
            if isinstance(binding, dict):
                bindings[(str(item.get("id")), binding.get("pointer"))] = binding
    for record in records:
        bound = bindings.get((str(record["id"]), record.get("pointer")))
        if not bound:
            continue
        current_status = str(record["status"])
        bound_status = str(bound.get("detected_status"))
        if current_status in {"OK", "EXTERNAL"} and current_status != bound_status:
            record["status"] = "CHANGED"
            continue
        if current_status != "OK":
            continue
        resolved = record.get("resolved_path")
        if resolved != bound.get("canonical_path"):
            record["status"] = "CHANGED"
            continue
        try:
            if sha256_file(Path(str(resolved))) != bound.get("sha256"):
                record["status"] = "CHANGED"
        except OSError:
            record["status"] = "MISSING"


def current_source_health(
    manifest: dict[str, object] | None,
) -> dict[str, object]:
    source = manifest.get("initial_source") if manifest else None
    if not isinstance(source, dict):
        return {"status": "NOT_APPLICABLE", "path": None, "historical": False}
    if source.get("available") is False:
        return {"status": "UNAVAILABLE", "path": None, "historical": False}
    canonical_path = source.get("canonical_path")
    if not isinstance(canonical_path, str):
        return {"status": "UNSAFE", "path": None, "historical": False}
    path = Path(canonical_path)
    if path.is_symlink():
        return {
            "status": "UNSAFE",
            "path": canonical_path,
            "historical": False,
        }
    if not path.is_file():
        return {
            "status": "MISSING",
            "path": canonical_path,
            "historical": False,
        }
    try:
        status = "OK" if sha256_file(path) == source.get("sha256") else "CHANGED"
    except OSError:
        status = "MISSING"
    return {"status": status, "path": canonical_path, "historical": False}


def resume_payload(task_root: Path) -> dict[str, object]:
    task_root = task_root.expanduser().resolve(strict=False)
    if not task_root.is_dir():
        raise ContextctlError(f"task root is not a directory: {task_root}")
    chain = audited_continuity_chain(task_root)
    snapshot = chain[-1]
    if snapshot.meta["task_id"] != task_root.name:
        raise ContextctlError("task directory name does not match checkpoint task_id")
    archived_review = (
        task_root
        / "reviews"
        / f"{snapshot.seq:04d}-{snapshot.meta['trigger']}.json"
    )
    legacy_allowed = (
        not archived_review.exists()
        and snapshot.meta.get("review_required") != REVIEW_SCHEMA
        and not (task_root / "reviews").exists()
    )
    validate_resume_projection(
        snapshot, allow_legacy_action_marker=legacy_allowed
    )
    validate_published_attestation(
        task_root, snapshot, chain[-2] if len(chain) > 1 else None
    )
    archived_manifest = archived_review_manifest(task_root, snapshot)

    evidence_lines = [
        line
        for line in section_lines(snapshot, "## Evidence Pointers")
        if re.search(r"\bE\d{3}\b", line)
    ]
    evidence = [
        record
        for line in evidence_lines
        for record in evidence_records(line, task_workspace(task_root))
    ]
    apply_bound_evidence_health(evidence, archived_manifest)
    source = current_source_health(archived_manifest)
    if source["status"] == "NOT_APPLICABLE":
        for historical_snapshot in reversed(chain[:-1]):
            historical_manifest = archived_review_manifest(
                task_root, historical_snapshot
            )
            historical_source = current_source_health(historical_manifest)
            if historical_source["status"] != "NOT_APPLICABLE":
                historical_source["historical"] = True
                source = historical_source
                break
    warnings = [
        f"{record['id']} {record['status']} {record['pointer'] or '<no single pointer>'}"
        for record in evidence
        if record["status"] != "OK"
    ]
    if not source["historical"] and source["status"] in {
        "MISSING",
        "CHANGED",
        "UNSAFE",
    }:
        warnings.append(
            f"initial source {source['status']} {source['path'] or '<no path>'}"
        )
    if snapshot.meta["status"] in {"waiting", "blocked"}:
        warnings.append(f"task status is {snapshot.meta['status']}")
    working_state = working_state_health(task_root)
    if any(value != "ABSENT" for value in working_state.values()):
        warnings.append(
            "unpublished working state exists; run doctor before the published action"
        )

    readiness = "READY"
    if snapshot.meta["status"] in {"waiting", "blocked"}:
        readiness = "BLOCKED"
    elif warnings:
        readiness = "WARNING"

    return {
        "schema": SCHEMA,
        "task_id": snapshot.meta["task_id"],
        "checkpoint": snapshot.path.name,
        "seq": snapshot.seq,
        "status": snapshot.meta["status"],
        "readiness": readiness,
        "warnings": warnings,
        "contract": section_lines(snapshot, "## Contract"),
        "current_state": section_lines(snapshot, "## Current State"),
        "decisions": section_lines(snapshot, "## Active Decisions"),
        "transitions": transition_records(snapshot),
        "next_action": section_lines(snapshot, "## Next Action")[0],
        "verification": section_lines(snapshot, "## Verification for Next Action")[0],
        "evidence": evidence,
        "source": source,
        "blockers": section_lines(snapshot, "## Blockers and Open Questions"),
        "failures": section_lines(snapshot, "## Failed Attempts Not To Repeat"),
        "unknowns": section_lines(snapshot, "## Assumptions and Unknowns"),
        "working_state": working_state,
    }


def join_items(items: list[str]) -> str:
    return " | ".join(items) if items else "none"


def contract_group(contract: list[str], prefix: str) -> list[str]:
    return [line for line in contract if re.match(rf"^- {prefix}\d{{3}}\b", line)]


def evidence_summary(records: list[dict[str, str | None]], status: str) -> str:
    values = [
        f"{record['id']} `{record['pointer'] or '<no single pointer>'}`"
        for record in records
        if record["status"] == status
    ]
    return join_items(values)


def status_labels(status: str) -> tuple[str, str | None, str]:
    return {
        "active": ("CURRENT", "DO NOW", "DONE WHEN"),
        "verifying": ("CURRENT", "VERIFY NOW", "PASS WHEN"),
        "waiting": ("WAITING STATE", "WAITING ON", "RESUME WHEN"),
        "blocked": ("BLOCKED STATE", "BLOCKED ON", "UNBLOCK WHEN"),
        "complete": ("OUTCOME", None, "FINAL EVIDENCE"),
    }[status]


def item_ids(items: object, prefixes: str) -> list[str]:
    if not isinstance(items, list):
        return []
    found: list[str] = []
    for line in items:
        if not isinstance(line, str):
            continue
        for item_id in re.findall(rf"\b[{prefixes}]\d{{3}}\b", line):
            if item_id not in found:
                found.append(item_id)
    return found


def evidence_counts(records: object) -> str:
    if not isinstance(records, list):
        return "none"
    statuses = ("OK", "MISSING", "EXTERNAL", "UNSAFE", "CHANGED", "UNCHECKED")
    counts = {
        status: sum(
            1
            for record in records
            if isinstance(record, dict) and record.get("status") == status
        )
        for status in statuses
    }
    return " ".join(f"{status}={counts[status]}" for status in statuses)


def render_card(payload: dict[str, object]) -> str:
    """Render the bounded daily-resume view; full canonical state stays in payload."""
    contract = payload["contract"]
    assert isinstance(contract, list)
    status = str(payload["status"])
    _, action_label, verify_label = status_labels(status)
    work_ids = item_ids(payload["current_state"], "W")
    decision_ids = item_ids(payload["decisions"], "D")
    blocker_ids = item_ids(payload["blockers"], "B")
    current_state = payload["current_state"]
    assert isinstance(current_state, list)
    verified_count = sum(line.startswith("- Verified:") for line in current_state)
    working_state = payload["working_state"]
    assert isinstance(working_state, dict)
    lines = [
        f"{payload['readiness']} · {payload['task_id']} · #{int(payload['seq']):04d} · {status.upper()}",
        f"CHECKPOINT: {payload['checkpoint']}",
        (
            "WORKING STATE: clean"
            if all(value == "ABSENT" for value in working_state.values())
            else "RUN DOCTOR FIRST: "
            + ", ".join(f"{key}={value}" for key, value in working_state.items())
        ),
        f"GOAL: {join_items(contract_group(contract, 'G'))}",
        f"STATE: verified={verified_count} · open={','.join(work_ids) or 'none'}",
        "ACTIVE IDS: "
        f"decisions={','.join(decision_ids) or 'none'} · "
        f"blockers={','.join(blocker_ids) or 'none'}",
    ]
    if action_label is not None:
        lines.append(f"{action_label}: {payload['next_action']}")
    source = payload["source"]
    assert isinstance(source, dict)
    source_status = (
        ("HISTORICAL " if source.get("historical") else "")
        + str(source["status"])
    )
    lines.extend(
        [
            f"{verify_label}: {payload['verification']}",
            f"HEALTH: source={source_status} · evidence {evidence_counts(payload['evidence'])}",
        ]
    )
    warnings = payload["warnings"]
    assert isinstance(warnings, list)
    if warnings:
        visible = warnings[:BRIEF_ATTENTION_LIMIT]
        remainder = len(warnings) - len(visible)
        lines.append(
            "ATTENTION: "
            + " | ".join(str(value) for value in visible)
            + (f" | +{remainder} more" if remainder else "")
        )
    lines.append(
        "FULL CONTEXT: run `contextctl.py resume <task-dir> --full` before a cold "
        "handoff, contract decision, or external mutation."
    )
    if len(lines) > BRIEF_CARD_LINE_LIMIT:
        raise ContextctlError(
            f"brief resume card needs {len(lines)} lines; limit is {BRIEF_CARD_LINE_LIMIT}"
        )
    return "\n".join(lines) + "\n"


def render_full_card(payload: dict[str, object]) -> str:
    contract = payload["contract"]
    assert isinstance(contract, list)
    status = str(payload["status"])
    current_label, action_label, verify_label = status_labels(status)
    lines = [
        f"{payload['readiness']} · {payload['task_id']} · #{int(payload['seq']):04d} · {status.upper()}",
        f"CHECKPOINT: {payload['checkpoint']}",
        (
            "WORKING STATE: clean"
            if all(value == "ABSENT" for value in payload["working_state"].values())
            else "RUN DOCTOR FIRST: "
            + ", ".join(
                f"{key}={value}" for key, value in payload["working_state"].items()
            )
        ),
        f"GOAL: {join_items(contract_group(contract, 'G'))}",
        f"IN SCOPE: {join_items(contract_group(contract, 'I'))}",
        f"OUT OF SCOPE: {join_items(contract_group(contract, 'O'))}",
        f"SUCCESS: {join_items(contract_group(contract, 'S'))}",
        f"CONSTRAINTS: {join_items(contract_group(contract, 'C'))}",
        f"{current_label}: {join_items(payload['current_state'])}",
        f"DECISIONS: {join_items(payload['decisions'])}",
        f"SUCCESSOR TRANSITIONS: {join_items(payload['transitions'])}",
    ]
    if action_label is not None:
        lines.append(f"{action_label}: {payload['next_action']}")
    lines.extend(
        [
            f"{verify_label}: {payload['verification']}",
            "SOURCE: "
            + ("HISTORICAL " if payload["source"].get("historical") else "")
            + f"{payload['source']['status']}"
            + (
                f" `{payload['source']['path']}`"
                if payload["source"].get("path")
                else ""
            ),
            f"EVIDENCE OK: {evidence_summary(payload['evidence'], 'OK')}",
            f"EVIDENCE MISSING: {evidence_summary(payload['evidence'], 'MISSING')}",
            f"EVIDENCE EXTERNAL: {evidence_summary(payload['evidence'], 'EXTERNAL')}",
            f"EVIDENCE UNSAFE: {evidence_summary(payload['evidence'], 'UNSAFE')}",
            f"EVIDENCE CHANGED: {evidence_summary(payload['evidence'], 'CHANGED')}",
            f"EVIDENCE UNCHECKED: {evidence_summary(payload['evidence'], 'UNCHECKED')}",
            f"BLOCKERS: {join_items(payload['blockers'])}",
            f"DO NOT REPEAT: {join_items(payload['failures'])}",
            f"UNKNOWNS: {join_items(payload['unknowns'])}",
        ]
    )
    if len(lines) > CARD_LINE_LIMIT:
        raise ContextctlError(
            f"resume card needs {len(lines)} lines; limit is {CARD_LINE_LIMIT}"
        )
    return "\n".join(lines) + "\n"


def quiet_call(function: object, *args: object) -> None:
    with contextlib.redirect_stdout(io.StringIO()):
        function(*args)


def set_optional_meta(path: Path, key: str, value: str) -> None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ContextctlError("candidate frontmatter is incomplete") from exc
    matches = [index for index in range(1, end) if lines[index].startswith(f"{key}:")]
    if len(matches) > 1:
        raise ContextctlError(f"candidate has duplicate {key} metadata")
    entry = f"{key}: {value}"
    if matches:
        lines[matches[0]] = entry
    else:
        lines.insert(end, entry)
    suffix = "\n" if text.endswith("\n") else ""
    atomic_replace(path, "\n".join(lines) + suffix)


def replace_section_text(text: str, heading: str, body: str) -> str:
    marker = f"{heading}\n\n"
    if text.count(marker) != 1:
        raise ContextctlError(f"candidate must contain exactly one {heading}")
    start = text.index(marker) + len(marker)
    next_heading = text.find("\n## ", start)
    end = len(text) if next_heading == -1 else next_heading + 1
    replacement = body.strip() + "\n\n"
    return text[:start] + replacement + text[end:]


def replace_section_body(path: Path, heading: str, body: str) -> None:
    text = path.read_text(encoding="utf-8")
    atomic_replace(path, replace_section_text(text, heading, body))


def chain_fingerprint(chain: list[Snapshot]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (
            snapshot.path.name,
            hashlib.sha256(snapshot.text.encode("utf-8")).hexdigest(),
        )
        for snapshot in chain
    )


def later_candidate_text(latest: Snapshot, trigger: str) -> str:
    return replace_meta(
        latest.text,
        {
            "seq": str(latest.seq + 1),
            "previous": latest.path.name,
            "previous_sha256": "auto",
            "contract_sha256": "auto",
            "trigger": trigger,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        },
    )


def remove_owned_candidate(candidate: Path, expected_sha256: str) -> None:
    try:
        if candidate.is_symlink() or not candidate.is_file():
            raise ContextctlError("completion candidate became missing or unsafe")
        if sha256_file(candidate) != expected_sha256:
            raise ContextctlError("completion candidate changed concurrently")
        candidate.unlink()
    except Exception as exc:
        raise ContextctlError(f"completion rollback incomplete: {exc}") from exc


def complete_candidate(task_root: Path) -> Path:
    task_root = task_root.expanduser().resolve(strict=False)
    candidate = task_root / "candidate.md"
    if candidate.exists() or candidate.is_symlink():
        raise ContextctlError(f"candidate already exists: {candidate}")
    chain = audited_continuity_chain(task_root)
    latest = chain[-1]
    if latest.meta["status"] == "complete":
        raise ContextctlError("latest checkpoint is already complete")
    original_fingerprint = chain_fingerprint(chain)
    historical_blocker_ids = sorted(
        {
            item_id
            for snapshot in chain
            for item_id in snapshot.state_ids
            if item_id.startswith("B")
        }
    )
    next_blocker_number = max(
        (int(item_id[1:]) for item_id in historical_blocker_ids), default=0
    ) + 1
    if next_blocker_number > 999:
        raise ContextctlError("cannot allocate a terminal blocker ID above B999")
    terminal_blocker = f"B{next_blocker_number:03d}"
    candidate_text = later_candidate_text(latest, "completion")
    work_ids = sorted(item_id for item_id in latest.state_ids if item_id.startswith("W"))
    blocker_ids = sorted(
        item_id for item_id in latest.state_ids if item_id.startswith("B")
    )
    current_lines = section(latest.body, "## Current State").splitlines()
    updated_current: list[str] = []
    for line in current_lines:
        if line.startswith("- In progress:"):
            updated_current.append("- In progress: none — task complete.")
        elif line.startswith("- Remaining:"):
            updated_current.append("- Remaining: none — task complete.")
        else:
            updated_current.append(line)
    candidate_text = replace_meta(
        candidate_text,
        {"status": "complete", "contract_sha256": "auto"},
    )
    candidate_text = replace_section_text(
        candidate_text, "## Current State", "\n".join(updated_current)
    )
    candidate_text = replace_section_text(
        candidate_text, "## Next Action", "- None — task complete."
    )
    candidate_text = replace_section_text(
        candidate_text,
        "## Blockers and Open Questions",
        f"- {terminal_blocker} — None — task complete; no blocker or open question remains.",
    )
    resolution = ", ".join(work_ids) if work_ids else "none"
    blocker_transition = ", ".join(
        f"{item_id} -> {terminal_blocker}" for item_id in blocker_ids
    )
    verification_ids = sorted(
        set(
            re.findall(
                r"\bE\d{3}\b",
                section(latest.body, "## Verification for Next Action"),
            )
        )
    )
    authorization = ", ".join(verification_ids) if verification_ids else "final verification"
    completion_delta = "\n".join(
        (
            f"- Added: {terminal_blocker} — terminal no-blocker state.",
            "- Changed: task status became complete; Current State and Next Action became terminal.",
            (
                f"- Superseded: {blocker_transition} — prior blocker state closed by completion."
                if blocker_transition
                else "- Superseded: none."
            ),
            f"- Resolved or cancelled: {resolution} — explicit completion command.",
            "- Compressed or dropped: none.",
            f"- Authorized by: {authorization} and the explicit completion command.",
            "- Growth reason: terminal checkpoint.",
        )
    )
    candidate_text = replace_section_text(
        candidate_text, "## Delta From Previous", completion_delta
    )
    completed = Snapshot.from_text(candidate, candidate_text, allow_auto=True)
    validate_pair(completed, latest, allow_auto=True)
    validate_resume_projection(completed, allow_legacy_action_marker=False)
    if chain_fingerprint(audited_continuity_chain(task_root)) != original_fingerprint:
        raise ContextctlError(
            "checkpoint chain changed during completion construction; rerun complete"
        )
    temporary_link = atomic_publish(candidate, candidate_text)
    candidate_sha256 = hashlib.sha256(candidate_text.encode("utf-8")).hexdigest()
    try:
        if sha256_file(candidate) != candidate_sha256:
            raise ContextctlError("completion candidate changed concurrently")
        if chain_fingerprint(audited_continuity_chain(task_root)) != original_fingerprint:
            raise ContextctlError(
                "checkpoint chain changed during completion candidate creation; rerun complete"
            )
    except Exception as original_error:
        rollback_errors: list[str] = []
        try:
            remove_owned_candidate(candidate, candidate_sha256)
        except Exception as rollback_error:
            rollback_errors.append(str(rollback_error))
        if temporary_link is not None:
            try:
                temporary_link.unlink(missing_ok=True)
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise ContextctlError(
                "completion rollback incomplete: " + "; ".join(rollback_errors)
            ) from original_error
        raise
    if temporary_link is not None:
        try:
            temporary_link.unlink(missing_ok=True)
        except OSError as cleanup_error:
            print(
                "WARNING completion candidate published but temporary link remains: "
                f"{temporary_link}: {cleanup_error}",
                file=sys.stderr,
            )
    return candidate


def initial_draft_text(task_id: str) -> str:
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    return f"""---
schema: context-continuity/v1
task_id: {task_id}
seq: 1
previous: none
previous_sha256: auto
contract_version: 1
contract_sha256: auto
status: active
trigger: init
created_at: {created_at}
workspace_revision: not-available
---

# {task_id}

## Contract

- G001 Goal: REPLACE-ME with the concrete outcome.
- I001 In scope: REPLACE-ME with the bounded surface.
- O001 Out of scope: REPLACE-ME with explicit exclusions.
- S001 Success: REPLACE-ME with measurable evidence.
- C001 Constraint: REPLACE-ME with the exact user boundary.

## Current State

- Verified: E001 will support the initial verified fact after REPLACE-ME is removed.
- In progress: initial contract lock.
- Remaining: W001 — REPLACE-ME with the first open outcome.

## Active Decisions

- D001 — REPLACE-ME with the first controlling decision. Why: REPLACE-ME. Supersedes: none.

## Next Action

- W001 — REPLACE-ME with one concrete action.

## Verification for Next Action

- REPLACE-ME with one observable check and required result.

## Evidence Pointers

- E001 — `REPLACE-ME` — supports S001 and W001.

## Blockers and Open Questions

- B001 — REPLACE-ME, or state that no blocker exists.

## Failed Attempts Not To Repeat

- F001 — REPLACE-ME, or state that no failed approach is active.

## Assumptions and Unknowns

- A001 — REPLACE-ME with an explicit assumption or unknown.

## Delta From Previous

- Added: initial checkpoint.
- Changed: none.
- Superseded: none.
- Resolved or cancelled: none.
- Compressed or dropped: none.
- Authorized by: initial task request.
- Growth reason: initial checkpoint.

## Retention Audit

- [x] contract
- [x] decisions
- [x] open-work-and-blockers
- [x] evidence-and-failures
- [x] next-action-and-check
"""


def draft_candidate(task_root: Path, trigger: str) -> Path:
    raw_task_root = task_root.expanduser()
    validate_task_id(raw_task_root.name)
    validate_trigger(trigger, enforce_length=True)
    task_root = raw_task_root.resolve(strict=False)
    validate_task_id(task_root.name)
    candidate_path = task_root / "candidate.md"
    if candidate_path.exists() or candidate_path.is_symlink():
        raise ContextctlError(f"candidate already exists: {candidate_path}")
    checkpoints = scan_checkpoints(task_root)
    if not checkpoints:
        validate_task_id(raw_task_root.name, enforce_length=True)
        validate_task_id(task_root.name, enforce_length=True)
        if trigger != "init":
            raise ContextctlError("a task without checkpoints must use trigger init")
        task_root.mkdir(parents=True, exist_ok=True)
        atomic_publish(candidate_path, initial_draft_text(task_root.name))
        return candidate_path
    if trigger == "init":
        raise ContextctlError("an existing task cannot create a second init checkpoint")
    chain = audited_continuity_chain(task_root)
    candidate_text = later_candidate_text(chain[-1], trigger)
    candidate_snapshot = Snapshot.from_text(
        candidate_path, candidate_text, allow_auto=True
    )
    validate_pair(candidate_snapshot, chain[-1], allow_auto=True)
    atomic_publish(candidate_path, candidate_text)
    return candidate_path


def prepared_candidate(candidate_path: Path) -> tuple[Snapshot, Snapshot | None]:
    candidate_path = candidate_path.expanduser().resolve(strict=False)
    task_root = candidate_path.parent
    checkpoints = scan_checkpoints(task_root)
    chain = audited_continuity_chain(task_root) if checkpoints else []
    previous = chain[-1] if chain else None
    candidate = Snapshot.load(candidate_path)
    validate_pair(candidate, previous, allow_auto=False)
    return candidate, previous


def snapshot_task_root(snapshot: Snapshot) -> Path:
    if snapshot.path.parent.name == "checkpoints":
        return snapshot.path.parent.parent.resolve(strict=False)
    return snapshot.path.parent.resolve(strict=False)


def review_template(
    snapshot: Snapshot,
    previous: Snapshot | None,
    initial_source: dict[str, object] | None = None,
) -> dict[str, object]:
    required_ids = sorted(snapshot.state_ids)
    next_action = section_lines(snapshot, "## Next Action")[0]
    verification = section_lines(snapshot, "## Verification for Next Action")[0]
    source_review: dict[str, object] | None = None
    if initial_source is not None:
        if initial_source.get("available") is True:
            source_ids = list(initial_source.get("detected_ids", []))
            source_review = {
                "required_ids": source_ids,
                "recovered_ids": [],
                "missing_ids": source_ids,
                "coverage_status": "UNCHECKED",
                "omissions": [],
            }
        else:
            source_review = {
                "required_ids": [],
                "recovered_ids": [],
                "missing_ids": [],
                "coverage_status": "NOT_APPLICABLE",
                "omissions": [],
            }
    return {
        "schema": REVIEW_SCHEMA,
        "canonical_task_root": str(snapshot_task_root(snapshot)),
        "task_id": snapshot.meta["task_id"],
        "seq": snapshot.seq,
        "trigger": snapshot.meta["trigger"],
        "parent_filename": previous.path.name if previous else "none",
        "parent_sha256": sha256_file(previous.path) if previous else "none",
        "candidate_sha256": sha256_file(snapshot.path),
        "required_ids": required_ids,
        "required_item_sha256": {
            item_id: hashlib.sha256((text + "\n").encode("utf-8")).hexdigest()
            for item_id, text in sorted(snapshot.state_items.items())
        },
        "next_action_sha256": hashlib.sha256((next_action + "\n").encode("utf-8")).hexdigest(),
        "verification_sha256": hashlib.sha256((verification + "\n").encode("utf-8")).hexdigest(),
        "initial_source": initial_source,
        "source_review": source_review,
        "source_detected_ids": (
            list(initial_source.get("detected_ids", []))
            if initial_source and initial_source.get("available") is True
            else []
        ),
        "recovered_ids": [],
        "missing_ids": required_ids,
        "reviewer": "",
        "reviewed_at": "",
        "review_mode": "fresh-agent-source-first",
        "instructions": [
            "For an available initial_source, open the bound source canonical_path before the candidate, recover every source required_id, and check source-only durable constraints and permissions; set source_review COMPLETE only when none are omitted.",
            "After the source-first check when applicable, read the prepared candidate and its cited evidence; do not use other task state.",
            "Set reviewer and reviewed_at, copy every required_id to recovered_ids, and empty missing_ids only after recovery.",
            "Copy the exact Next Action and Verification bullets; a single terminal newline is accepted, but no other whitespace change is. Record every contradiction, ambiguity, or evidence issue.",
            "Do not edit detected_status: it is machine-derived from the exact candidate and task workspace.",
            "Set status to the same OK or EXTERNAL value as detected_status after opening the pointer; a missing relative or unsafe pointer is never EXTERNAL and cannot PASS.",
            "Set semantic_status to DIRECT only for direct support, REPORTED only for an explicitly attributed source report, or UNSUPPORTED when the claim is not supported; UNSUPPORTED cannot PASS.",
            "Set verdict to PASS only when coverage is complete and every issue list is empty.",
        ],
        "contradictions": [],
        "ambiguities": [],
        "recovered_next_action": "",
        "recovered_verification": "",
        "evidence_health": detected_review_evidence(snapshot),
        "verdict": "INCOMPLETE",
    }


def restore_review_candidate(
    candidate_path: Path, original: str, original_error: Exception
) -> None:
    try:
        if candidate_path.is_symlink() or not candidate_path.is_file():
            raise ContextctlError("candidate became missing or unsafe")
        if candidate_path.read_text(encoding="utf-8") != original:
            atomic_replace(candidate_path, original)
    except Exception as rollback_error:
        raise ContextctlError(
            f"review-init rollback incomplete: {rollback_error}"
        ) from original_error


def initialize_review(
    candidate_path: Path,
    output_path: Path,
    source_path: Path | None = None,
    no_source_reason: str | None = None,
    replace_existing: bool = False,
) -> dict[str, object]:
    candidate_path = candidate_path.expanduser().resolve(strict=False)
    task_root = candidate_path.parent
    canonical_output = task_root / "review.json"
    raw_output = output_path.expanduser()
    if raw_output.is_symlink() or raw_output.resolve(strict=False) != canonical_output:
        raise ContextctlError(
            f"review output must be canonical task review.json: {canonical_output}"
        )
    if canonical_output == candidate_path:
        raise ContextctlError("review output must not overlap the candidate")
    if source_path is not None and source_path.expanduser().resolve(strict=False) == canonical_output:
        raise ContextctlError("review output must not overlap the initial source")
    if source_path is not None:
        canonical_initial_source(candidate_path, source_path)
    existing_review_hash: str | None = None
    if canonical_output.exists():
        if canonical_output.is_symlink() or not canonical_output.is_file():
            raise ContextctlError("existing canonical review output is unsafe")
        existing, _ = load_manifest(canonical_output)
        if existing.get("schema") not in {REVIEW_SCHEMA_V1, REVIEW_SCHEMA}:
            raise ContextctlError(
                "existing canonical review.json is not a review for this task"
            )
        if existing.get("canonical_task_root") != str(task_root):
            raise ContextctlError(
                "review belongs to a different canonical task root "
                f"(bound={existing.get('canonical_task_root')}; current={task_root})"
            )
        if not replace_existing:
            raise ContextctlError(
                "canonical review.json already exists; inspect it or rerun with --replace-existing"
            )
        existing_review_hash = sha256_file(canonical_output)
    if "REPLACE-ME" in candidate_path.read_text(encoding="utf-8"):
        raise ContextctlError("replace every REPLACE-ME marker before review-init")
    checkpoints = scan_checkpoints(task_root)
    chain = audited_continuity_chain(task_root) if checkpoints else []
    previous_path = chain[-1].path if chain else None
    original = candidate_path.read_text(encoding="utf-8")
    try:
        set_optional_meta(candidate_path, "review_required", REVIEW_SCHEMA)
        quiet_call(prepare, candidate_path, previous_path)
        snapshot, previous = prepared_candidate(candidate_path)
        validate_resume_projection(snapshot, allow_legacy_action_marker=False)
        if snapshot.seq == 1:
            if source_path is None and no_source_reason is None:
                raise ContextctlError(
                    "initial review requires --source or --no-source-reason"
                )
            source = (
                initial_source_binding(snapshot, source_path)
                if source_path is not None
                else no_source_binding(str(no_source_reason))
            )
        else:
            if source_path is not None or no_source_reason is not None:
                raise ContextctlError(
                    "--source and --no-source-reason are allowed only for an initial checkpoint"
                )
            source = None
        manifest = review_template(snapshot, previous, source)
    except Exception as exc:
        restore_review_candidate(candidate_path, original, exc)
        raise
    manifest_text = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )
    try:
        if existing_review_hash is None:
            atomic_publish(canonical_output, manifest_text)
        else:
            if (
                canonical_output.is_symlink()
                or not canonical_output.is_file()
                or sha256_file(canonical_output) != existing_review_hash
            ):
                raise ContextctlError(
                    "canonical review.json changed after it was inspected"
                )
            atomic_replace(canonical_output, manifest_text)
    except Exception as exc:
        restore_review_candidate(candidate_path, original, exc)
        raise
    return manifest


def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ContextctlError(f"review manifest contains duplicate key: {key}")
        result[key] = value
    return result


def load_manifest(path: Path) -> tuple[dict[str, object], str]:
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    if not isinstance(payload, dict):
        raise ContextctlError("review manifest must be a JSON object")
    return payload, raw


def validate_review_snapshot(
    candidate: Snapshot,
    previous: Snapshot | None,
    manifest: dict[str, object],
    live_bindings: bool = True,
) -> None:
    if candidate.meta.get("review_required") != REVIEW_SCHEMA:
        raise ContextctlError("candidate does not require the current review schema")
    validate_resume_projection(candidate, allow_legacy_action_marker=False)
    source_value = manifest.get("initial_source")
    if candidate.seq == 1:
        if not isinstance(source_value, dict):
            raise ContextctlError(
                "initial review requires a bound source or explicit no-source provenance"
            )
        if source_value.get("available") is True:
            canonical_path = source_value.get("canonical_path")
            if not isinstance(canonical_path, str) or not canonical_path:
                raise ContextctlError("initial source canonical_path is required")
            if live_bindings:
                recomputed_source = initial_source_binding(
                    candidate, Path(canonical_path)
                )
            else:
                validate_frozen_source_binding(
                    candidate, source_value, require_v2=True
                )
                recomputed_source = source_value
        elif source_value.get("available") is False:
            reason = source_value.get("reason")
            if not isinstance(reason, str):
                raise ContextctlError("initial no-source provenance reason is required")
            recomputed_source = no_source_binding(reason)
        else:
            raise ContextctlError("initial source availability is invalid")
        if source_value != recomputed_source:
            raise ContextctlError("initial source binding changed after review-init")
    else:
        recomputed_source = None
        if source_value is not None:
            raise ContextctlError("non-initial review must not contain an initial source")
    expected = review_template(candidate, previous, recomputed_source)
    if manifest.get("schema") != REVIEW_SCHEMA:
        raise ContextctlError("review schema is invalid")
    if manifest.get("source_detected_ids") != expected["source_detected_ids"]:
        raise ContextctlError("source_detected_ids changed after review-init")
    if manifest.get("canonical_task_root") != expected["canonical_task_root"]:
        raise ContextctlError(
            "review belongs to a different canonical task root "
            f"(bound={manifest.get('canonical_task_root')}; "
            f"current={expected['canonical_task_root']})"
        )
    if manifest.get("task_id") != candidate.meta["task_id"] or manifest.get("seq") != candidate.seq:
        raise ContextctlError("review belongs to a different task or sequence")
    actual_hash = sha256_file(candidate.path)
    if manifest.get("candidate_sha256") != actual_hash:
        raise ContextctlError("candidate SHA-256 changed after review")
    for key in (
        "parent_filename",
        "parent_sha256",
        "required_item_sha256",
        "next_action_sha256",
        "verification_sha256",
    ):
        if manifest.get(key) != expected[key]:
            raise ContextctlError(f"review binding changed: {key}")

    required_ids = list(expected["required_ids"])
    supplied_required = manifest.get("required_ids")
    if supplied_required != required_ids:
        missing = sorted(set(required_ids) - set(supplied_required or []))
        extra = sorted(set(supplied_required or []) - set(required_ids))
        raise ContextctlError(f"review required IDs differ; missing={missing} extra={extra}")
    recovered_ids = manifest.get("recovered_ids")
    if recovered_ids != required_ids or len(set(recovered_ids or [])) != len(required_ids):
        missing = sorted(set(required_ids) - set(recovered_ids or []))
        raise ContextctlError(f"review is missing protected IDs: {missing}")
    if manifest.get("missing_ids") != []:
        raise ContextctlError(f"review remains INCOMPLETE: missing_ids={manifest.get('missing_ids')}")
    if not manifest.get("reviewer") or not manifest.get("reviewed_at"):
        raise ContextctlError("reviewer and reviewed_at are required")
    if manifest.get("review_mode") != "fresh-agent-source-first":
        raise ContextctlError("review must use fresh-agent-source-first mode")
    if manifest.get("verdict") != "PASS":
        raise ContextctlError("review verdict is INCOMPLETE")
    for key in ("contradictions", "ambiguities"):
        if manifest.get(key) != []:
            raise ContextctlError(f"review has unresolved {key}")

    expected_source_review = expected["source_review"]
    source_review = manifest.get("source_review")
    if expected_source_review is None:
        if source_review is not None:
            raise ContextctlError("non-initial review must not contain source_review")
    elif not isinstance(source_review, dict) or not isinstance(
        expected_source_review, dict
    ):
        raise ContextctlError("source_review must be an object")
    elif recomputed_source and recomputed_source.get("available") is True:
        required_source_ids = list(expected_source_review["required_ids"])
        if source_review.get("required_ids") != required_source_ids:
            raise ContextctlError("source_review required IDs changed")
        if source_review.get("recovered_ids") != required_source_ids:
            raise ContextctlError("source_review did not recover every source ID")
        if source_review.get("missing_ids") != []:
            raise ContextctlError("source_review still has missing source IDs")
        if source_review.get("coverage_status") != "COMPLETE":
            raise ContextctlError("source_review coverage_status must be COMPLETE")
        if source_review.get("omissions") != []:
            raise ContextctlError("source_review has unresolved durable omissions")
    elif source_review != expected_source_review:
        raise ContextctlError("no-source review provenance changed")

    next_action = section_lines(candidate, "## Next Action")[0]
    verification = section_lines(candidate, "## Verification for Next Action")[0]
    if manifest.get("recovered_next_action") not in {next_action, next_action + "\n"}:
        raise ContextctlError("review next action does not exactly match the candidate")
    if manifest.get("recovered_verification") not in {verification, verification + "\n"}:
        raise ContextctlError("review verification does not exactly match the candidate")

    expected_evidence = sorted(item_id for item_id in required_ids if item_id.startswith("E"))
    evidence_health = manifest.get("evidence_health")
    if not isinstance(evidence_health, list):
        raise ContextctlError("review evidence_health must be a list")
    evidence_ids = [record.get("id") for record in evidence_health if isinstance(record, dict)]
    if sorted(evidence_ids) != expected_evidence or len(evidence_ids) != len(set(evidence_ids)):
        raise ContextctlError("review evidence coverage is incomplete")
    if not live_bindings:
        validate_frozen_evidence_bindings(candidate, evidence_health)
        return
    detected_by_id = {
        str(record["id"]): record for record in expected["evidence_health"]
    }
    for record in evidence_health:
        if not isinstance(record, dict):
            raise ContextctlError("review evidence record must be an object")
        item_id = str(record.get("id"))
        expected_record = detected_by_id[item_id]
        detected_status = str(expected_record["detected_status"])
        if record.get("detected_status") != detected_status:
            raise ContextctlError(
                f"review detected evidence status changed for {item_id}; "
                f"expected {detected_status}"
            )
        if record.get("content_bindings") != expected_record.get("content_bindings"):
            raise ContextctlError(
                f"review evidence content binding changed for {item_id}"
            )
        if detected_status not in {"OK", "EXTERNAL"}:
            raise ContextctlError(
                f"candidate evidence {item_id} is {detected_status}; repair the pointer before review"
            )
        if record.get("status") != detected_status:
            raise ContextctlError(
                f"review evidence status for {item_id} must match detected_status "
                f"{detected_status} after semantic verification"
            )
        semantic_status = record.get("semantic_status")
        if semantic_status not in {"DIRECT", "REPORTED"}:
            raise ContextctlError(
                f"review evidence semantic_status for {item_id} must be DIRECT or "
                "REPORTED after inspection"
            )


def validate_legacy_review_snapshot(
    candidate: Snapshot,
    previous: Snapshot | None,
    manifest: dict[str, object],
) -> None:
    expected = review_template(candidate, previous, None)
    for key in (
        "canonical_task_root",
        "task_id",
        "seq",
        "trigger",
        "parent_filename",
        "parent_sha256",
        "candidate_sha256",
        "required_ids",
        "required_item_sha256",
        "next_action_sha256",
        "verification_sha256",
    ):
        if manifest.get(key) != expected[key]:
            raise ContextctlError(f"legacy review attestation mismatch: {key}")
    required_ids = list(expected["required_ids"])
    if manifest.get("recovered_ids") != required_ids or manifest.get("missing_ids") != []:
        raise ContextctlError("legacy review protected-ID coverage is incomplete")
    if not manifest.get("reviewer") or not manifest.get("reviewed_at"):
        raise ContextctlError("legacy review provenance is incomplete")
    if manifest.get("review_mode") != "fresh-agent-candidate-only":
        raise ContextctlError("legacy review mode is invalid")
    if manifest.get("verdict") != "PASS":
        raise ContextctlError("legacy review verdict is not PASS")
    for key in ("contradictions", "ambiguities"):
        if manifest.get(key) != []:
            raise ContextctlError(f"legacy review has unresolved {key}")
    next_action = section_lines(candidate, "## Next Action")[0]
    verification = section_lines(candidate, "## Verification for Next Action")[0]
    if manifest.get("recovered_next_action") not in {next_action, next_action + "\n"}:
        raise ContextctlError("legacy review next action changed")
    if manifest.get("recovered_verification") not in {
        verification,
        verification + "\n",
    }:
        raise ContextctlError("legacy review verification changed")
    evidence = manifest.get("evidence_health")
    expected_evidence = sorted(
        item_id for item_id in required_ids if item_id.startswith("E")
    )
    if not isinstance(evidence, list) or sorted(
        record.get("id") for record in evidence if isinstance(record, dict)
    ) != expected_evidence:
        raise ContextctlError("legacy review evidence coverage is incomplete")
    for record in evidence:
        if not isinstance(record, dict) or record.get("status") not in {"OK", "EXTERNAL"}:
            raise ContextctlError("legacy review evidence status is invalid")
        if "semantic_status" in record and record.get("semantic_status") not in {
            "DIRECT",
            "REPORTED",
        }:
            raise ContextctlError("legacy review evidence semantics are invalid")
    source = manifest.get("initial_source")
    if isinstance(source, dict):
        validate_frozen_source_binding(candidate, source, require_v2=False)


def validate_review(
    candidate_path: Path, manifest_path: Path
) -> tuple[Snapshot, dict[str, object], str]:
    candidate, previous = prepared_candidate(candidate_path)
    manifest_path = manifest_path.expanduser().resolve(strict=False)
    if manifest_path != candidate.path.parent / "review.json" or manifest_path.is_symlink():
        raise ContextctlError("review manifest must be canonical task review.json")
    manifest, raw = load_manifest(manifest_path)
    validate_review_snapshot(candidate, previous, manifest)
    return candidate, manifest, raw


def publish_reviewed(candidate_path: Path, manifest_path: Path) -> Snapshot:
    candidate, manifest, raw_manifest = validate_review(candidate_path, manifest_path)
    task_root = candidate.path.parent
    review_dir = task_root / "reviews"
    review_target = review_dir / f"{candidate.seq:04d}-{candidate.meta['trigger']}.json"
    checkpoint_name = f"{candidate.seq:04d}-{candidate.meta['trigger']}.md"
    marker = review_target.with_name(f"{review_target.stem}.required.json")
    marker_text = json.dumps(
        {
            "schema": REVIEW_REQUIRED_SCHEMA,
            "task_id": candidate.meta["task_id"],
            "checkpoint": checkpoint_name,
            "checkpoint_sha256": str(manifest["candidate_sha256"]),
            "review": review_target.name,
            "review_sha256": hashlib.sha256(raw_manifest.encode("utf-8")).hexdigest(),
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"
    companions = [(review_target, raw_manifest), (marker, marker_text)]

    def precommit_check(
        locked_candidate: Snapshot,
        locked_previous: Snapshot | None,
        locked_chain: list[Snapshot],
    ) -> None:
        if review_dir.is_symlink() or (
            review_dir.exists() and not review_dir.is_dir()
        ):
            raise ContextctlError("published review directory is unsafe")
        validate_attested_chain(task_root, locked_chain)
        validate_review_snapshot(locked_candidate, locked_previous, manifest)

    with contextlib.redirect_stdout(io.StringIO()):
        published_path = promote(
            candidate.path,
            str(manifest["candidate_sha256"]),
            companions=companions,
            precommit_check=precommit_check,
        )
    published = Snapshot(
        published_path,
        candidate.text,
        candidate.meta,
        candidate.body,
        candidate.contract,
        candidate.delta,
        candidate.state_items,
    )
    manifest_path = manifest_path.expanduser().resolve(strict=False)
    try:
        if manifest_path.is_file() and hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest() == hashlib.sha256(raw_manifest.encode("utf-8")).hexdigest():
            manifest_path.unlink()
    except OSError as exc:
        print(
            f"WARNING published but could not remove working review manifest: {exc}",
            file=sys.stderr,
        )
    return published


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


def lock_fingerprint(lock: Path) -> tuple[int, int, str]:
    if lock.is_symlink() or not lock.is_dir():
        raise ContextctlError("writer lock is not a safe real directory")
    owner_path = lock / "owner.json"
    if owner_path.is_symlink() or not owner_path.is_file():
        raise ContextctlError("writer lock owner metadata is missing or unsafe")
    entries = list(lock.iterdir())
    if entries != [owner_path]:
        raise ContextctlError("writer lock contains unexpected files")
    stat = os.lstat(lock)
    return stat.st_dev, stat.st_ino, sha256_file(owner_path)


def diagnose_lock(task_root: Path) -> dict[str, object]:
    lock = task_root / ".checkpoint-write.lock"
    if not lock.exists() and not lock.is_symlink():
        return {"status": "ABSENT"}
    try:
        fingerprint = lock_fingerprint(lock)
        owner_path = lock / "owner.json"
        if owner_path.stat().st_size > 16 * 1024:
            raise ContextctlError("writer lock owner metadata exceeds 16 KiB")
        owner, _ = load_manifest(owner_path)
        if owner.get("schema") != LOCK_SCHEMA:
            raise ContextctlError("writer lock owner schema is invalid")
        pid = owner.get("pid")
        hostname = owner.get("hostname")
        if not isinstance(pid, int) or pid <= 1 or not isinstance(hostname, str):
            raise ContextctlError("writer lock owner identity is invalid")
        if hostname != socket.gethostname():
            return {
                "status": "UNKNOWN",
                "pid": pid,
                "hostname": hostname,
                "reason": "foreign host",
                "fingerprint": fingerprint,
            }
        alive = pid_is_alive(pid)
        recorded_start = owner.get("process_start")
        current_start = process_start_token(pid) if alive else None
        if alive and recorded_start and current_start and recorded_start != current_start:
            alive = False
        return {
            "status": "ACTIVE" if alive else "STALE",
            "pid": pid,
            "hostname": hostname,
            "operation": owner.get("operation"),
            "created_at": owner.get("created_at"),
            "candidate_sha256": owner.get("candidate_sha256"),
            "candidate_path": owner.get("candidate_path"),
            "lock_id": owner.get("nonce"),
            "fingerprint": fingerprint,
        }
    except (ContextctlError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"status": "UNKNOWN", "reason": str(exc)}


def working_state_health(task_root: Path) -> dict[str, str]:
    candidate = task_root / "candidate.md"
    review = task_root / "review.json"
    lock_status = str(diagnose_lock(task_root)["status"])
    candidate_present = candidate.exists() or candidate.is_symlink()
    review_present = review.exists() or review.is_symlink()
    candidate_status = "ABSENT"
    review_status = "ABSENT"

    if candidate_present:
        if candidate.is_symlink() or not candidate.is_file():
            candidate_status = "UNSAFE"
        else:
            candidate_status = "DRAFT"
    if review_present:
        if review.is_symlink() or not review.is_file():
            review_status = "UNSAFE"
        elif not candidate_present:
            review_status = "ORPHANED"
        elif candidate_status == "DRAFT" and lock_status == "ABSENT":
            try:
                validate_review(candidate, review)
            except (
                ContextctlError,
                GuardError,
                OSError,
                UnicodeError,
                IndexError,
                json.JSONDecodeError,
            ):
                review_status = "INCOMPLETE"
            else:
                candidate_status = "PREPARED"
                review_status = "PASS"
        else:
            review_status = "PRESENT"

    return {
        "candidate": candidate_status,
        "review": review_status,
        "lock": lock_status,
    }


def doctor_payload(task_root: Path) -> dict[str, object]:
    task_root = task_root.expanduser().resolve(strict=False)
    if not task_root.is_dir():
        raise ContextctlError(f"task root is not a directory: {task_root}")
    lock = diagnose_lock(task_root)
    candidate = task_root / "candidate.md"
    review = task_root / "review.json"
    checks: dict[str, object] = {
        "chain": {"status": "UNAVAILABLE", "checkpoints": 0},
        "latest": {"status": "UNAVAILABLE", "checkpoint": None, "seq": None},
        "evidence": {
            "status": "UNAVAILABLE",
            "ok": 0,
            "missing": 0,
            "external": 0,
            "unsafe": 0,
            "changed": 0,
            "unchecked": 0,
        },
        "source": {
            "status": "UNAVAILABLE",
            "path": None,
            "historical": False,
        },
        "candidate": {
            "status": "PRESENT"
            if candidate.exists() or candidate.is_symlink()
            else "ABSENT"
        },
        "review": {
            "status": "PRESENT"
            if review.exists() or review.is_symlink()
            else "ABSENT"
        },
        "lock": {key: value for key, value in lock.items() if key != "fingerprint"},
    }
    if lock["status"] == "ABSENT":
        chain = audited_continuity_chain(task_root)
        latest = chain[-1]
        resume = resume_payload(task_root)
        evidence = resume["evidence"]
        ok_count = sum(1 for record in evidence if record["status"] == "OK")
        missing_count = sum(1 for record in evidence if record["status"] == "MISSING")
        external_count = sum(1 for record in evidence if record["status"] == "EXTERNAL")
        unsafe_count = sum(1 for record in evidence if record["status"] == "UNSAFE")
        changed_count = sum(1 for record in evidence if record["status"] == "CHANGED")
        unchecked_count = sum(1 for record in evidence if record["status"] == "UNCHECKED")
        checks["chain"] = {"status": "OK", "checkpoints": len(chain)}
        checks["latest"] = {
            "status": "OK",
            "checkpoint": latest.path.name,
            "seq": latest.seq,
        }
        checks["evidence"] = {
            "status": "OK" if ok_count == len(evidence) else "WARNING",
            "ok": ok_count,
            "missing": missing_count,
            "external": external_count,
            "unsafe": unsafe_count,
            "changed": changed_count,
            "unchecked": unchecked_count,
        }
        checks["source"] = resume["source"]
        archived_review = (
            task_root
            / "reviews"
            / f"{latest.seq:04d}-{latest.meta['trigger']}.json"
        )
        if not review.is_file() and archived_review.is_file():
            checks["review"] = {"status": "ATTESTED"}
        readiness = str(resume["readiness"])
        source_status = str(resume["source"]["status"])
        if (
            missing_count
            or unsafe_count
            or changed_count
            or unchecked_count
            or (
                not bool(resume["source"].get("historical"))
                and source_status in {"MISSING", "CHANGED", "UNSAFE"}
            )
        ):
            next_step = (
                "create an evidence-repair draft, preserve unavailable provenance, "
                "then review and publish the replacement state"
            )
        elif external_count:
            next_step = (
                "resume is allowed; EXTERNAL is location-only and the archived "
                "review remains the semantic gate"
            )
        else:
            next_step = "continue from the Resume Card"
        candidate_present = candidate.exists() or candidate.is_symlink()
        review_present = review.exists() or review.is_symlink()
        if candidate_present:
            if candidate.is_symlink() or not candidate.is_file():
                checks["candidate"] = {"status": "UNSAFE"}
                readiness = "BLOCKED"
                next_step = "repair the unsafe candidate path; do not follow or delete it"
            elif not review_present:
                checks["candidate"] = {"status": "DRAFT"}
                checks["review"] = {"status": "ABSENT"}
                readiness = "DRAFTING"
                next_step = "finish the candidate, then run review-init"
            elif review.is_symlink() or not review.is_file():
                checks["candidate"] = {"status": "DRAFT"}
                checks["review"] = {"status": "UNSAFE"}
                readiness = "BLOCKED"
                next_step = "repair the unsafe review path; do not follow or delete it"
            else:
                try:
                    validate_review(candidate, review)
                except (
                    ContextctlError,
                    GuardError,
                    OSError,
                    UnicodeError,
                    IndexError,
                    json.JSONDecodeError,
                ) as exc:
                    checks["candidate"] = {"status": "DRAFT"}
                    checks["review"] = {
                        "status": "INCOMPLETE",
                        "reason": str(exc),
                    }
                    readiness = "REVIEWING"
                    next_step = "run review-check for the exact gap, then complete the independent review"
                else:
                    checks["candidate"] = {"status": "PREPARED"}
                    checks["review"] = {"status": "PASS"}
                    readiness = "REVIEWED"
                    next_step = "publish the reviewed candidate"
        elif review_present:
            checks["review"] = {"status": "ORPHANED"}
            readiness = "WARNING"
            next_step = "inspect the leftover working review; the published checkpoint remains authoritative"
    else:
        readiness = {
            "ACTIVE": "BUSY",
            "STALE": "STALE",
            "UNKNOWN": "UNKNOWN",
        }[str(lock["status"])]
        if lock["status"] == "STALE" and lock.get("lock_id"):
            next_step = (
                f"python3 {Path(__file__).resolve()} unlock {task_root} --stale "
                f"--lock-id {lock['lock_id']}"
            )
        else:
            next_step = "coordinate with the reported writer owner; do not delete the lock"
    return {
        "schema": DOCTOR_SCHEMA,
        "task_id": task_root.name,
        "readiness": readiness,
        "checks": checks,
        "next": next_step,
    }


def render_doctor(payload: dict[str, object]) -> str:
    checks = payload["checks"]
    assert isinstance(checks, dict)
    lines = [f"{payload['readiness']} · {payload['task_id']}"]
    for key in (
        "chain",
        "latest",
        "source",
        "evidence",
        "candidate",
        "review",
        "lock",
    ):
        value = checks[key]
        assert isinstance(value, dict)
        detail = ""
        if key == "chain" and "checkpoints" in value:
            detail = f" · checkpoints={value['checkpoints']}"
        elif key == "latest" and value.get("checkpoint"):
            detail = f" · {value['checkpoint']}"
        elif key == "evidence" and "ok" in value:
            detail = (
                f" · ok={value['ok']} missing={value['missing']} "
                f"external={value['external']} unsafe={value['unsafe']} "
                f"changed={value['changed']}"
            )
        elif key == "source" and value.get("path"):
            detail = f" · {value['path']}"
        elif key == "lock" and value.get("pid"):
            detail = f" · pid={value['pid']}"
        lines.append(f"{key.upper()}: {value['status']}{detail}")
    lines.append(f"NEXT: {payload['next']}")
    return "\n".join(lines) + "\n"


def audit_chain_with_lock(task_root: Path, expected_fingerprint: tuple[int, int, str]) -> None:
    checkpoints = scan_checkpoints(task_root)
    if not checkpoints:
        raise ContextctlError("cannot unlock a task without a valid checkpoint chain")
    initial_hashes = {path: sha256_file(path) for path in checkpoints}
    previous: Path | None = None
    for current in checkpoints:
        snapshot = Snapshot.load(current)
        if snapshot.meta["task_id"] != task_root.name:
            raise ContextctlError("checkpoint task_id does not match the task directory")
        if current.name != f"{snapshot.seq:04d}-{snapshot.meta['trigger']}.md":
            raise ContextctlError("checkpoint filename does not match metadata")
        check(current, previous, quiet=True)
        previous = current
    if scan_checkpoints(task_root) != checkpoints or any(
        sha256_file(path) != digest for path, digest in initial_hashes.items()
    ):
        raise ContextctlError("checkpoint chain changed during lock diagnosis")
    if lock_fingerprint(task_root / ".checkpoint-write.lock") != expected_fingerprint:
        raise ContextctlError("writer lock changed during diagnosis")


def unlock_stale(task_root: Path, lock_id: str | None = None) -> None:
    task_root = task_root.expanduser().resolve(strict=False)
    lock = task_root / ".checkpoint-write.lock"
    diagnosis = diagnose_lock(task_root)
    if diagnosis.get("status") != "STALE":
        raise ContextctlError(
            f"writer lock is {diagnosis.get('status')}, not a proven local stale lock"
        )
    expected_lock_id = diagnosis.get("lock_id")
    if expected_lock_id and lock_id != expected_lock_id:
        raise ContextctlError("unlock requires the exact lock ID reported by doctor")
    expected_fingerprint = diagnosis.get("fingerprint")
    if not isinstance(expected_fingerprint, tuple):
        raise ContextctlError("writer lock fingerprint is unavailable")
    candidate_path = task_root / "candidate.md"
    expected_candidate_hash = diagnosis.get("candidate_sha256")
    if candidate_path.exists():
        if candidate_path.is_symlink() or not candidate_path.is_file():
            raise ContextctlError("candidate is not a safe regular file")
        if not expected_candidate_hash or sha256_file(candidate_path) != expected_candidate_hash:
            raise ContextctlError("candidate changed or is not bound to the stale lock")
    if list(task_root.glob(".publishing-*")) or list((task_root / "checkpoints").glob(".publishing-*")):
        raise ContextctlError("partial publication artifacts exist")
    audit_chain_with_lock(task_root, expected_fingerprint)
    final = diagnose_lock(task_root)
    if final.get("status") != "STALE" or final.get("fingerprint") != expected_fingerprint:
        raise ContextctlError("writer lock changed before unlock")
    owner = lock / "owner.json"
    owner.unlink()
    lock.rmdir()


def list_tasks_payload(workspace_root: Path) -> dict[str, object]:
    workspace_root = workspace_root.expanduser().resolve(strict=False)
    continuity = workspace_root / ".continuity"
    tasks: list[dict[str, object]] = []
    if continuity.is_dir() and not continuity.is_symlink():
        for task_root in sorted(continuity.iterdir(), key=lambda path: path.name):
            if task_root.is_symlink() or not task_root.is_dir():
                continue
            try:
                resume = resume_payload(task_root)
                tasks.append(
                    {
                        "task_id": resume["task_id"],
                        "status": resume["status"],
                        "seq": resume["seq"],
                        "next_action": resume["next_action"],
                        "readiness": resume["readiness"],
                    }
                )
            except (ContextctlError, GuardError, OSError, UnicodeError, IndexError) as exc:
                tasks.append(
                    {
                        "task_id": task_root.name,
                        "status": "unknown",
                        "seq": None,
                        "next_action": "none",
                        "readiness": "BROKEN",
                        "error": str(exc),
                    }
                )
    return {
        "schema": TASK_LIST_SCHEMA,
        "workspace_root": str(workspace_root),
        "tasks": tasks,
    }


def render_task_list(payload: dict[str, object]) -> str:
    lines = [f"TASKS · {payload['workspace_root']}"]
    for task in payload["tasks"]:
        detail = f" · {task['error']}" if task.get("error") else ""
        lines.append(
            f"{task['task_id']} · {task['status']} · #{task['seq']} · "
            f"{task['readiness']} · {task['next_action']}{detail}"
        )
    return "\n".join(lines) + "\n"


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(description=__doc__)
    command_parser.add_argument("--version", action="version", version=VERSION)
    commands = command_parser.add_subparsers(dest="command", required=True)
    resume = commands.add_parser(
        "resume", help="audit and render the latest bounded Resume Card"
    )
    resume.add_argument("task_root", type=Path)
    resume_view = resume.add_mutually_exclusive_group()
    resume_view.add_argument("--json", action="store_true")
    resume_view.add_argument(
        "--full",
        action="store_true",
        help="render the complete cold-start contract, evidence, and risk view",
    )
    draft = commands.add_parser(
        "draft",
        help="create one exclusive candidate checkpoint",
        description=(
            "Create one exclusive candidate after validating inputs. A new task "
            "must use trigger init; an existing task must use a non-init trigger. The task ID "
            "starts with a lowercase alphanumeric and then uses only lowercase "
            "alphanumerics, dots, underscores, or hyphens. A trigger starts with "
            "a lowercase alphanumeric and then uses only lowercase alphanumerics "
            "or hyphens. A new task ID and every trigger are at most 200 UTF-8 "
            "bytes. After completion, only postcommit-receipt directly after "
            "completion or evidence-repair may follow, and the task remains complete."
        ),
    )
    draft.add_argument(
        "task_root",
        type=Path,
        help="task directory whose final path component is the task ID",
    )
    draft.add_argument(
        "--trigger",
        required=True,
        help="lowercase trigger; use complete command for the completion transition",
    )
    complete = commands.add_parser("complete", help="create a validated terminal candidate")
    complete.add_argument("task_root", type=Path)
    review_init = commands.add_parser("review-init", help="bind a fresh review manifest")
    review_init.add_argument("candidate", type=Path)
    review_init.add_argument("--output", type=Path, required=True)
    source_group = review_init.add_mutually_exclusive_group()
    source_group.add_argument("--source", type=Path)
    source_group.add_argument("--no-source-reason")
    review_init.add_argument("--replace-existing", action="store_true")
    review_check = commands.add_parser("review-check", help="validate the completed review")
    review_check.add_argument("candidate", type=Path)
    review_check.add_argument("manifest", type=Path)
    publish = commands.add_parser("publish", help="atomically publish a reviewed candidate")
    publish.add_argument("candidate", type=Path)
    publish.add_argument("--review", type=Path, required=True)
    doctor = commands.add_parser("doctor", help="diagnose chain and working-state recovery")
    doctor.add_argument("task_root", type=Path)
    doctor.add_argument("--json", action="store_true")
    unlock = commands.add_parser("unlock", help="remove only a proven stale writer lock")
    unlock.add_argument("task_root", type=Path)
    unlock.add_argument("--stale", action="store_true", required=True)
    unlock.add_argument("--lock-id")
    list_tasks = commands.add_parser("list-tasks", help="list continuity tasks by checkpoint state")
    list_tasks.add_argument("workspace_root", type=Path)
    list_tasks.add_argument("--json", action="store_true")
    return command_parser


def error_code(command: str, message: str) -> tuple[str, str]:
    lowered = message.casefold()
    if "review belongs to a different canonical task root" in lowered:
        return (
            "CTX304",
            "this chain was copied or moved; keep it read-only and initialize a new task ID in the current workspace, or resume it at the bound canonical root",
        )
    if "rollback incomplete" in message.casefold():
        return "CTX501", "run doctor and inspect task artifacts before any retry"
    if command == "unlock" or "lock" in message.casefold():
        return "CTX401", "run doctor and repair the reported lock condition; never force-delete it"
    if "source_review" in message.casefold() or "source coverage" in message.casefold():
        return (
            "CTX303",
            "have a fresh reviewer reopen the bound source, repair omissions, and complete source_review",
        )
    if "requires --source or --no-source-reason" in message.casefold():
        return (
            "CTX302",
            "rerun review-init with exactly one provenance mode: --source <path> or --no-source-reason <reason>",
        )
    if "source must not be the candidate or a same-file alias" in message.casefold():
        return (
            "CTX302",
            "rerun review-init with a distinct stable source, or use --no-source-reason when no prior artifact exists",
        )
    if command == "complete" and "already complete" in message.casefold():
        return "CTX103", "resume the completed task; do not create another completion checkpoint"
    if command == "complete" and "complete verification must cite a defined" in lowered:
        return (
            "CTX103",
            "publish a phase-verified checkpoint whose Verification cites only defined final E IDs, then rerun complete",
        )
    if command == "draft" and "without checkpoints must use trigger init" in lowered:
        return (
            "CTX202",
            "rerun draft for this new task with --trigger init; no task directory or candidate was created",
        )
    if command == "draft" and "cannot create a second init checkpoint" in lowered:
        return (
            "CTX202",
            "rerun draft for this existing task with the actual non-init durable-state trigger; no candidate was created",
        )
    if command == "draft" and (
        "transition to complete" in lowered
        or "trigger completion requires status complete" in lowered
    ):
        return (
            "CTX203",
            "run contextctl complete <task-dir> for the terminal transition; ordinary draft cannot create completion",
        )
    if command == "draft" and "a completed task accepts only" in lowered:
        return (
            "CTX203",
            "for evidence maintenance rerun draft <task-dir> --trigger evidence-repair; for new work run draft <new-task-dir> --trigger init",
        )
    if command == "draft" and "postcommit-receipt must directly follow completion" in lowered:
        return (
            "CTX203",
            "the postcommit receipt slot is unavailable; use draft <task-dir> --trigger evidence-repair for evidence maintenance or draft <new-task-dir> --trigger init for new work",
        )
    if command == "draft" and "postcommit-receipt requires a completed parent" in lowered:
        return (
            "CTX203",
            "continue the active task with its actual nonterminal trigger, or run contextctl complete <task-dir> when acceptance is proved; do not create a receipt yet",
        )
    if command == "draft" and (
        "task_id must" in lowered or "trigger must" in lowered
    ):
        return (
            "CTX202",
            "use the documented lowercase task ID and trigger character sets; no draft artifact was created",
        )
    if command in {"review-init", "review-check", "publish"} and "source" in message.casefold():
        return (
            "CTX302",
            "compress the initial candidate and rerun review-init --source with the same stable source artifact",
        )
    if command in {"review-init", "review-check", "publish"} and "evidence" in message.casefold():
        return (
            "CTX301",
            "repair the candidate claim or its missing or unsafe pointer, run review-init, then have a fresh reviewer verify each detected status",
        )
    if command in {"review-init", "review-check", "publish"} or "review" in message.casefold():
        return "CTX301", "repair the candidate, run review-init, and obtain a fresh review"
    if command == "draft":
        return "CTX201", "inspect or finish the existing candidate before creating another draft"
    return "CTX103", "repair the reported checkpoint or path, then rerun the same command"


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "resume":
            payload = resume_payload(args.task_root)
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
            elif args.full:
                print(render_full_card(payload), end="")
            else:
                print(render_card(payload), end="")
        elif args.command == "draft":
            candidate = draft_candidate(args.task_root, args.trigger)
            if (
                args.trigger != "init"
                and Snapshot.load(candidate, allow_auto=True).meta["status"] == "complete"
            ):
                print(
                    f"DRAFT COMPLETE-MAINTENANCE {candidate} · edit durable facts, "
                    "evidence, and Delta; keep status complete and terminal Next Action unchanged"
                )
            else:
                print(f"DRAFT {candidate} · edit durable facts, Delta, and next action")
        elif args.command == "complete":
            candidate = complete_candidate(args.task_root)
            print(
                f"DRAFT COMPLETE {candidate} · verify final evidence, then review-init and publish"
            )
        elif args.command == "review-init":
            manifest = initialize_review(
                args.candidate,
                args.output,
                args.source,
                args.no_source_reason,
                args.replace_existing,
            )
            evidence = ",".join(
                f"{record['id']}:{record['detected_status']}"
                for record in manifest["evidence_health"]
            )
            source = manifest["initial_source"]
            source_summary = "none"
            if isinstance(source, dict) and source.get("available") is True:
                compression = source["compression_basis_points"]
                source_summary = (
                    f"words={compression['words']}bp,bytes={compression['utf8_bytes']}bp"
                    if source.get("gate_required") is True
                    else "not-required"
                )
            elif isinstance(source, dict):
                source_summary = "explicitly-unavailable"
            print(
                f"REVIEW INCOMPLETE · candidate={manifest['candidate_sha256']} "
                f"missing={len(manifest['missing_ids'])} · evidence={evidence} "
                f"· source-compression={source_summary} · manifest={args.output}"
            )
        elif args.command == "review-check":
            candidate, _, _ = validate_review(args.candidate, args.manifest)
            print(f"PASS review seq={candidate.seq} ids={len(candidate.state_ids)}")
        elif args.command == "publish":
            published = publish_reviewed(args.candidate, args.review)
            print(f"PUBLISHED {published.path} · chain audited · review bound")
        elif args.command == "doctor":
            payload = doctor_payload(args.task_root)
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
            else:
                print(render_doctor(payload), end="")
        elif args.command == "unlock":
            unlock_stale(args.task_root, args.lock_id)
            print(f"UNLOCKED {args.task_root} · stale writer lock removed safely")
        else:
            payload = list_tasks_payload(args.workspace_root)
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
            else:
                print(render_task_list(payload), end="")
        return 0
    except (
        ContextctlError,
        GuardError,
        OSError,
        UnicodeError,
        IndexError,
        json.JSONDecodeError,
    ) as exc:
        code, next_step = error_code(args.command, str(exc))
        print(f"FAIL {code} {args.command.upper()}_FAILED: {exc}", file=sys.stderr)
        if "rollback incomplete" in str(exc).casefold():
            print(
                "SAFETY: UNKNOWN/PARTIAL · inspect doctor and task artifacts before retry",
                file=sys.stderr,
            )
        else:
            print("SAFETY: UNCHANGED · no destructive repair was attempted", file=sys.stderr)
        print(f"NEXT: {next_step}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
