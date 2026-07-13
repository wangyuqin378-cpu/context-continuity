# Raw long-task source: webhook retry recovery

This is a synthetic chronological work log. It deliberately mixes durable facts,
superseded decisions, repeated output, and unrelated chatter. A handoff writer must
recover the current state without treating every line as active truth.

## Turn 1 — request

User: `[G001]` Fix the webhook retry path so another engineer can safely resume the
work after a context reset. `[I001]` Start with the webhook worker, its tests, and
the deployment note. `[O001]` Do not redesign the admin dashboard or billing flow.

User: `[S001]` Initial acceptance idea: p95 below 600 ms in one local run. `[C001]`
The early demo is currently on port 8080. Do not touch unrelated modules.

Agent: I will inspect the worker and its tests, then change the smallest surface.

## Turn 2 — initial inspection

Tool output: `src/webhook.py` retries immediately with no backoff. The test file is
`tests/test_webhook_retry.py`. The deployment note still says SQLite is enough for
the demo. The log contains an old branch name, `feat/retry-spike`.

`NOISE-ALPHA` dependency scan line 001: package cache warm.
`NOISE-ALPHA` dependency scan line 002: package cache warm.
`NOISE-ALPHA` dependency scan line 003: package cache warm.

Agent: `[D001]` Proposed decision: store retry state in SQLite and use a fixed
one-second delay. `[W001]` Remaining: implement fixed delay and rerun all tests.

## Turn 3 — unrelated discussion

Meeting note: the team may revisit dashboard colors next quarter. Someone likes a
blue theme. Finance asked whether invoices can export CSV. These topics are not
part of the webhook task. The office Wi-Fi was unstable in the afternoon.

`NOISE-BETA` browser console: favicon missing.
`NOISE-BETA` browser console: favicon missing.
`NOISE-BETA` browser console: favicon missing.

## Turn 4 — first implementation and failure

Agent: Added a fixed delay and launched the full suite with verbose traces.

Tool output excerpt:

```text
collecting 1487 tests
worker trace buffer: 1.8 GB
worker trace buffer: 2.4 GB
process terminated: OOM-KILL-137
```

Agent: `[F002]` Do not repeat the full verbose suite before running the focused
retry tests; error signature is `OOM-KILL-137`. `[W002]` Focused retry test still
fails because the payload hash changes between attempts.

## Turn 5 — user correction changes the contract

User: Correction: `[I002]` narrow the active code scope to `src/webhook.py` and
`tests/test_webhook_retry.py`; the deployment note may be cited but should not be
edited. `[C002]` The required local port is **18080**, not 8080. Preserve that exact
number. `[S002]` Real acceptance is p95 below **450 ms across three consecutive
runs**, with zero retry-state errors. The earlier S001/C001 values are superseded.

User: Keep `[O001]` unchanged. Do not pull the dashboard or billing flow into this.

## Turn 6 — architecture correction

Tool output: the worker already writes durable attempt state to Postgres through
`RetryStore`; SQLite would create a second source of truth.

Agent: `[D003]` Supersede D001. Use the existing Postgres `RetryStore` with bounded
exponential backoff, at most **3 attempts**, and no new persistence layer. Reason:
one retry source of truth avoids state divergence. `[W003]` Implement this path and
add a deterministic payload hash assertion.

User: Agreed. Keep max attempts at 3.

## Turn 7 — implementation evidence

Tool output: focused tests pass after the bounded-backoff change.

```text
tests/test_webhook_retry.py::test_backoff_schedule PASSED
tests/test_webhook_retry.py::test_attempt_state PASSED
tests/test_webhook_retry.py::test_payload_hash FAILED
expected sha256: 17aa
observed sha256: 17ab
```

Agent: `[E002]` `tests/test_webhook_retry.py#test_backoff_schedule` proves the
backoff schedule and max-attempt rule. `[W003]` implementation is complete, but the
hash assertion exposes one remaining issue.

`NOISE-GAMMA` repeated trace: attempt=1 connection reused.
`NOISE-GAMMA` repeated trace: attempt=1 connection reused.
`NOISE-GAMMA` repeated trace: attempt=1 connection reused.
`NOISE-GAMMA` repeated trace: attempt=1 connection reused.

## Turn 8 — latest measured state

Tool output: two performance runs succeeded, one remains before acceptance can be
claimed.

```text
run 15 p95=421ms retry_state_errors=0
run 16 p95=433ms retry_state_errors=0
run 17 blocked before measurement: payload hash mismatch 17aa/17ab
```

Agent: `[E003]` `evidence/run-17.json#summary` contains the latest exact results.
`[W004]` The only active work is to determine why canonical payload serialization
changes the hash on run 17, fix it if the source confirms a defect, then complete
the third performance run.

## Turn 9 — blocker and assumption

User: `[B002]` Staging webhook-secret access is pending security approval. Do not
invent or rotate a credential. Local payload investigation can continue; staging
verification must wait for approval.

Agent: `[A001]` It is unknown whether the pending credential has already been
rotated. Treat that as an assumption, not a verified fact.

## Turn 10 — explicit handoff boundary

User: Pause here and hand this to a fresh agent. The one next action is
`[NEXT-VERIFY-PAYLOAD-17]`: compare canonical serialization before and after retry
inside `src/webhook.py`, using the failing `17aa/17ab` example. Do not start staging
work.

User: The verification for that next action is `[VERIFY-3X-450MS]`: the focused
payload-hash test passes, followed by three consecutive local runs on port 18080
with p95 below 450 ms and zero retry-state errors. The third run is still missing.

Agent note: Current active decision is D003, current scope is I002, current success
criterion is S002, and current constraint is C002. D001, S001, C001, W001, W002,
and W003 are historical or resolved and must not be presented as current work.

## Trailing distractor output

`NOISE-DELTA` lint cache hit.
`NOISE-DELTA` lint cache hit.
`NOISE-DELTA` lint cache hit.
Reminder from another project: update the podcast transcript index next month.
This reminder is unrelated to webhook retry recovery.
