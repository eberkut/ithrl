# ithrl documentation

<!-- rumdl-disable MD033 -->
<div class="ithrl-wordmark">
  <img class="ithrl-logo-light" src="branding/ithrl-logo-light.svg" alt="ithrl" width="280" height="138">
  <img class="ithrl-logo-dark" src="branding/ithrl-logo-dark.svg" alt="ithrl" width="280" height="138">
</div>
<!-- rumdl-enable MD033 -->

`ithrl` extracts a selected decision from a saved coding-agent session and searches for context
that preserves its behavior. Context fuzzing explores text appends to prior user input for Codex
and Copilot final-response targets.

Start with
[installation and the bundled example](https://github.com/eberkut/ithrl/blob/main/README.md#install).
The example runs locally without an agent installation or model access.

- [Native runs](native-runs.md): select a saved decision, configure a reduction experiment and
  inspect its provider, safety controls and budget before execution. The page also covers native
  fuzzing and a provider-free walkthrough.
- [Results and privacy](results.md): inspect retained context, interpret preservation and usage,
  and keep private session data out of public reports.
- [CLI reference](cli.md): commands, arguments, options and defaults generated from the CLI.
- [Troubleshooting](troubleshooting.md): selection, setup, refusal and resume problems.
- [Measurements](measurements.md): benchmark construction, outcomes and costs.

For development context, see [architecture](architecture.md), [decision records](decisions/index.md)
and [research](research/index.md). The
[roadmap](https://github.com/eberkut/ithrl/blob/main/ROADMAP.md) links future direction to the
public issue tracker. The [Project board](https://github.com/users/eberkut/projects/1/views/4) shows
work status and dependencies.

For help or a proposed change, see [Support](https://github.com/eberkut/ithrl/blob/main/SUPPORT.md).
Report suspected vulnerabilities through
[private vulnerability reporting](https://github.com/eberkut/ithrl/security/advisories/new).
Contributor setup and checks are in
[Contributing](https://github.com/eberkut/ithrl/blob/main/CONTRIBUTING.md).

The site's theme, fonts and browser assets have [third-party notices](third-party-notices.txt).
