# Context Continuity

[简体中文](README.zh-CN.md) · [Walkthrough](examples/quickstart/README.md) · [v2.1.0](https://github.com/wangyuqin378-cpu/context-continuity/releases/tag/v2.1.0)

## What it is

An Agent Skill and local Python CLI for continuing long tasks after an interruption, context compaction or handoff. It saves the current goal, decisions, evidence and next action as Markdown checkpoints, then produces a compact **Resume Card** for the next session.

It is for work that spans sessions and has decisions worth preserving. It is usually unnecessary for a short, disposable task. Python 3.9+, standard library only; local CLI checks cover macOS and Linux.

## How to use it

### Install in Codex

Clone into a new skill directory; do not overwrite an existing installation:

```sh
mkdir -p ~/.codex/skills
git clone https://github.com/wangyuqin378-cpu/context-continuity.git \
  ~/.codex/skills/context-continuity
python3 ~/.codex/skills/context-continuity/scripts/contextctl.py --version
```

Start a new Codex task so the skill catalog reloads. Other skill-aware hosts can read [SKILL.md](SKILL.md), but their complete integration has not been validated here.

### Start a task, then resume it

Give the agent a stable task ID and real workspace path:

```text
Use context-continuity for this task.
Task ID: checkout-timeout
Workspace root: /absolute/path/to/storefront
Goal: remove checkout timeouts without changing the public API.
Success: the timeout tests pass with no duplicate charge in the staging trace.
Checkpoint durable changes, not every turn.
```

The agent uses the review-and-publish workflow to maintain `.continuity/checkout-timeout/`. Keep `**/.continuity/` in the target project's `.gitignore` unless you have reviewed its contents for publication; checkpoints can contain local paths and project details.

After an interruption, ask in a fresh session:

```text
Resume checkout-timeout with context-continuity.
Read the full checkpoint before continuing. Follow its next action and check.
```

The underlying recovery command is:

```sh
python3 ~/.codex/skills/context-continuity/scripts/contextctl.py resume \
  /absolute/path/to/storefront/.continuity/checkout-timeout --full
```

A Resume Card answers **what is current, what to do next and how to check it**. This abbreviated example shows its shape, not a live result:

```text
GOAL: Remove checkout timeouts without changing the public API.
DO NOW: Reproduce the remaining payload-hash mismatch.
DONE WHEN: Three fixed payloads produce the expected stable hashes.
```

Use the [complete walkthrough](examples/quickstart/README.md) for the first checkpoint, recovery and safe completion. For a routine continuation after the contract is loaded, omit `--full` for the shorter daily view.

## Why this project exists

A long conversation can remember a lot while still losing the one correction that matters. After a handoff, an agent may repeat a failed approach, revive an old requirement or mistake an unchecked claim for a finished result.

Context Continuity makes those facts explicit and recoverable. It preserves the current contract and evidence pointers, rather than trying to keep every message. New checkpoints record meaningful changes; the next session starts from one action and one observable check.

Structural validation does not prove that every claim in a checkpoint is true. The review process still depends on the evidence and reviewer. See the [evaluation scope](docs/GUIDE.md#measured-results) and [trust boundary](docs/GUIDE.md#limits-and-trust-boundary) before relying on it for consequential work.

[Detailed guide](docs/GUIDE.md) · [Evaluation protocol](evals/README.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [MIT license](LICENSE)
