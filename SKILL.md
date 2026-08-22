---
name: context-continuity
description: Preserve the minimum verified task state across compaction, interruptions, and handoffs. Activate only for durable multi-session work or expensive/high-risk recovery; skip short, cheap-to-restart tasks.
compatibility: Requires Python 3.9+ for structural validation and atomic publication.
---

# Context Continuity

Keep long-running work restartable without turning ordinary development into a
checkpoint ceremony. The target is the minimum verified state a fresh agent needs,
not a complete task ledger.

While a checkpoint is being published, publication is a hard gate: do not start
the next durable phase until it succeeds. Between durable transitions, keep doing
normal implementation and verification without creating another checkpoint.
Without host lifecycle hooks, interruption capture remains manual/best effort.

## Activation gate

Activate this skill only when the user explicitly requests durable checkpoints or
when restarting incorrectly would cost more than maintaining the capsule. Strong
signals are work spanning sessions or days, agent handoffs, exact scope or
permission boundaries, expensive failed attempts, and external high-impact
operations.

Do not activate it for a short one-session task, disposable exploration, or work
that is cheaper to restart. Do not make an `AGENTS.md` require a full continuity
review before every edit, test, or read-only inspection. A short `HANDOFF.md` or
ordinary task plan is enough for low-risk work.

## Resolve the installed CLI once

Resolve the directory containing this `SKILL.md` as `SKILL_DIR`.

- Preferred CLI: `$SKILL_DIR/scripts/contextctl.py`.
- If it is missing but
  `$SKILL_DIR/context-continuity/scripts/contextctl.py` exists, the installation
  is accidentally nested. That fallback may be used for read-only `--version`,
  `list-tasks`, `resume`, or `doctor`; reinstall the repository at the flat
  `SKILL_DIR` before drafting or publishing.
- Verify the selected copy with `python3 "$CONTEXTCTL" --version`.

Never guess among several writable copies.

## Resume with the smallest sufficient view

Routine resume audits the complete chain but prints a bounded daily card:

```bash
python3 "$CONTEXTCTL" resume <task-dir>
```

Use the complete cold-start view when a new agent/session lacks the contract,
after compaction or handoff, before changing scope/permissions, before an external
mutation or release decision, or whenever the brief card reports attention:

```bash
python3 "$CONTEXTCTL" resume <task-dir> --full
```

Use `--json` only for machine consumers. The published checkpoint remains the
source of truth; both views are deterministic projections.

If resume fails or reports a writer lock, diagnose before repair:

```bash
python3 "$CONTEXTCTL" doctor <task-dir>
```

Only a same-host dead owner with an unchanged healthy chain is eligible for
`unlock --stale`; never delete a lock manually. `CTX304` means a chain was copied
or moved away from its bound canonical root. Keep that chain read-only and create
a new task ID in the current workspace, or resume it at the original root. Never
rewrite archived reviews to make a copied chain pass.

## Start one bounded chain

1. Define one goal and stable workspace root. Use one active task chain per
   deliverable and workspace; do not copy `.continuity` chains between worktrees.
2. Choose a lowercase task ID using only lowercase alphanumerics, `.`, `_`, and
   `-`, at most 200 UTF-8 bytes.
3. Read [checkpoint-format.md](references/checkpoint-format.md), then create the
   initial candidate before substantive multi-session work:

   ```bash
   python3 "$CONTEXTCTL" draft <task-dir> --trigger init
   ```

4. Replace every skeleton marker. Keep only active contract, current state,
   active decisions, current blockers, expensive failures, one next action, one
   verification check, and evidence required by those active claims.

## Checkpoint only durable changes

At a required trigger, draft from the latest published state:

```bash
python3 "$CONTEXTCTL" draft <task-dir> --trigger <trigger>
```

Required triggers are:

- user correction or authorized change to goal, scope, permissions, or success;
- accepted or superseded decision that controls future work;
- verified completion of a meaningful phase;
- a pivot or failure whose repetition would be costly or unsafe;
- pause, block, approval wait, agent/session handoff, or host pre-compaction;
- final completion.

Do not checkpoint after each turn, file edit, test run, or transient failure.

## Keep the capsule lean

Aim for an active checkpoint at or below 8 KiB. Treat 12 KiB as a compression
stop sign unless exact active contract or safety facts justify the growth. The
structural maximum remains 128 KiB for compatibility, not as a writing target.

- Keep at most a few current verified milestones; move completed detail to cited
  artifacts.
- Keep only decisions, blockers, assumptions, and failures that can still change
  the next action or prevent harm.
- Evidence belongs in the active checkpoint only while it supports an active
  claim.
- Published history is already immutable provenance. When evidence is superseded,
  missing, or changed, record the explicit transition and replacement/downgrade;
  do not copy the entire inactive pointer and narrative into every successor.
- Preserve exact corrections, permissions, acceptance values, error signatures,
  and unresolved conflicts.

Read [compaction-gate.md](references/compaction-gate.md) when preparing a publish.

## Review and publish proportionally

Create the bound review:

```bash
# Initial checkpoint with a stable source artifact:
python3 "$CONTEXTCTL" review-init <candidate> \
  --output <task-dir>/review.json --source <source>

# Initial checkpoint with no prior source:
python3 "$CONTEXTCTL" review-init <candidate> \
  --output <task-dir>/review.json --no-source-reason "<why none exists>"

# Later checkpoints omit both source options.
```

For a routine local checkpoint, an honest clean-room self-review is sufficient;
do not spawn another agent merely to satisfy ceremony. Use a distinct fresh
reviewer when the user requires independent review or the checkpoint authorizes
production/deployment, security/privacy changes, destructive operations, or
other high-impact external mutations.

The reviewer opens the candidate and active evidence, fills only reviewer-owned
fields, copies the exact next action and verification, records contradictions,
and sets `PASS` only when every required check is complete. Machine-owned hashes,
IDs, bindings, and detected statuses never change. Full field rules and the
unavailable-evidence fallback live in the two reference documents.

Then validate and publish:

```bash
python3 "$CONTEXTCTL" review-check <candidate> <task-dir>/review.json
python3 "$CONTEXTCTL" publish <candidate> --review <task-dir>/review.json
```

Any candidate edit invalidates its review. Resolve every publication error before
the next durable phase. Never edit or delete a published checkpoint, sidecar, or
required marker.

## Finish

After final evidence already proves acceptance:

```bash
python3 "$CONTEXTCTL" complete <task-dir>
```

Review and publish the terminal candidate through the same gate. Completion is
monotonic. A completed chain accepts only its bounded postcommit receipt or an
evidence repair that remains complete; reopened or new scope starts a new task ID.

## Core invariants

- Preserve exact user corrections, constraints, permissions, and acceptance
  criteria until explicitly superseded.
- Preserve uncertainty; never compact an unresolved claim into fact.
- Keep stable IDs as semantic identities and record explicit transitions.
- Separate pointer reachability from semantic support.
- Use one checkpoint writer; parallel workers return focused deltas.
- Failed or concurrent publication leaves the previous latest intact.
- Local hashes prove integrity and workflow provenance, not independent authorship.

## Quality bar

- The brief card is enough for routine continuation; the full card plus active
  evidence is enough for a cold handoff.
- The capsule stays small because inactive history does not accumulate in the
  current state.
- Contract changes remain authorized, history remains append-only and hash-linked,
  and high-impact transitions receive proportionate review.
