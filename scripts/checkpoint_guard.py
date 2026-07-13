#!/usr/bin/env python3
"""Validate and atomically publish context-continuity checkpoints."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


SCHEMA = "context-continuity/v1"
LOCK_SCHEMA = "context-continuity/lock/v1"
MAX_CHECKPOINT_BYTES = 128 * 1024
STATUSES = {"active", "waiting", "blocked", "verifying", "complete"}
META_KEYS = {
    "schema",
    "task_id",
    "seq",
    "previous",
    "previous_sha256",
    "contract_version",
    "contract_sha256",
    "status",
    "trigger",
    "created_at",
    "workspace_revision",
}
OPTIONAL_META_KEYS = {"review_required"}
HEADINGS = (
    "## Contract",
    "## Current State",
    "## Active Decisions",
    "## Next Action",
    "## Verification for Next Action",
    "## Evidence Pointers",
    "## Blockers and Open Questions",
    "## Failed Attempts Not To Repeat",
    "## Assumptions and Unknowns",
    "## Delta From Previous",
    "## Retention Audit",
)
ID_RE = re.compile(r"\b[GIOSCDWEBFA]\d{3}\b")
DELTA_LABELS = (
    "Added",
    "Changed",
    "Superseded",
    "Resolved or cancelled",
    "Compressed or dropped",
    "Authorized by",
    "Growth reason",
)
ITEM_SECTIONS = {
    "## Contract": "GIOSC",
    "## Current State": "W",
    "## Active Decisions": "D",
    "## Evidence Pointers": "E",
    "## Blockers and Open Questions": "B",
    "## Failed Attempts Not To Repeat": "F",
    "## Assumptions and Unknowns": "A",
}
TASK_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
TRIGGER_RE = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
COMPLETE_MAINTENANCE_TRIGGERS = {"postcommit-receipt", "evidence-repair"}
MAX_IDENTIFIER_BYTES = 200


class GuardError(Exception):
    pass


def validate_task_id(value: str, *, enforce_length: bool = False) -> None:
    if enforce_length and len(value.encode("utf-8")) > MAX_IDENTIFIER_BYTES:
        raise GuardError(
            f"task_id must be at most {MAX_IDENTIFIER_BYTES} UTF-8 bytes for a new task"
        )
    if not TASK_ID_RE.fullmatch(value):
        raise GuardError(
            "task_id must start with a lowercase alphanumeric and contain only "
            "lowercase alphanumerics, dots, underscores, or hyphens"
        )


def validate_trigger(value: str, *, enforce_length: bool = False) -> None:
    if enforce_length and len(value.encode("utf-8")) > MAX_IDENTIFIER_BYTES:
        raise GuardError(
            f"trigger must be at most {MAX_IDENTIFIER_BYTES} UTF-8 bytes"
        )
    if not TRIGGER_RE.fullmatch(value):
        raise GuardError(
            "trigger must start with a lowercase alphanumeric and contain only "
            "lowercase alphanumerics or hyphens"
        )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def split_document(text: str) -> tuple[list[str], str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise GuardError("missing opening frontmatter delimiter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise GuardError("missing closing frontmatter delimiter") from exc
    return lines[1:end], "\n".join(lines[end + 1 :]).strip() + "\n"


def parse_meta(lines: list[str]) -> dict[str, str]:
    meta: dict[str, str] = {}
    for line in lines:
        if not line.strip():
            continue
        if ":" not in line:
            raise GuardError(f"invalid frontmatter line: {line}")
        key, value = (part.strip() for part in line.split(":", 1))
        if key in meta:
            raise GuardError(f"duplicate frontmatter key: {key}")
        meta[key] = value
    missing = META_KEYS - meta.keys()
    if missing:
        raise GuardError(f"missing frontmatter keys: {', '.join(sorted(missing))}")
    unexpected = meta.keys() - META_KEYS - OPTIONAL_META_KEYS
    if unexpected:
        raise GuardError(
            f"unexpected frontmatter keys: {', '.join(sorted(unexpected))}"
        )
    return meta


def section(body: str, heading: str) -> str:
    lines = body.splitlines()
    positions = [index for index, line in enumerate(lines) if line == heading]
    if len(positions) != 1:
        raise GuardError(f"expected exactly one {heading}")
    start = positions[0] + 1
    end = next(
        (index for index in range(start, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    value = "\n".join(lines[start:end]).strip()
    if not value:
        raise GuardError(f"empty section: {heading}")
    return value


def labeled_value(delta: str, label: str) -> str:
    match = re.search(rf"^- {re.escape(label)}:\s*(.+)$", delta, re.MULTILINE | re.IGNORECASE)
    if not match:
        raise GuardError(f"Delta From Previous is missing '{label}:'")
    return match.group(1).strip()


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def size_metrics(text: str) -> dict[str, int]:
    return {
        "words": word_count(text),
        "bytes": len(text.encode("utf-8")),
        "characters": len(text),
        "lines": len(text.splitlines()),
    }


def defined_state_items(body: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for heading, prefixes in ITEM_SECTIONS.items():
        pattern = re.compile(rf"^- (?:[^:\n]+:\s*)?([{prefixes}]\d{{3}})\b", re.MULTILINE)
        for line in section(body, heading).splitlines():
            match = pattern.match(line)
            if not match:
                continue
            item_id = match.group(1)
            if item_id in found:
                raise GuardError(f"duplicate protected item: {item_id}")
            found[item_id] = re.sub(r"\s+", " ", line).strip()
    return found


def transition_successor(value: str, item_id: str) -> str | None:
    match = re.search(rf"\b{item_id}\s*(?:->|→)\s*([GIOSCDWEBFA]\d{{3}})\b", value)
    return match.group(1) if match else None


@dataclass
class Snapshot:
    path: Path
    text: str
    meta: dict[str, str]
    body: str
    contract: str
    delta: str
    state_items: dict[str, str]

    @property
    def state_ids(self) -> set[str]:
        return set(self.state_items)

    @property
    def seq(self) -> int:
        return int(self.meta["seq"])

    @property
    def contract_version(self) -> int:
        return int(self.meta["contract_version"])

    @property
    def contract_hash(self) -> str:
        return sha256_bytes((self.contract + "\n").encode())

    @classmethod
    def from_text(
        cls, path: Path, text: str, allow_auto: bool = False
    ) -> "Snapshot":
        frontmatter, body = split_document(text)
        meta = parse_meta(frontmatter)
        for heading in HEADINGS:
            section(body, heading)
        contract = section(body, "## Contract")
        delta = section(body, "## Delta From Previous")
        snapshot = cls(path, text, meta, body, contract, delta, defined_state_items(body))
        snapshot.validate_shape(allow_auto)
        return snapshot

    @classmethod
    def load(cls, path: Path, allow_auto: bool = False) -> "Snapshot":
        return cls.from_text(path, path.read_text(encoding="utf-8"), allow_auto)

    def validate_shape(self, allow_auto: bool) -> None:
        if self.meta["schema"] != SCHEMA:
            raise GuardError(f"schema must be {SCHEMA}")
        validate_task_id(self.meta["task_id"])
        if not self.meta["seq"].isdigit() or self.seq < 1:
            raise GuardError("seq must be a positive integer")
        if not self.meta["contract_version"].isdigit() or self.contract_version < 1:
            raise GuardError("contract_version must be a positive integer")
        if self.meta["status"] not in STATUSES:
            raise GuardError(f"invalid status: {self.meta['status']}")
        validate_trigger(self.meta["trigger"])
        if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", self.meta["created_at"]):
            raise GuardError("created_at must be an ISO-8601 timestamp")
        if not self.meta["workspace_revision"]:
            raise GuardError("workspace_revision must not be empty")
        if re.search(r"<[^>]+>|\bTBD\b|\bTODO\b", self.body, re.IGNORECASE):
            raise GuardError("replace every template placeholder before validation")
        if len(self.text.encode("utf-8")) > MAX_CHECKPOINT_BYTES:
            raise GuardError("checkpoint exceeds the 128 KiB recovery-capsule limit")

        for prefix in "GIOSC":
            if not re.search(rf"\b{prefix}\d{{3}}\b", self.contract):
                raise GuardError(f"Contract is missing a {prefix}### item")
        if not re.search(r"\bE\d{3}\b", section(self.body, "## Evidence Pointers")):
            raise GuardError("Evidence Pointers must contain at least one E### item")

        for heading in ("## Next Action", "## Verification for Next Action"):
            bullets = re.findall(r"^- (?!\[[ xX]\])\S.*$", section(self.body, heading), re.MULTILINE)
            if len(bullets) != 1:
                raise GuardError(f"{heading} must contain exactly one top-level bullet")

        audit = section(self.body, "## Retention Audit")
        if "- [ ]" in audit or len(re.findall(r"^- \[[xX]\] ", audit, re.MULTILINE)) < 5:
            raise GuardError("Retention Audit must contain at least five checked items and no unchecked item")
        for label in DELTA_LABELS:
            labeled_value(self.delta, label)

        evidence_ids = {item_id for item_id in self.state_ids if item_id.startswith("E")}
        for line in section(self.body, "## Current State").splitlines():
            if line.startswith("- Verified:"):
                cited = set(re.findall(r"\bE\d{3}\b", line))
                if not cited or not cited <= evidence_ids:
                    raise GuardError("every Verified item must cite a defined E### evidence item")

        next_action = section(self.body, "## Next Action")
        if self.meta["status"] in {"waiting", "blocked"}:
            blocker_ids = {item_id for item_id in self.state_ids if item_id.startswith("B")}
            cited = set(re.findall(r"\bB\d{3}\b", next_action))
            if len(cited) != 1 or not cited <= blocker_ids:
                raise GuardError("waiting or blocked Next Action must cite exactly one defined B###")
        if self.meta["status"] == "complete":
            if next_action.strip().casefold() != "- none — task complete.":
                raise GuardError("complete checkpoint must use 'None — task complete.'")
            if any(item_id.startswith("W") for item_id in self.state_ids):
                raise GuardError("complete checkpoint cannot retain open W### work")
            verification = section(self.body, "## Verification for Next Action")
            if not set(re.findall(r"\bE\d{3}\b", verification)) <= evidence_ids or not re.search(
                r"\bE\d{3}\b", verification
            ):
                raise GuardError("complete verification must cite a defined E###")

        contract_hash = self.meta["contract_sha256"]
        if contract_hash != self.contract_hash and not (allow_auto and contract_hash == "auto"):
            raise GuardError("contract_sha256 does not match the Contract section")
        if self.meta["previous_sha256"] == "auto" and not allow_auto:
            raise GuardError("published or checked checkpoint cannot contain an auto hash")


def validate_pair(candidate: Snapshot, previous: Snapshot | None, allow_auto: bool) -> None:
    if candidate.path.parent.name != "checkpoints":
        validate_trigger(candidate.meta["trigger"], enforce_length=True)
        if previous is None:
            validate_task_id(candidate.meta["task_id"], enforce_length=True)
    if previous is None:
        if candidate.seq != 1 or candidate.meta["previous"] != "none":
            raise GuardError("initial checkpoint must use seq 1 and previous: none")
        if candidate.meta["trigger"] != "init" or candidate.contract_version != 1:
            raise GuardError("initial checkpoint must use trigger init and contract_version 1")
        if candidate.meta["previous_sha256"] not in ({"auto", "none"} if allow_auto else {"none"}):
            raise GuardError("initial checkpoint must use previous_sha256: none")
        return

    if candidate.meta["task_id"] != previous.meta["task_id"]:
        raise GuardError("task_id differs from the previous checkpoint")
    if candidate.seq != previous.seq + 1:
        raise GuardError(f"seq must be {previous.seq + 1}")
    if candidate.meta["previous"] != previous.path.name:
        raise GuardError(f"previous must be {previous.path.name}")
    expected_previous_hash = sha256_file(previous.path)
    actual_previous_hash = candidate.meta["previous_sha256"]
    if actual_previous_hash != expected_previous_hash and not (allow_auto and actual_previous_hash == "auto"):
        raise GuardError("previous_sha256 does not match the previous checkpoint")

    previous_complete = previous.meta["status"] == "complete"
    candidate_complete = candidate.meta["status"] == "complete"
    trigger = candidate.meta["trigger"]
    if previous_complete:
        if trigger not in COMPLETE_MAINTENANCE_TRIGGERS:
            raise GuardError(
                "a completed task accepts only postcommit-receipt or evidence-repair successors"
            )
        if not candidate_complete:
            raise GuardError("a completed task successor must remain complete")
        if trigger == "postcommit-receipt" and previous.meta["trigger"] != "completion":
            raise GuardError("postcommit-receipt must directly follow completion")
    else:
        if candidate_complete and trigger != "completion":
            raise GuardError("a transition to complete must use trigger completion")
        if trigger == "completion" and not candidate_complete:
            raise GuardError("trigger completion requires status complete; use contextctl complete")
        if trigger == "postcommit-receipt":
            raise GuardError("postcommit-receipt requires a completed parent")

    previous_contract = {
        item_id: text for item_id, text in previous.state_items.items() if item_id[0] in "GIOSC"
    }
    candidate_contract = {
        item_id: text for item_id, text in candidate.state_items.items() if item_id[0] in "GIOSC"
    }
    contract_changed = candidate.contract != previous.contract
    if contract_changed:
        if candidate.meta["trigger"] != "contract-change":
            raise GuardError("Contract changed without trigger: contract-change")
        if candidate.contract_version != previous.contract_version + 1:
            raise GuardError("contract-change must increment contract_version by one")
        authorization = labeled_value(candidate.delta, "Authorized by")
        authorization_ids = set(re.findall(r"\bE\d{3}\b", authorization))
        if not authorization_ids or not authorization_ids <= candidate.state_ids:
            raise GuardError("contract-change authorization must cite a defined E###")
        for item_id in previous_contract.keys() & candidate_contract.keys():
            if previous_contract[item_id] != candidate_contract[item_id]:
                raise GuardError(f"contract item {item_id} changed meaning; use a new ID")
        superseded = labeled_value(candidate.delta, "Superseded")
        for item_id in previous_contract.keys() - candidate_contract.keys():
            successor = transition_successor(superseded, item_id)
            if successor not in candidate_contract:
                raise GuardError(f"contract item {item_id} needs an explicit successor")
    else:
        if candidate.contract_version != previous.contract_version:
            raise GuardError("contract_version changed while Contract stayed the same")
        if candidate.meta["trigger"] == "contract-change":
            raise GuardError("contract-change trigger requires an actual Contract change")

    changed = labeled_value(candidate.delta, "Changed")
    previous_decisions = {
        item_id: text for item_id, text in previous.state_items.items() if item_id.startswith("D")
    }
    candidate_decisions = {
        item_id: text for item_id, text in candidate.state_items.items() if item_id.startswith("D")
    }
    for item_id in previous_decisions.keys() & candidate_decisions.keys():
        if previous_decisions[item_id] != candidate_decisions[item_id] and not re.search(
            rf"\b{item_id}\b", changed
        ):
            raise GuardError(f"decision {item_id} changed meaning without a Changed entry")

    superseded = labeled_value(candidate.delta, "Superseded")
    resolved = labeled_value(candidate.delta, "Resolved or cancelled")
    compressed = labeled_value(candidate.delta, "Compressed or dropped")
    for item_id in sorted(previous.state_ids - candidate.state_ids):
        successor = transition_successor(superseded, item_id) or transition_successor(
            compressed, item_id
        )
        explicitly_resolved = re.search(rf"\b{item_id}\b", resolved) and not re.search(
            rf"\b{item_id}\b[^\n]*(?:unresolved|not resolved|remains open)", resolved, re.IGNORECASE
        )
        if successor is not None and successor not in candidate.state_ids:
            raise GuardError(f"transition successor {successor} is not defined")
        if successor is None and not explicitly_resolved:
            raise GuardError(f"protected item {item_id} disappeared without an explicit transition")

    candidate_size = size_metrics(candidate.text)
    previous_size = size_metrics(previous.text)
    growth = {
        name: candidate_size[name] - previous_size[name]
        for name in candidate_size
        if candidate_size[name] > previous_size[name]
    }
    growth_reason = labeled_value(candidate.delta, "Growth reason").casefold().rstrip(".。")
    if growth and growth_reason in {"none", "n/a", "no growth"}:
        details = ", ".join(f"{name}=+{amount}" for name, amount in growth.items())
        raise GuardError(f"checkpoint grew without a growth reason: {details}")


def replace_meta(text: str, updates: dict[str, str]) -> str:
    frontmatter, body = split_document(text)
    replaced: set[str] = set()
    for index, line in enumerate(frontmatter):
        key = line.split(":", 1)[0].strip() if ":" in line else ""
        if key in updates:
            frontmatter[index] = f"{key}: {updates[key]}"
            replaced.add(key)
    if replaced != updates.keys():
        raise GuardError(f"cannot update missing keys: {', '.join(sorted(updates.keys() - replaced))}")
    return "---\n" + "\n".join(frontmatter) + "\n---\n\n" + body


def atomic_replace(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def process_start_token(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            text=True,
            capture_output=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = " ".join(result.stdout.split())
    return value or None


def lock_owner(candidate_path: Path) -> dict[str, object]:
    pid = os.getpid()
    return {
        "schema": LOCK_SCHEMA,
        "pid": pid,
        "hostname": socket.gethostname(),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "operation": "publish",
        "process_start": process_start_token(pid),
        "nonce": secrets.token_hex(16),
        "task_root": str(candidate_path.parent.resolve(strict=False)),
        "candidate_path": str(candidate_path.resolve(strict=False)),
        "candidate_sha256": sha256_file(candidate_path),
    }


def safe_checkpoint_directory(task_root: Path) -> Path:
    checkpoint_dir = task_root / "checkpoints"
    if checkpoint_dir.is_symlink():
        raise GuardError("checkpoint directory must not be a symlink")
    if not checkpoint_dir.exists():
        return checkpoint_dir
    if not checkpoint_dir.is_dir():
        raise GuardError("checkpoint path must be a real directory")
    canonical_root = task_root.resolve(strict=True)
    canonical_checkpoints = checkpoint_dir.resolve(strict=True)
    try:
        canonical_checkpoints.relative_to(canonical_root)
    except ValueError as exc:
        raise GuardError("checkpoint directory escapes the task root") from exc
    return checkpoint_dir


def scan_checkpoints(task_root: Path) -> list[Path]:
    checkpoint_dir = safe_checkpoint_directory(task_root)
    if not checkpoint_dir.exists():
        return []
    indexed: dict[int, Path] = {}
    for path in checkpoint_dir.glob("*.md"):
        if path.is_symlink() or not path.is_file():
            raise GuardError(f"checkpoint must be a safe regular file: {path.name}")
        match = re.fullmatch(r"(\d{4})-([a-z0-9][a-z0-9-]*)\.md", path.name)
        if not match:
            raise GuardError(f"invalid checkpoint filename: {path.name}")
        seq = int(match.group(1))
        if seq in indexed:
            raise GuardError(f"duplicate checkpoint sequence: {seq}")
        indexed[seq] = path
    expected = list(range(1, len(indexed) + 1))
    if sorted(indexed) != expected:
        raise GuardError("checkpoint sequence is not continuous")
    return [indexed[seq] for seq in expected]


def prepare(candidate_path: Path, previous_path: Path | None) -> None:
    candidate = Snapshot.load(candidate_path, allow_auto=True)
    if candidate.meta["task_id"] != candidate_path.parent.name:
        raise GuardError("task_id must match the candidate's parent directory")
    checkpoints = scan_checkpoints(candidate_path.parent)
    expected_previous = checkpoints[-1] if checkpoints else None
    if (previous_path.resolve() if previous_path else None) != (
        expected_previous.resolve() if expected_previous else None
    ):
        raise GuardError("--previous must name the current latest checkpoint")
    previous = Snapshot.load(previous_path) if previous_path else None
    validate_pair(candidate, previous, allow_auto=True)
    finalized = replace_meta(
        candidate.text,
        {
            "previous_sha256": sha256_file(previous_path) if previous_path else "none",
            "contract_sha256": candidate.contract_hash,
        },
    )
    atomic_replace(candidate_path, finalized)
    strict_candidate = Snapshot.load(candidate_path)
    validate_pair(strict_candidate, previous, allow_auto=False)
    print(f"PASS prepared seq={strict_candidate.seq} candidate={candidate_path}")


def check(candidate_path: Path, previous_path: Path | None, quiet: bool = False) -> None:
    candidate = Snapshot.load(candidate_path)
    previous = Snapshot.load(previous_path) if previous_path else None
    validate_pair(candidate, previous, allow_auto=False)
    if not quiet:
        candidate_size = size_metrics(candidate.text)
        previous_size = size_metrics(previous.text) if previous else {name: 0 for name in candidate_size}
        metrics = " ".join(
            f"{name}={value} delta_{name}={value - previous_size[name]:+d}"
            for name, value in candidate_size.items()
        )
        print(f"PASS checked seq={candidate.seq} {metrics} ids={len(candidate.state_ids)}")


def audited_chain(task_root: Path) -> list[Snapshot]:
    lock = task_root / ".checkpoint-write.lock"
    if lock.exists() or lock.is_symlink():
        raise GuardError(f"checkpoint writer lock exists: {lock}")
    checkpoints = scan_checkpoints(task_root)
    if not checkpoints:
        raise GuardError("no published checkpoints")
    initial_hashes = {path: sha256_file(path) for path in checkpoints}
    snapshots: list[Snapshot] = []
    previous: Path | None = None
    for current in checkpoints:
        snapshot = Snapshot.load(current)
        if snapshot.meta["task_id"] != task_root.name:
            raise GuardError(f"task_id does not match directory: {current.name}")
        expected_name = f"{snapshot.seq:04d}-{snapshot.meta['trigger']}.md"
        if current.name != expected_name:
            raise GuardError(f"filename does not match checkpoint metadata: {current.name}")
        check(current, previous, quiet=True)
        snapshots.append(snapshot)
        previous = current
    if scan_checkpoints(task_root) != checkpoints or any(
        sha256_file(path) != digest for path, digest in initial_hashes.items()
    ):
        raise GuardError("checkpoint chain changed while it was being audited")
    if lock.exists() or lock.is_symlink():
        raise GuardError(f"checkpoint writer lock appeared during audit: {lock}")
    return snapshots


def audit(task_root: Path) -> None:
    snapshots = audited_chain(task_root)
    print(
        f"PASS chain checkpoints={len(snapshots)} "
        f"latest={snapshots[-1].path.name}"
    )


def latest(task_root: Path) -> None:
    snapshots = audited_chain(task_root)
    print(snapshots[-1].path)


def atomic_publish(target: Path, text: str) -> Path | None:
    if target.exists() or target.is_symlink():
        raise GuardError(f"target already exists: {target.name}")
    descriptor, temporary = tempfile.mkstemp(prefix=".publishing-", dir=target.parent)
    committed = False
    operation_error: Exception | None = None
    cleanup_artifact: Path | None = None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
        committed = True
    except Exception as exc:
        operation_error = exc
        raise
    finally:
        if os.path.lexists(temporary):
            try:
                os.unlink(temporary)
            except OSError as cleanup_error:
                if committed:
                    cleanup_artifact = Path(temporary)
                    print(
                        "WARNING target published but temporary link cleanup failed: "
                        f"{cleanup_error}",
                        file=sys.stderr,
                    )
                else:
                    raise GuardError(
                        "atomic publish rollback incomplete; state is UNKNOWN/PARTIAL: "
                        f"temporary file cleanup failed: {cleanup_error}"
                    ) from operation_error
    return cleanup_artifact


def promote(
    candidate_path: Path,
    expected_sha256: str | None = None,
    companions: list[tuple[Path, str]] | None = None,
    precommit_check: Callable[
        [Snapshot, Snapshot | None, list[Snapshot]], None
    ]
    | None = None,
) -> Path:
    task_root = candidate_path.parent
    checkpoint_dir = safe_checkpoint_directory(task_root)
    created_checkpoint_dir = False
    if not checkpoint_dir.exists():
        try:
            checkpoint_dir.mkdir()
            created_checkpoint_dir = True
        except FileExistsError:
            pass
        checkpoint_dir = safe_checkpoint_directory(task_root)
    lock = task_root / ".checkpoint-write.lock"
    try:
        lock.mkdir()
    except FileExistsError as exc:
        if created_checkpoint_dir:
            try:
                checkpoint_dir.rmdir()
            except OSError as rollback_error:
                raise GuardError(
                    "publication rollback incomplete; state is UNKNOWN: "
                    f"checkpoint directory cleanup failed: {rollback_error}"
                ) from exc
        raise GuardError(f"checkpoint writer lock exists: {lock}") from exc

    owner_path = lock / "owner.json"
    try:
        atomic_replace(
            owner_path,
            json.dumps(lock_owner(candidate_path), ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
        )
    except Exception as original_error:
        rollback_errors: list[str] = []
        try:
            lock.rmdir()
        except OSError as rollback_error:
            rollback_errors.append(str(rollback_error))
        if created_checkpoint_dir:
            try:
                checkpoint_dir.rmdir()
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise GuardError(
                "publication rollback incomplete; state is UNKNOWN: "
                + "; ".join(rollback_errors)
            ) from original_error
        raise

    published_target: Path | None = None
    published_companions: list[Path] = []
    temporary_links: list[Path] = []
    created_parents: list[Path] = []
    committed = False
    try:
        checkpoints = scan_checkpoints(task_root)
        checkpoint_hashes = {path: sha256_file(path) for path in checkpoints}
        existing_snapshots: list[Snapshot] = []
        previous: Path | None = None
        for current in checkpoints:
            snapshot = Snapshot.load(current)
            if snapshot.meta["task_id"] != task_root.name:
                raise GuardError(f"task_id does not match directory: {current.name}")
            expected_name = f"{snapshot.seq:04d}-{snapshot.meta['trigger']}.md"
            if current.name != expected_name:
                raise GuardError(
                    f"filename does not match checkpoint metadata: {current.name}"
                )
            check(current, previous, quiet=True)
            existing_snapshots.append(snapshot)
            previous = current
        if scan_checkpoints(task_root) != checkpoints or any(
            sha256_file(path) != digest for path, digest in checkpoint_hashes.items()
        ):
            raise GuardError("checkpoint chain changed during locked precommit")
        candidate = Snapshot.load(candidate_path)
        candidate_hash = sha256_bytes(candidate.text.encode("utf-8"))
        if expected_sha256 is not None and candidate_hash != expected_sha256:
            raise GuardError("candidate SHA-256 changed after review")
        if candidate.meta["task_id"] != task_root.name:
            raise GuardError("task_id must match the candidate's parent directory")
        previous_snapshot = existing_snapshots[-1] if existing_snapshots else None
        validate_pair(candidate, previous_snapshot, allow_auto=False)
        if precommit_check is not None:
            precommit_check(candidate, previous_snapshot, existing_snapshots)
        for companion_path, companion_text in companions or []:
            raw_companion_path = companion_path.expanduser()
            if raw_companion_path.is_symlink():
                raise GuardError("publication companion must not be a symlink")
            companion_path = Path(os.path.abspath(str(raw_companion_path)))
            try:
                relative = companion_path.relative_to(task_root.resolve(strict=False))
            except ValueError as exc:
                raise GuardError("publication companion escapes the task root") from exc
            current_parent = task_root.resolve(strict=False)
            for part in relative.parts[:-1]:
                current_parent = current_parent / part
                if current_parent.is_symlink():
                    raise GuardError("publication companion parent must not be a symlink")
                if current_parent.exists() and not current_parent.is_dir():
                    raise GuardError("publication companion parent is not a directory")
            if not companion_path.parent.exists():
                companion_path.parent.mkdir(parents=True)
                created_parents.append(companion_path.parent)
            temporary_link = atomic_publish(companion_path, companion_text)
            if temporary_link is not None:
                temporary_links.append(temporary_link)
            published_companions.append(companion_path)
            if sha256_file(companion_path) != sha256_bytes(
                companion_text.encode("utf-8")
            ):
                raise GuardError("published companion differs from reviewed bytes")
        target = checkpoint_dir / f"{candidate.seq:04d}-{candidate.meta['trigger']}.md"
        temporary_link = atomic_publish(target, candidate.text)
        if temporary_link is not None:
            temporary_links.append(temporary_link)
        published_target = target
        if sha256_file(target) != candidate_hash:
            raise GuardError("published checkpoint differs from the candidate")
        committed = True
        try:
            if sha256_file(candidate_path) == candidate_hash:
                candidate_path.unlink()
            else:
                print(
                    "WARNING candidate changed during publication and was preserved",
                    file=sys.stderr,
                )
        except OSError as exc:
            print(f"WARNING published but could not remove candidate: {exc}", file=sys.stderr)
        for temporary_link in temporary_links:
            try:
                temporary_link.unlink(missing_ok=True)
            except OSError as exc:
                print(
                    "WARNING published but temporary link remains: "
                    f"{temporary_link}: {exc}",
                    file=sys.stderr,
                )
        print(f"PASS promoted {target}")
        return target
    except Exception as original_error:
        if not committed:
            rollback_errors: list[str] = []
            if published_target is not None:
                try:
                    published_target.unlink(missing_ok=True)
                except OSError as exc:
                    rollback_errors.append(str(exc))
            for companion_path in reversed(published_companions):
                try:
                    companion_path.unlink(missing_ok=True)
                except OSError as exc:
                    rollback_errors.append(str(exc))
            for temporary_link in reversed(temporary_links):
                try:
                    temporary_link.unlink(missing_ok=True)
                except OSError as exc:
                    rollback_errors.append(str(exc))
            for parent in reversed(created_parents):
                try:
                    parent.rmdir()
                except OSError:
                    pass
            if created_checkpoint_dir:
                try:
                    checkpoint_dir.rmdir()
                except OSError as exc:
                    rollback_errors.append(str(exc))
            if rollback_errors:
                raise GuardError(
                    "publication rollback incomplete; state is UNKNOWN: "
                    + "; ".join(rollback_errors)
                ) from original_error
        raise
    finally:
        try:
            owner_path.unlink(missing_ok=True)
            lock.rmdir()
        except OSError as exc:
            print(f"WARNING could not release checkpoint writer lock: {exc}", file=sys.stderr)


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(description=__doc__)
    commands = command_parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "check"):
        sub = commands.add_parser(name)
        sub.add_argument("candidate", type=Path)
        sub.add_argument("--previous", type=Path)
    promote_parser = commands.add_parser("promote")
    promote_parser.add_argument("candidate", type=Path)
    promote_parser.add_argument("--expected-sha256")
    audit_parser = commands.add_parser("audit")
    audit_parser.add_argument("task_root", type=Path)
    latest_parser = commands.add_parser("latest")
    latest_parser.add_argument("task_root", type=Path)
    return command_parser


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "prepare":
            prepare(args.candidate, args.previous)
        elif args.command == "check":
            check(args.candidate, args.previous)
        elif args.command == "promote":
            promote(args.candidate, args.expected_sha256)
        elif args.command == "audit":
            audit(args.task_root)
        else:
            latest(args.task_root)
        return 0
    except (GuardError, OSError, UnicodeError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
