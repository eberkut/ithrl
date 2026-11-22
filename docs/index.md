# ithrl documentation

<!-- rumdl-disable MD033 -->
<div class="ithrl-wordmark">
  <img class="ithrl-logo-light" src="branding/ithrl-logo-light.svg" alt="ithrl" width="280" height="138">
  <img class="ithrl-logo-dark" src="branding/ithrl-logo-dark.svg" alt="ithrl" width="280" height="138">
</div>
<!-- rumdl-enable MD033 -->

`ithrl` turns saved coding-agent sessions into debugging experiments. Find a smaller context that
preserves a selected behavior, test a text append that changes it, or compare two selected contexts.
Passive compaction inspection reads recorded operations and results without model calls.

## Start here

Choose the task you want to complete:

| Task | Start with |
| --- | --- |
| Try ithrl without an agent or model account | [Install ithrl](https://github.com/eberkut/ithrl/blob/main/README.md#install), then [run the bundled example](https://github.com/eberkut/ithrl/blob/main/README.md#try-it) |
| Use a saved session | [Select a decision and extract a case](native-runs.md#select-a-saved-decision) |
| Find a smaller context that preserves a behavior | [Native reduction](reduction.md) |
| Search for text appends that decrease the target's match rate | [Context fuzzing](fuzzing.md) |
| Test whether a selected context change repairs or degrades behavior | [Context comparison](comparison.md) |
| Inspect compaction without model calls | [Recorded operations and captured results](results.md#inspect-recorded-compaction) |
| Inspect saved results or estimate token costs | [Results and privacy](results.md) |
| Use ithrl through a coding agent | [Agent skill](agent-skill.md) |

The [worked reduction](research/ordinary-session-investigation.md),
[comparisons](research/comparative-debugging.md) and
[tool output investigations](research/tool-output-interventions.md) show how to interpret the
experimental results.

## Reference and background

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
