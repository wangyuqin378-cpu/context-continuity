# Five-minute quick start

This walkthrough shows the shortest user path. The agent operates the checkpoint
CLI; the user supplies a stable task ID and workspace.

## 0. Install and verify

```bash
mkdir -p ~/.codex/skills
git clone https://github.com/wangyuqin378-cpu/context-continuity.git \
  ~/.codex/skills/context-continuity
python3 ~/.codex/skills/context-continuity/scripts/contextctl.py --help
```

Start a new Codex task after installation so the skill catalog reloads. For an
existing installation, run:

```bash
git -C ~/.codex/skills/context-continuity pull --ff-only
```

## 1. Protect continuity files

Add this to the target project's `.gitignore` unless the checkpoints have been
reviewed for publication:

```gitignore
**/.continuity/
```

Checkpoints may contain project goals, local paths, decisions, blockers, and
evidence metadata. Never put credentials or private keys in them.

## 2. Start the task

Give the agent a concrete outcome and tell it to activate the skill:

```text
Use context-continuity for this task.

Task ID: checkout-timeout
Workspace root: /absolute/path/to/storefront
Goal: remove checkout timeouts without changing the public checkout API.
Success: the focused timeout suite passes and the staging trace shows no duplicate charge.

Create checkpoints only when durable state changes.
```

Before substantive work, the agent should create and publish
`.continuity/checkout-timeout/checkpoints/0001-init.md` through the review gate.

## 3. Let work continue normally

You do not need to ask for a checkpoint every turn. The skill requires one when:

- you correct scope, permission, or success criteria;
- a durable decision is accepted or replaced;
- a phase is verified;
- an attempt fails in a way that should not be repeated;
- work blocks, pauses, hands off, or approaches context compaction;
- the task really completes.

Each new checkpoint compares itself with the previous one and keeps only durable
state.

## 4. Resume in a fresh session

```text
Resume checkout-timeout with context-continuity.
Use the Resume Card as the recovery entry point. Treat the published checkpoint
and cited evidence as the source of truth; do not reconstruct the task from chat.
```

The agent runs:

```bash
SKILL_DIR="$HOME/.codex/skills/context-continuity"
python3 "$SKILL_DIR/scripts/contextctl.py" resume \
  /absolute/path/to/storefront/.continuity/checkout-timeout
```

The card should answer, without opening older history:

- What outcome and boundaries govern the task?
- What has been verified?
- Which decisions control the approach, and why?
- What is blocked, unknown, or unsafe to repeat?
- What is the one next action?
- What exact observation marks that action complete?

## 5. If resume refuses to continue

Do not delete files or locks manually. Ask the agent to diagnose the task:

```text
Run context-continuity doctor for checkout-timeout. Explain what changed, whether
the published state is still safe, and the exact next repair action.
```

`doctor` distinguishes an active draft, incomplete review, evidence drift, stale
same-host lock, healthy committed chain, and hard integrity failure.

## 6. Finish honestly

Completion requires already-existing final evidence. A planned receipt is not
proof that publication committed. The agent creates a terminal candidate with
`complete`, reviews it, publishes it, and keeps the task complete afterward.

If new work appears later, start a new task ID instead of reopening the completed
chain.
