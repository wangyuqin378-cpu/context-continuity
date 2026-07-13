# Changelog

All notable public changes to Context Continuity are documented here.

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
