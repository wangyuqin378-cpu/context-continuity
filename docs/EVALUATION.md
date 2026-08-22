# Evaluation and claim boundary

Context Continuity is evaluated against one user outcome: **can a fresh agent
continue a noisy, long-running task without losing the current contract, repeating
an unsafe failure, or guessing the next step?**

This document separates checks that anyone can reproduce from empirical results
that depended on separate model readers during development.

## Evidence labels used here

- **Publicly reproducible** means the exact code, fixture, scorer, or test is in
  this repository and can be rerun from the documented command.
- **Frozen internal result** means the result was retained from the development
  evaluation but is not a third-party audit. The original model version and
  sampling parameters were not fully frozen, so exact LLM reproduction is not
  claimed.
- **Independent** means a separate reader or attack pass outside the primary
  implementation path. It does not mean organizational, financial, or third-party
  independence.

The sanitized [result manifest](../evals/frozen-results.json) records which claims
belong to each evidence class. These claims apply to the `v2.0.0` public release
tag, not automatically to later changes.

## What is reproducible from this repository

### Deterministic regression suite

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m py_compile scripts/checkpoint_guard.py scripts/contextctl.py
```

The frozen V2 suite contains 127 tests. It covers structural continuity,
source-first review, evidence binding, publication transactions, recovery cards,
working-state diagnosis, path containment, concurrency, terminal-state
monotonicity, malformed metadata, and fail-closed error routing.

The GitHub Actions workflow runs the suite on macOS and Ubuntu with Python 3.9
and 3.13.

### Recovery benchmark protocol

The repository includes:

- [`evals/fixtures/long-task-source.md`](../evals/fixtures/long-task-source.md), a
  synthetic task with current and superseded requirements, decisions, failures,
  evidence, noise, one blocker, and one next action;
- [`evals/fixtures/ground-truth.json`](../evals/fixtures/ground-truth.json), the
  protected facts and scoring thresholds;
- [`evals/score_handoff.py`](../evals/score_handoff.py), the deterministic scorer;
- [`evals/README.md`](../evals/README.md), the producer and cold-reader protocol.

Generated model responses are not deterministic. A new evaluator can rerun the
published protocol and score its own outputs, but should report every untouched
attempt rather than silently repairing failed trials.

## Frozen empirical results

The accepted development run produced:

| Measure | Baseline | Accepted V2 |
|---|---:|---:|
| Exposed recovery path | Multiple commands and manual reading | One `resume` command |
| Full cold-start operator view | About 77 lines | 23-line active / 22-line terminal card |
| Free-form/current-card first-pass recovery | 0/3 | 3/3 |
| Mean protected-fact recall | 92.3% | 100% |
| Feedback cycles across three readers | 5 | 0 |
| Exact accepted active-card recovery | Not available | 3/3; 90/90 IDs each |
| Unsupported facts in accepted recovery | Not measured | 0 |
| Deterministic regression suite | Not available | 127/127 |
| Internal separate-pass terminal attack matrix | Not available | 91/91 |
| Internal code/security and operator-UX audits | Not available | P0=0, P1=0, P2=0 |

The accepted producer trials averaged 37.17% word compression and 36.04% UTF-8
byte compression. Fixed-protocol recovery averaged 56.21% and 59.47%
respectively.

These model-reader and separate-pass audit numbers are frozen internal empirical
records, not deterministic unit-test claims or a third-party audit. The generated
workbench outputs were excluded
from the public repository because they contain environment-specific absolute
paths and runtime metadata; the fixture, scorer, protocol, and all deterministic
tests remain public.

For the internal audit labels: P0 meant a release-blocking integrity or safety
bypass; P1 meant a substantial functional, security, or operator-routing failure
that undermined a declared gate; P2 meant a lower-severity usability or
documentation defect. `P0=0, P1=0, P2=0` means no known open finding remained at
that audit closeout, not that the software is defect-free.

## Failures that changed the design

The final behavior came from keeping failed attempts visible rather than counting
only clean runs. Important failures included:

- a reviewer could relabel missing relative evidence as external;
- initial-source compression was not originally bound to review;
- stale review, projection drift, partial transactions, and path escapes were not
  all rejected;
- a valid checkpoint did not guarantee complete free-form cold recovery;
- completion could initially be reopened through lower-level paths;
- invalid task IDs and triggers could write unusable candidates before failing;
- over-broad metadata rejection broke one legitimate optional field;
- a planned post-commit receipt was incorrectly referenced as evidence before it
  existed.

Each accepted correction added a focused regression before the full suite was run
again.

## What the result means

Inside the declared local boundary, the implementation has a reproducible,
fail-closed state machine and a measured recovery improvement on the published
synthetic fixture.

It does **not** mean:

- every host will invoke the skill before a crash or context compaction;
- every model will author a semantically complete checkpoint on the first try;
- a local hash authenticates a human reviewer against a malicious same-permission
  process;
- an external evidence location automatically proves the associated claim;
- synthetic fixtures estimate every real workload;
- the current release is a GUI product, network service, or universal memory
  layer.

The release label is therefore **V2 bounded**, not “perfect memory” or “guaranteed
continuity.”
