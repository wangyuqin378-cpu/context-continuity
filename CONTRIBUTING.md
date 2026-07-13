# Contributing

Thank you for helping make long-running agent work easier to resume and harder to
silently corrupt.

## Before opening a change

- Start with the user failure the change prevents. Protocol complexity needs a
  concrete recovery, integrity, or operator-experience benefit.
- Search existing issues and the bounded release report for prior attempts.
- Keep the trust boundary explicit. Do not turn a local integrity check into a
  universal reliability or authorship claim.

## Development workflow

1. Create a focused branch.
2. Add or update a deterministic regression test before changing an invariant.
3. Make the smallest coherent implementation change.
4. Update `SKILL.md`, a reference document, or the README when operator behavior
   changes.
5. Run:

   ```bash
   python3 -m unittest discover -s tests -p 'test_*.py'
   python3 -m py_compile scripts/checkpoint_guard.py scripts/contextctl.py
   ```

6. In the pull request, describe the failure mode, the invariant being changed,
   the observable user impact, and the verification evidence.

## Protocol expectations

- Published checkpoints remain append-only and hash-linked.
- User corrections, permissions, scope, decisions, blockers, failures, evidence,
  next action, and verification conditions cannot disappear silently.
- Reachability and semantic support remain separate evidence checks.
- Failed publication must preserve the previously published latest checkpoint.
- Completion remains monotonic; new work uses a new task chain.
- Error messages must say whether state changed and what the safe next step is.

## Evaluation artifacts

The public repository tracks the frozen fixture, scorer, and bounded final report.
Generated trial outputs and local iteration notes under `evals/outputs/` are not
committed. If a change alters a measured claim, include a reproducible protocol
update and revise the bounded report rather than replacing an inconvenient failed
result.

## Pull requests

Keep pull requests focused. A good description answers four questions:

1. What can go wrong for the user today?
2. Which invariant or experience changes?
3. What new test would have caught the old behavior?
4. What remains outside the claim boundary?
