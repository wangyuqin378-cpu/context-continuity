# Security Policy

## Supported version

Security fixes are applied to the latest version on the default branch.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository when available.
If that option is not visible, open a minimal issue asking the maintainer for a
private reporting channel. Do not include exploit details, secrets, private paths,
or sensitive checkpoint content in a public issue.

Useful reports include:

- the affected command and version or commit;
- the smallest safe reproduction;
- whether any checkpoint, review, marker, lock, or external file changed;
- the expected fail-closed behavior;
- potential impact within the documented local trust boundary.

## Security boundary

Context Continuity is a local workflow integrity tool. Its hashes, manifests,
locks, and path checks are designed to detect accidental drift, stale review,
unsafe pointers, invalid transitions, and partial publication. They are not a
cryptographic identity system and do not authenticate a reviewer against a
malicious process with the same filesystem permissions.

Never store credentials, access tokens, private keys, raw hidden reasoning, or
unnecessary personal data in checkpoints or evaluation fixtures.
