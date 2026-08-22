# Changelog

All notable public changes to Context Continuity are documented here.

## [2.1.0] - 2026-08-22

### Added

- A bounded daily Resume Card as the default, with `resume --full` preserving the
  complete V2 cold-start projection.
- CLI `--version` output and actionable `CTX304` diagnostics for chains copied or
  moved away from their canonical task root.
- Broken-task reasons in `list-tasks` output.

### Changed

- Added an explicit activation gate so short, cheap-to-restart work does not pay
  checkpoint and review overhead.
- Routine local checkpoints may use an honest clean-room self-review; independent
  reviewers remain required for explicitly independent or high-impact transitions.
- Inactive evidence provenance stays in immutable history instead of being copied
  into every successor capsule.
- Added 8 KiB authoring and 12 KiB compression targets while preserving the
  existing 128 KiB compatibility ceiling.

### Fixed

- Documented a safe read-only fallback for accidentally nested skill installs and
  required a flat reinstall before writes.

## [2.0.0] - 2026-07-13

### Added

- One-command audited resume with a deterministic active or terminal Resume Card.
- Append-only, continuous, hash-linked Markdown checkpoint chains.
- Explicit delta and multi-metric compaction checks at durable state changes.
- Source-first review bound to candidate, source, local evidence, protected IDs,
  next action, and verification condition.
- Lock-protected atomic publication of checkpoint, review sidecar, and marker.
- State-aware `doctor`, safe stale-lock handling, and working-state projection.
- Monotonic completion with bounded post-commit receipt and evidence repair.
- Early task-ID and trigger validation with zero-write failures.
- A 127-test deterministic suite and 91-case independent terminal red-team.

### Documentation

- Added English and Simplified Chinese user-first READMEs.
- Published the reproducible evaluation protocol and bounded release report.
- Added contribution, security, CI, and license files for public collaboration.

### Boundary

- Host lifecycle hooks, GUI integration, network services, same-permission
  malicious-process authentication, and universal model reliability remain out of
  scope.
