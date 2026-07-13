---
name: context-continuity
description: Preserve the minimum verified task state across context compaction, interruptions, agent or session handoffs, and restarts. Use when the user requests durable checkpointed execution or a long, multi-session task explicitly needs persistent Markdown state; within that work, use it at major decisions, phase transitions, handoffs, pauses, blocks, and completion.
compatibility: Requires Python 3.9+ for structural validation and atomic publication.
---

# Context Continuity

Keep long-running work restartable without carrying the whole conversation.

While this skill is active, checkpoint publication is a hard gate: do not start
the next phase until it succeeds. A plain skill cannot intercept every stop,
crash, compaction, or uninvoked session; without host hooks, describe this as a
manual/best-effort guarantee.

## Workflow

Resolve the directory containing this `SKILL.md` as `SKILL_DIR` before running
any command below.

1. Bound one goal and choose a stable `task-id`. It must start with a lowercase
   alphanumeric and then use only lowercase alphanumerics, `.`, `_`, or `-`; a
   new task ID is at most 200 UTF-8 bytes.
   If several tasks exist and the intended one is unclear, list them before asking:

   ```bash
   python3 "$SKILL_DIR/scripts/contextctl.py" list-tasks <workspace-root>
   ```
2. Resolve a stable workspace root from the active artifact or an explicit user
   path, never from a changing current directory. If no stable root is known,
   require an absolute `task-dir`. Store one append-only chain at
   `<workspace-root>/.continuity/<task-id>/`. Sequence starts at `0001`; the
   largest continuous sequence is authoritative. Never use `mtime`.
3. For a new task, read [checkpoint-format.md](references/checkpoint-format.md)
   and create the initial candidate before substantive work:

   ```bash
   python3 "$SKILL_DIR/scripts/contextctl.py" draft <task-dir> --trigger init
   ```

   Replace every skeleton marker, then review and publish it as `0001-init.md`
   through step 7.
4. To resume, use the safe high-level entry:

   ```bash
   python3 "$SKILL_DIR/scripts/contextctl.py" resume <task-dir>
   ```

   This audits the full checkpoint and review-attestation chain, validates latest,
   reports source/evidence health, and renders a deterministic Resume Card. Use
   `--json` for machine-readable output.
   Do not open history unless the card reports a conflict, provenance is requested,
   or a cited source must be revalidated.
   If resume fails or reports a writer lock, diagnose before taking any repair step:

   ```bash
   python3 "$SKILL_DIR/scripts/contextctl.py" doctor <task-dir>
   ```

   Only a same-host dead owner with an unchanged, healthy chain is eligible for
   `contextctl.py unlock <task-dir> --stale`. Never delete a lock manually or use
   age alone as proof that it is stale.
   A missing or changed source/evidence file is a warning, not erased history.
   Use `draft <task-dir> --trigger evidence-repair`, preserve the old E### and
   pointer as inactive provenance, then publish the downgraded or replacement
   state through a fresh review. Missing/tampered review sidecars or markers remain
   hard failures and never enter this repair path.
5. Continue from the Resume Card's exact action and verification check. The card
   is a read-only projection; the published checkpoint remains the source of truth.
6. At a required trigger, create a safe draft and edit only durable facts, Delta,
   and the next action/check:

   ```bash
   python3 "$SKILL_DIR/scripts/contextctl.py" draft <task-dir> --trigger <trigger>
   ```

   A trigger starts with a lowercase alphanumeric and then uses only lowercase
   alphanumerics or `-`, up to 200 UTF-8 bytes. Invalid task IDs and triggers fail
   before a directory or candidate is created. Do not use `--trigger completion`; use `complete` in
   step 8 so the terminal transition is generated and validated as one operation.

   The tool fills sequence, parent, hashes, and time while preserving the canonical
   body. Read [checkpoint-format.md](references/checkpoint-format.md) only when the
   candidate needs structural editing.
7. Read [compaction-gate.md](references/compaction-gate.md), then bind a fresh review
   to the prepared candidate and publish through the high-level gate:

   ```bash
   # Initial checkpoint with a stable source artifact:
   python3 "$SKILL_DIR/scripts/contextctl.py" review-init <candidate> \
     --output <task-dir>/review.json --source <source>
   # If no prior artifact exists, replace --source with an explicit provenance:
   python3 "$SKILL_DIR/scripts/contextctl.py" review-init <candidate> \
     --output <task-dir>/review.json --no-source-reason "<why none exists>"
   # Later checkpoints omit both source options.
   ```

   After `review-init`, freeze the candidate and open `<task-dir>/review.json`.
   A fresh reviewer means a different agent or clean session that did not author
   the candidate and does not use other task state. For an initial checkpoint it
   opens the manifest-bound source first; it then opens the candidate and every
   cited evidence item. The reviewer edits only reviewer-owned fields:

   - set `reviewer` and `reviewed_at`;
   - for an available initial source, complete `source_review` by recovering every
     source ID, clearing `missing_ids`, setting `coverage_status` to `COMPLETE`,
     and leaving no unresolved `omissions`;
   - copy every `required_id` into `recovered_ids`, clear `missing_ids`, and copy
     the exact Next Action and Verification bullets;
   - for each evidence item, leave machine-owned `detected_status` and bindings
     unchanged, set `status` to the matching `OK` or `EXTERNAL`, and set
     `semantic_status` only after inspection;
   - record contradictions or ambiguities instead of forcing a pass, and set
     `verdict` to `PASS` only when every required check is complete.

   Do not edit hashes, task/source bindings, detected IDs, required IDs, or other
   machine-owned fields. If a separate reviewer is unavailable, use a clean-room
   self-review with no authoring context and identify it honestly in `reviewer`;
   the local gate may pass, but do not describe that result as independent review.
   Any finding requires candidate repair and a new bound review, not a cosmetic
   manifest pass.

   Then validate and publish:

   ```bash
   python3 "$SKILL_DIR/scripts/contextctl.py" review-check <candidate> <task-dir>/review.json
   python3 "$SKILL_DIR/scripts/contextctl.py" publish <candidate> --review <task-dir>/review.json
   ```

   Review output is exactly `<task-dir>/review.json`; any other path or symlink is
   rejected. If it already exists, inspect it before explicitly rerunning
   `review-init --replace-existing`; do not overwrite it accidentally. Default
   creation is exclusive under concurrency. Explicit replacement is bound to the
   inspected old bytes, and a failed output write restores candidate bytes.
   Any candidate byte change invalidates the review. The manifest binds task root,
   parent, protected IDs, exact action, exact verification, and candidate SHA-256.
   `review-init` also owns `detected_status`, which `review-check` recomputes from
   the exact candidate and task workspace; reviewers never edit it. A missing or
   escaping relative pointer is not EXTERNAL. Reviewers separately set
   `semantic_status` to DIRECT, REPORTED, or UNSUPPORTED after opening the cited
   content. Follow the unavailable-direct-evidence fallback in
   [checkpoint-format.md](references/checkpoint-format.md); EXTERNAL describes a
   location, not proof.
   An initial review always requires `--source` or `--no-source-reason`. With a
   source, the tool binds path/SHA/size, requires 30% word+byte compression above
   500 words or 4 KiB, and makes the reviewer complete `source_review` against the
   bound source before candidate recovery. Local OK evidence also binds canonical
   path and content SHA-256. Source, evidence, or manifest drift blocks publication.
   Treat 30% as the hard gate, not the writing target: aim for at least 40% in both
   words and bytes before review-init so prepared metadata and model variance have
   margin. Prefer one accessible source-capture E### when it supports several
   explicitly attributed reports, and keep each initial Delta label to one short
   line without dropping any transition.
   High-level publish validates the Resume Card projection, then commits review
   sidecar, required marker, and checkpoint as one writer-lock transaction with
   the checkpoint last. Resume, later drafts, review initialization, and locked
   publish revalidate every current-schema checkpoint's frozen sidecar and marker
   across the full chain; deleting a historical attestation cannot silently fall
   back to legacy or be hidden by a later checkpoint. Current source/evidence bytes
   are rechecked for the candidate being published; drift in an already published
   checkpoint is surfaced as repairable health state rather than making a repair
   draft impossible.
   It prevents stale or misapplied local review; without host attestation it is not
   cryptographic proof that a distinct human or agent performed the review.
   Low-level guard commands remain available for compatibility and debugging, but
   they are not the compliant V2 publication path.
   Resolve every error before downstream work.
8. To finish, create a safe completion draft, confirm its final evidence, then run
   the same later-checkpoint review/publish gate:

   ```bash
   python3 "$SKILL_DIR/scripts/contextctl.py" complete <task-dir>
   ```

   Before running it, the latest published `Verification for Next Action` must
   cite only already-defined final E### evidence. Do not pre-reference a receipt E
   that can exist only after completion. If this check fails, publish a new
   phase-verified correction checkpoint; never edit the published predecessor.

   This sets exact `complete` state, resolves open W IDs in Delta, allocates a
   never-used terminal B### from the full audited history, and writes the terminal
   sentinel. B999 exhaustion fails before a draft is created. Publish only after
   final E### really proves acceptance.

   Completion is monotonic. A completed task can only receive one
   `postcommit-receipt` directly after its `completion` checkpoint or an
   `evidence-repair` maintenance checkpoint, and every such successor must remain
   `complete`. New scope or reopened work always starts a new task chain.

## Required triggers

- initial contract lock;
- user correction or change to permissions, scope, or success criteria;
- accepted or superseded durable decision;
- verified phase completion;
- pivot or failed attempt that should not be repeated;
- block, approval wait, pause, agent/session handoff, or host pre-compaction;
- final completion.

Do not checkpoint every turn. Checkpoint when durable state changes.

## Core rules

- Preserve exact user corrections, constraints, permissions, and acceptance
  criteria until explicitly superseded.
- Preserve conflicts and uncertainty; never compact an unresolved claim into fact.
- Keep active decisions with rationale, one next action, and one verification check.
- Treat stable IDs as semantic identities: decision changes require an explicit
  `Changed` entry, while contract changes require a new ID and authorization E###.
- Prefer exact source pointers, versions, tests, and error signatures over copied
  chat, raw logs, long tool output, secrets, or hidden reasoning.
- Before review, repair every MISSING or UNSAFE active evidence pointer. Never
  relabel a missing relative path as EXTERNAL or invent the absent artifact.
- Never rewrite or recompress a Resume Card. It is rendered deterministically from
  the canonical checkpoint so contract boundaries and exact verification survive.
- Do not require every version to be shorter. Any growth must be explained by new
  durable facts across words, UTF-8 bytes, characters, and lines; detailed
  completed history must not accumulate. A checkpoint may not exceed 128 KiB.
- Use one checkpoint writer. Parallel agents return focused deltas to that writer.
- Never edit or delete a published checkpoint, and never bypass the guard's lock.
- Treat lock-owner and review identities as local workflow provenance, not as
  cryptographic authentication against a same-permission malicious process.

## Quality bar

- A fresh agent can continue from only the latest checkpoint and cited sources.
- The contract changes only through an authorized `contract-change` checkpoint.
- History is append-only, continuous, and hash-linked.
- Failed or concurrent publication leaves the prior latest checkpoint intact.
- The recovery capsule grows only when new durable information requires it.
