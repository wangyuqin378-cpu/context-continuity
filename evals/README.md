# Context Continuity Effectiveness Eval

This benchmark measures whether a handoff remains correct and compact after a
noisy, multi-phase task. It compares a generic concise handoff with a checkpoint
produced under `context-continuity` from the same source.

## Fixture

`fixtures/long-task-source.md` contains current facts, superseded contract items,
resolved work, one blocker, one unsafe-to-repeat failure, repeated logs, and one
explicit next action. `fixtures/ground-truth.json` defines the protected facts,
required transitions, distractor tokens, and thresholds.

## Trial protocol

Run at least three independent trials per variant.

Baseline producer prompt:

> Read the fixture and write a concise English handoff for a fresh agent. Preserve
> exact identifiers, values, paths, constraints, decisions, blockers, failed
> attempts, evidence, next action, and verification. Do not use or inspect the
> context-continuity skill.

Checkpoint producer prompt:

> Read the fixture and the context-continuity skill. Create an initial checkpoint
> in the assigned task directory and follow both references. Use the current
> high-level path: draft, review-init with the fixture as source, fresh source-first
> manifest completion, review-check, publish, and cold-start rehearsal through
> resume. Use only source-supported facts; do not use the low-level compatibility
> commands.

Current-schema producer prompt adds this fixed authorship boundary:

> Treat 30% as the hard publication gate and target at least 40% word and byte
> compression before review-init. Prefer one source-capture evidence ID for
> multiple attributed reports and keep each initial Delta label to one short line.

Score both variants with the same semantic anchors:

```bash
python3 evals/score_handoff.py \
  evals/fixtures/ground-truth.json \
  evals/outputs/round-0/baseline-1.md

python3 evals/score_handoff.py \
  evals/fixtures/ground-truth.json \
  evals/outputs/round-0/checkpoint-1.md \
  --require-ids --strict
```

## Promotion gate

- protected-fact recall: 100%;
- current-contract transition recall: 100%;
- repeated noise retained: 0;
- compression from raw source: at least 30%;
- three fresh recovery agents independently identify the current contract,
  decisions, blocker, failed attempt, next action, and verification;
- existing guard tests remain green.

## Fixed Resume Card recovery protocol

Give each fresh reader exactly one `contextctl resume <task-dir>` output and no
checkpoint, source, review, ground truth, or peer answer. Require a concise answer
with these slots, preserving every stable ID shown by the card:

1. `CONTRACT IDS` — exact G/I/O/S/C meanings;
2. `CURRENT IDS` — verified state and the single W###;
3. `DECISION IDS` — active D### and rationale;
4. `BLOCKER / FAILURE / UNKNOWN IDS` — every B/F/A identifier;
5. `SUCCESSOR TRANSITIONS` — every exact `OLD -> NEW` pair;
6. `DO NOW` — the exact action bullet;
7. `DONE WHEN` — the exact verification bullet.

The prompt is frozen before the accepted run. A response that keeps a meaning but
drops its stable ID is a strict failure; do not repair or overwrite it.

An iteration changes one hypothesis only. Stop after a fully passing round or
after four rounds; report the best measured safe variant, not a global optimum.

The public result summary, including failed attempts, evidence classes,
experience measurements, and explicit limits, is recorded in
[`docs/EVALUATION.md`](../docs/EVALUATION.md).
