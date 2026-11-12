# ithrl documentation

<!-- rumdl-disable MD033 -->
<div class="ithrl-wordmark">
  <img class="ithrl-logo-light" src="branding/ithrl-logo-light.svg" alt="ithrl" width="280" height="138">
  <img class="ithrl-logo-dark" src="branding/ithrl-logo-dark.svg" alt="ithrl" width="280" height="138">
</div>
<!-- rumdl-enable MD033 -->

`ithrl` turns saved coding-agent sessions into debugging experiments: reduce context while
preserving a selected behavior, or test text appends that alter it. You can also inspect recorded
operations around compaction without running a model.

## Start here

- **Learn the workflow:**
    [install ithrl](https://github.com/eberkut/ithrl/blob/main/README.md#install) and
    [run the bundled example](https://github.com/eberkut/ithrl/blob/main/README.md#try-it). It needs
    no agent installation or model access and shows the expected output.
- **Use your own session:** [select a saved decision](native-runs.md#select-a-saved-decision),
    [prepare a reduction](reduction.md) and review its provider, safety
    controls and budget. The [fuzzing walkthrough](fuzzing.md) covers text
    appends.
- **Inspect compaction without model calls:**
    [compare recorded operations and captured results](results.md#inspect-recorded-compaction) in
    one saved session.
- **Investigate a result:** [inspect retained context](results.md#inspect-retained-context) and
  read the [worked investigation](research/ordinary-session-investigation.md) for an example of
  what the reduced context can tell you.

## Reference and background

- [CLI reference](cli.md): commands, arguments, options and defaults generated from the CLI.
- [Results and privacy](results.md): saved files, preservation, resource usage, resume and private
  data handling.
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
