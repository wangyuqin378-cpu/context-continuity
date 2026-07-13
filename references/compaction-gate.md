# Compaction gate

Read this file only when comparing and publishing a new checkpoint.

The target is higher information density, not mechanical word-count reduction.
Build the candidate from verified deltas and current source artifacts, then audit
it against the latest checkpoint. Never summarize the full conversation.

For an initial checkpoint replacing a source larger than 500 words or 4 KiB,
the default gate is at least 30% smaller in both whitespace words and UTF-8 bytes.
During authorship, target at least 40% smaller in both metrics; the extra margin
absorbs finalized hashes/review metadata and reduces avoidable repair loops.
Pass that artifact through `review-init --source`; `review-check` revalidates its
path, SHA-256, counts, and compression before publication. If protected facts make
the gate impossible, name the exact P0 IDs in `Growth reason` and stop for an
explicit contract decision; do not silently waive it. For CJK-heavy content,
bytes and Unicode characters are authoritative because whitespace words undercount.
The guard reports words, UTF-8 bytes, Unicode characters, and lines. Growth in any
metric needs a durable-fact reason, and the whole checkpoint must stay at or below
128 KiB. If it reaches that ceiling, move completed detail to cited artifacts; a
recovery capsule is not an archive.

## Section-by-section comparison

| Section | Required treatment |
|---|---|
| Contract | Preserve exactly unless the user authorized a contract change. |
| Current State | Keep only current verified milestones and open outcomes; replace completed detail with evidence pointers. |
| Active Decisions | Keep active decisions and rationale. Move a superseded ID out only when the delta names its successor. |
| Next Action | Keep exactly one action and one observable verification check. |
| Evidence | Preserve source-provided E IDs and exact pointers verbatim. Do not repoint them to the chat or source log that mentioned them. Add only pointers needed by active claims. |
| Blockers / Unknowns | Never silently resolve or convert uncertainty into fact. |
| Failed Attempts | Keep failures whose repetition would waste time or cause harm; archive irrelevant detail with a reason. |
| Delta | Explain every addition, change, transition, compression, and any net growth. |

## Evidence review

Reachability and semantic support are separate gates:

| Field | Allowed result | Meaning |
|---|---|---|
| machine `detected_status` | `OK` | One safe local pointer resolves inside the task workspace and its content SHA-256 is bound; semantic support is not implied. |
| machine `detected_status` | `EXTERNAL` | An explicit URI or absolute outside-workspace location; retrieval or support is not implied. |
| machine `detected_status` | `MISSING` / `UNSAFE` | A relative file is absent, the pointer is ambiguous, or it escapes through path/symlink/shell syntax; publication is blocked. |
| reviewer `semantic_status` | `DIRECT` | The exact cited content was opened and directly supports the claim. |
| reviewer `semantic_status` | `REPORTED` | An accessible capture supports only the attributed statement that another source reported the claim. |
| reviewer `semantic_status` | `UNSUPPORTED` | Content was not inspected or does not support the claim; publication is blocked. |

The reviewer never edits `detected_status`; `review-check` recomputes it. Set the
review record's `status` to the matching `OK` or `EXTERNAL` only after inspection.
PASS requires no MISSING, UNSAFE, or UNSUPPORTED item. EXTERNAL alone never means
verified. For unavailable direct evidence, use the inactive-provenance fallback in
`checkpoint-format.md`; do not silently repoint the old E ID.

For an available initial source, the cold reviewer opens the bound source before
the candidate, recovers every machine-detected source ID, and records COMPLETE
only after checking source-only durable facts. Candidate-declared IDs alone are
never sufficient evidence of source coverage.

## Protected information

The following is P0 and cannot disappear without an explicit transition in
`Delta From Previous`:

- goal, scope, non-goals, success criteria, user corrections, and permissions;
- current phase, remaining work, and the one next action;
- active decisions and their rationale;
- blockers, pending approvals, and release conditions;
- verified claims and the evidence locations supporting them;
- source/workspace revision and failures that must not be repeated.

For a removed stable ID, use one of these explicit forms:

- `Superseded: D001 -> D002 — reason and authorization.`
- `Resolved or cancelled: W001 — result and E003.`
- `Compressed or dropped: E001 -> E004 — more stable source.`

Use arrow syntax when a successor exists (`OLD -> NEW`). `Changed` can acknowledge
new wording for an item that still exists, but it cannot remove an item. Contract
items never change meaning in place: create a new ID and cite authorization E###.

## Compression order

Compress in this order until the capsule is lean enough to cold-start:

1. raw tool output and logs into evidence pointers;
2. repeated narrative into one verified statement;
3. detailed completed steps into milestone plus proof;
4. stale background that does not affect a current decision;
5. superseded decisions into a successor reference in the delta.

For an initial snapshot, the source log itself is not evidence for every fact.
Prefer reachable direct evidence already named in the source. When it is
unavailable, a stable source capture may support only an explicitly attributed
report, never the underlying result. Reuse one capture E### for multiple attributed
reports when its scope is clear instead of duplicating equivalent capture pointers.
Keep the initial Delta to one short line per required label, and keep the Retention
Audit to its five tokens.

Never compress exact acceptance values, user constraints, permissions, paths,
API names, error signatures, unresolved conflicts, or the reason for an active
decision.

If the candidate is longer than its predecessor, `Growth reason` must identify
the new P0 facts. “More detail” is not a sufficient reason.

## Cold-start rehearsal

Before promotion, give a fresh sub-agent only the candidate when available. If
sub-agents are unavailable, reread the candidate without relying on chat history.
Use fixed recovery slots rather than asking for a free-form rewritten handoff, and
do not apply the checkpoint compression threshold to the rehearsal answer.
The reader must answer all of these without opening older checkpoints:

1. What exact outcome, scope, non-goals, and success evidence govern the task?
2. What is verified now, and which source proves it?
3. Which active decisions control the approach, and why?
4. What remains blocked, uncertain, or unsafe to repeat?
5. What is the one next action, and how will it be verified?

Any missing or contradictory answer blocks promotion. Repair the candidate,
rerun the structural guard, and repeat the rehearsal.

## Publish safety

- The candidate's `previous` must still be the latest checkpoint at promotion.
- The guard uses a single-writer lock and exclusive append. Do not bypass it.
- A conflict means another writer won. Rebase on the new latest checkpoint.
- Current-schema publication revalidates source/evidence bytes and Resume Card
  projection inside the writer lock. Sidecar and required marker are linked first;
  checkpoint is the commit point and is linked last.
- Any precommit failure rolls companions back and leaves the previous latest and
  candidate unchanged. Cleanup after the commit point may warn but must not report
  `SAFETY: UNCHANGED` or turn a committed publication into failure.
- Every current-schema checkpoint in the full history requires its exact archived
  sidecar and marker before resume, later drafting, review initialization, or
  locked publication; only checkpoints without the schema marker use legacy.
- Archived sidecar/marker integrity is frozen; current source/evidence reachability
  is health state. If a published pointer becomes MISSING or CHANGED, create an
  `evidence-repair` draft and follow the inactive-provenance transition. The new
  candidate must pass live source/evidence binding before it can publish.
- Completion is a monotonic state transition enforced by the shared pair guard.
  Only a complete postcommit receipt directly after completion, or a complete
  evidence-repair successor, may extend that chain. Reopened work belongs to a
  new task chain.
- Never remove a stale lock without first establishing that no writer is active.
