# Security policy

## Report a vulnerability

Use [GitHub's private vulnerability reporting](https://github.com/eberkut/ithrl/security/advisories/new)
to report a suspected vulnerability. Keep security reports out of public issues.

Include the affected version or commit, platform, impact, and a minimal reproduction using
synthetic content. Do not send credentials, private sessions, proprietary source, raw cases or
unreviewed diagnostic bundles. If the problem requires sensitive evidence, describe what it
contains first so the maintainer can agree on a safe way to investigate.

The maintainer will coordinate investigation, a fix where appropriate, and disclosure with the
reporter. Response and resolution times depend on maintainer availability.

## Supported versions

Security fixes target the latest published release. Older releases do not have separate
maintenance branches; upgrading may be necessary to receive a fix. Before the first release,
report issues against the default branch.

Pre-1.0 releases may change persisted formats and APIs. Review release notes before upgrading.

## Relevant boundaries

Report suspected isolation escapes, execution of returned actions, unintended provider calls,
credential exposure, or writes to protected source evidence or the original workspace.

Saved sessions and model output are untrusted input. Native experiments require explicit model
execution acknowledgement and a verified capability profile; returned actions must remain denied.
The [native-run guide](docs/native-runs.md) describes prerequisites, and
[results and privacy](docs/results.md) explains data handling and the local threat model.
