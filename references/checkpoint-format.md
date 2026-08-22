# Checkpoint format

Read this file only when initializing or refreshing a checkpoint.

## Paths and ordering

- Task root: `<stable-workspace-root>/.continuity/<task-id>/`
- Draft: `.continuity/<task-id>/candidate.md`
- Published snapshots: `.continuity/<task-id>/checkpoints/<seq>-<trigger>.md`
- Sequence starts at `0001` and must be continuous.
- A coordinator is the sole checkpoint writer. Parallel workers return deltas.
- A task ID starts with a lowercase alphanumeric and then uses only lowercase
  alphanumerics, `.`, `_`, or `-`. A trigger starts with a lowercase alphanumeric
  and then uses only lowercase alphanumerics or `-`. A new task ID and every new
  trigger are at most 200 UTF-8 bytes so checkpoint, review, and marker filenames
  remain publishable. The high-level draft command rejects invalid values before
  creating a directory or candidate.

Use the following exact H2 section names. Replace every placeholder before
validation. Keep one short bullet per durable fact; do not restate the same fact
across sections. Give durable facts stable IDs. Define each protected ID at the
start of exactly one canonical state bullet. In `Current State`, define the open
W### only on `Remaining`; `In progress` names the phase without defining the same
W### again. `Next Action` then cites that existing W###.

```markdown
---
schema: context-continuity/v1
task_id: example-task
seq: 1
previous: none
previous_sha256: auto
contract_version: 1
contract_sha256: auto
status: active
trigger: init
created_at: 2026-07-11T12:00:00+08:00
workspace_revision: not-available
---

# Example task checkpoint

## Contract

- G001 Goal: concrete outcome.
- I001 In scope: bounded surface.
- O001 Out of scope: excluded surface.
- S001 Success: measurable evidence.
- C001 Constraint: exact user boundary.

## Current State

- Verified: current fact (E001).
- In progress: current phase or `none`.
- Remaining: W001 — open outcome.

## Active Decisions

- D001 — decision. Why: rationale. Supersedes: none.

## Next Action

- W001 — one concrete action.

## Verification for Next Action

- Observable check and required result.

## Evidence Pointers

- E001 — `original-path#exact-location` — supports S001/W001.

## Blockers and Open Questions

- B001 — none, or blocker plus release condition.

## Failed Attempts Not To Repeat

- F001 — none, or signature, cause, and evidence.

## Assumptions and Unknowns

- A001 — assumption or unknown.

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
```

## Field rules

- `previous_sha256` and `contract_sha256` may be `auto` only in a candidate.
  The promotion guard writes real SHA-256 values into the published checkpoint.
- Allowed status values: `active`, `waiting`, `blocked`, `verifying`, `complete`.
- Use `trigger: contract-change` only when goal, scope, non-goals, success
  criteria, constraints, or permissions change. Increment `contract_version`,
  give every changed contract item a new ID, cite the authorizing user statement
  through a defined E###, and list every `OLD -> NEW` contract transition.
- For any other trigger, copy `## Contract` exactly and keep the same
  `contract_version`.
- `workspace_revision` is an observable source revision, content hash, build ID,
  or `not-available`; do not assume Git exists.
- On an initial checkpoint, use exactly one of `review-init --source` or
  `--no-source-reason`. Sources above 500 whitespace words or 4 KiB must shrink
  by at least 30% in both words and UTF-8 bytes. The manifest binds the canonical
  source path, SHA-256, detected IDs, counts, and compression. The fresh reviewer
  opens that bound source before the candidate, recovers all source IDs, and sets
  source coverage COMPLETE only when no source-only durable constraint, correction,
  permission, or acceptance fact is omitted. Later checkpoints compare against
  their published predecessor instead.
- Keep stable IDs for durable items: `G`, `I`, `O`, `S`, `C`, `D`, `W`, `E`,
  `B`, `F`, and `A` followed by three digits.
- An ID has one meaning. If an active decision's wording changes, name that ID in
  `Changed`. Contract meaning changes always require a new ID. Removed items must
  use `Superseded: OLD -> NEW`, `Resolved or cancelled: OLD`, or
  `Compressed or dropped: OLD -> NEW`; a generic `Changed` note is insufficient.
- Preserve an evidence ID and its direct pointer as one immutable pair; never
  silently assign that ID to another location. Each active E### bullet contains
  one backticked pointer.
- A missing relative pointer cannot be active evidence and is never EXTERNAL. If
  an initial source names direct evidence that is unavailable, keep the exact
  `E### — pointer` pair as plain text in an A### inactive-provenance note, create
  a new E### for an accessible source capture, and word every dependent claim as
  “the capture reports …”. The capture does not prove the underlying artifact or
  result. If no capture exists, keep the underlying claim unknown.
- If a published E### later becomes unavailable, its immutable historical
  checkpoint already preserves the original pair. Add any current capture under a
  new E###, downgrade dependent claims, and record `Compressed or dropped: OLD ->
  NEW — not equivalent evidence`. Copy the old pair into A### only when it still
  changes an active interpretation; do not carry an inactive provenance ledger in
  every successor. Never invent or copy a file merely to make reachability pass.
  Resume/doctor report MISSING or CHANGED; create the repair candidate with
  `contextctl draft <task-dir> --trigger evidence-repair`.
- For a local OK pointer, review binds canonical path and file SHA-256; any content
  edit after review-init invalidates review-check and publish. External content
  remains inside the explicitly stated reviewer/source trust boundary.
- Aim for an active checkpoint at or below 8 KiB and stop to compress at 12 KiB
  unless exact active contract or safety facts justify the growth. The complete
  checkpoint file must not exceed 128 KiB. Size checks cover words, UTF-8 bytes,
  Unicode characters, and lines so CJK text cannot bypass the budget.
- Prefer `contextctl complete <task-dir>` for the terminal draft. `complete` still
  requires `Next Action`; write exactly `None — task complete.` and
  point its verification section to the final acceptance evidence. The command
  allocates the terminal B### above every B### in the audited history, records
  transitions only for currently active blockers, and refuses B999 exhaustion
  before creating a candidate.
- Before `complete`, the latest published Verification may cite only final E###
  IDs already defined in Evidence Pointers. A planned postcommit receipt is not
  yet evidence. Correct this through a new phase-verified checkpoint rather than
  editing the published predecessor.
- A transition from a non-complete checkpoint to `complete` must use trigger
  `completion`; a normal draft cannot synthesize it. After completion, only one
  `postcommit-receipt` directly following `completion`, or `evidence-repair`, may
  follow. These maintenance successors must remain `complete`; new work uses a
  new task chain.
- `waiting` and `blocked` must cite exactly one current B### in `Next Action`.
  `complete` may not retain W### work and its verification must cite E###.
