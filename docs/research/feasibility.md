# Post-hoc experiment feasibility

The feasibility study completed on **2026-09-02** with a qualified decision to proceed:
ordinary saved sessions can supply decision evidence, and harness-specific runners can support
context interventions. Exact provider-request capture is optional. This is a historical research
record, not a compatibility table for current installations. The [README supported-inputs
table](https://github.com/eberkut/ithrl/blob/main/README.md#supported-inputs) owns public support;
the later [native measurements](../measurements.md) describe the qualified reduction profiles.

## What the study established

The initial question was whether a decision could be investigated after ordinary harness use,
without prior tracing, an interception proxy, persistent configuration changes, or an upstream
product change. Four separate questions determine the answer:

1. Does the saved evidence identify the decision and its preceding logical context?
2. Can an isolated candidate reconstruct retained and edited context at that boundary?
3. Can the runner observe the target behavior while preventing action execution?
4. Has that exact profile passed calibration, useful reduction and fresh confirmation?

Evidence extraction does not establish reconstruction. A working fork does not establish
filesystem or network isolation. A visible denied action does not establish useful reduction.
The initial study answered bounded technical-feasibility questions; later implementation and
native gates established the narrower product claims.

The credential-free control reduced a planted ten-node case to five nodes while retaining the
two behavior-relevant input items. It demonstrated dependency-aware reduction with zero provider
calls. It did not establish a stochastic-model preservation result.

## Four-harness portability matrix

This matrix records the versions inspected on 2026-09-02. **Observed** means a bounded local
control; **documented** means the vendor described the interface; **source** means inspected
public source; **unknown** means the study did not establish the capability. These strengths
apply to individual cells, not to a harness as a whole. Pi was deferred and remains unclassified.

| Capability | Codex `0.152.0` | Copilot `1.0.81` | Claude Code `2.1.258` | OpenCode `1.18.5` |
| --- | --- | --- | --- | --- |
| Ordinary decision evidence | Observed structured logical history, turns, messages and tool records | Observed structured session events, turns and tool records | Observed two existing JSONL transcripts with record identifiers and parent linkage | Observed SQLite session/message/part/event structure; bounded controls populated an initially empty store |
| Pre-decision boundary | Observed completed-turn fork and experimental before-turn fork | Observed exact pre-decision rewind | Observed record identities; documented resume/fork | Source: message-boundary fork, stable message/part identifiers |
| Edited candidate reconstruction | Observed historical removal and fresh history injection for final responses | Observed instruction-retained and instruction-removed rewind controls; independent repeatable construction remained implementation work | Unknown for arbitrary edited history; documented resume/fork alone is insufficient | Unknown for arbitrary edited imports; direct database rewriting was not accepted as a runner interface |
| Final-response observation | Observed retained instruction produced `done`; removal produced clarification | Observed removal stopped the selected tool behavior and produced a response | Observed bounded JSON result, stop condition, usage and latency | Observed JSON events and persisted response parts |
| Visible shell action with execution denied | Inert client-owned dynamic tool observed in a direct control; historical native `exec` and its mapped replay were not reproduced | Observed native `bash(pwd)` at the retained boundary; `shell` denial blocked execution | Observed scoped `Bash(pwd)` denial retained the proposed action and blocked execution | Observed `bash(command="pwd")` with `ask`, followed by rejection of its exact pending permission request |
| Isolation and side effects | Observed temporary session state and source preservation; model-visible history does not restore the native tool registry | Native operations appended events without rewriting earlier ones; rewind alone is not independent candidate isolation | Documented sandbox and restricted mode; fork copies conversation, not workspace state | Partial tool/file controls; no general hermetic network-denial contract established; sharing must be disabled |
| Research-to-product boundary | Final-response path viable; shell mapping calibration-gated | Native denied-shell mechanism viable; repeated candidates and statistical confirmation still required | Research only; no implemented adapter/runner or useful-reduction gate | Research only; no implemented adapter/runner or useful-reduction gate |

Claude's documented CLI/SDK session surfaces and OpenCode's CLI/server/SDK surfaces supported
bespoke runner designs. They did not justify a shared universal session format or advertised
experimental support. Transcript schemas, permissions, source-store effects and reconstruction
remain versioned adapter/runner responsibilities.

## Rejected shortcuts and failed controls

### A Codex rollout trace is not necessarily a provider request

Two successful public-profile Codex captures contained four completed inference requests. Both
first requests embedded `additional_tools` inside logical `input`, included internal message
metadata and lacked the required top-level public Responses tool declaration. Continuations
used `previous_response_id` and `custom_tool_call_output`. Preflight refused direct Responses
replay before network access. No direct replay was sent.

That four-request sample rejects this capture seam for the tested version/configuration; it is
not an applicability estimate for ordinary Codex sessions or every trace transport. The trace
event name is insufficient to infer capture completeness. The [rollout-trace documentation](https://github.com/openai/codex/blob/main/codex-rs/rollout-trace/README.md)
and [inference implementation](https://github.com/openai/codex/blob/main/codex-rs/rollout-trace/src/inference.rs)
were the original research references; those moving links do not pin the historical installation.
The subsequent native-surface audit used [Codex source commit
`316795b3`](https://github.com/openai/codex/tree/316795b3cf2a45e90d121d9f46499d4658b2645c)
for `rust-v0.152.0`. It found no supported isolated reconstruction path that restored the recorded
native tool registry. History injection restores model-visible history, not that runtime state.

### Codex controlled shell: a bounded negative result

A later separately bounded applicability test on **2026-10-01**, Codex `0.159.3`, used
`gpt-6.1-sol` and a single client-owned `ithrl_exec(cmd: string)` function. Native tools were
disabled; the client could return only a synthetic disabled result and could never execute the
arguments. A synthetic native-style history requested `pwd` on the fixed trigger. The preserved
selected call was expected-behavior evidence and was not injected into model context.

The frozen sequence required two independent full-context matches before either removal
control. Both full-context turns completed normally with fresh ephemeral state and **0/2
matches**: no native/dynamic action item or client tool request appeared. Removal controls did
not run. The two attempts took 8.796 and 10.250 seconds, with verified cleanup, unchanged source
and workspace, zero executed actions and zero residual state. Four possible provider calls were
charged against an eight-call bound; exact transport request counts were unavailable.

The result is **unsupported for that controlled mapping and plan**. It is neither proof that all
Codex dynamic-tool use is impossible nor evidence of historical native `exec` equivalence.
Because no action appeared, these attempts do not establish live action visibility or denial.
They did not change the already qualified final-response capability.

### OpenCode denial must preserve action visibility

An initial OpenCode control used a global deny overridden by the active custom agent's allow
rule and executed the harmless `pwd`. A subsequent verified static-deny control prevented the
command but removed Bash before inference, producing no tool/permission record. Neither path
met the required visible-but-denied contract.

The successful server control used an ephemeral, verified agent policy with Bash set to `ask`.
The server emitted the native tool part and `permission.asked`; rejecting that exact identifier
produced a permission rejection, an error tool part with no output, and an idle session. No
command executed. The first model turn asked for textual confirmation, so the successful
control needed a second user message. This is a one-shot protocol result, not a reduction or
arbitrary-history reconstruction result. Effective permission precedence must be checked;
static policy text alone is insufficient.

## Historical ordinary-cost evidence

The study reused usage/timing metadata without new model calls. It selected Codex `0.152.0`
records from 58 session files: 200 completed turns had both final token usage and completion
timing. Codex latency covers a whole agent turn and can include reasoning/tool work. The
Copilot `1.0.81` sample comprised 43 successful model-call records in five files, all short
controls created during the study. The samples are purposive and their clocks differ.

Bands used observed input tokens: short below 16,000; medium 16,000–63,999; long at least 64,000.

| Harness / band | Count | Input tokens, median (range) | Cached input, median | Output tokens, median | Latency, median | First token, median |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| Codex short | 2 | 14,772 (14,533–15,010) | 7,265 | 93 | 3.2 s | 2.2 s |
| Codex medium | 105 | 32,092 (16,197–63,226) | 27,797 | 94 | 5.2 s | 3.6 s |
| Codex long | 93 | 127,843 (64,000–268,149) | 121,763 | 546 | 53.7 s | 7.4 s |
| Copilot short | 43 | 549 (167–674) | 0 reported | 46 | 1.3 s | 1.0 s |
| Copilot medium / long | 0 | Unmeasured | Unmeasured | Unmeasured | Unmeasured | Unmeasured |

For Codex long turns, ninetieth-percentile observations were 234,443 input tokens, 309.8 seconds
whole-turn latency and 14.3 seconds to first token. Complete-session sizes were 63.4 MB total
for Codex (755 KB median, 5.2 MB maximum), and 15.1 MB/3,956 events for Copilot (3.3 MB median).
They are not per-candidate state-growth measurements.

Illustrative serialized arithmetic applied those medians to 380 requests: eight candidates at
20 observations, one 20-observation baseline epoch and 100 fresh observations per confirmation
arm. It yielded 12.19 million input tokens/33 minutes for Codex medium, and 48.58 million/5.7
hours for Codex long. Applying the long ninetieth percentile instead yielded about 89.1 million
input tokens/32.7 hours. These were planning scenarios, not production defaults, measured
campaigns, permission to share baselines, or predictions of current provider cost. The historic
five-percentage-point planning example required 470 requests for a final candidate and 5,170
for it plus ten independently tested neighbors, before search.

Candidate materialization and startup were unmeasured in this study. No defensible monetary
price existed for the Codex/Copilot access paths. The result motivated budget-first exploratory
screening with independent final confirmation, rather than promising narrow noninferiority or
confirmed minimality for an ordinary interactive run. [Reduction-design research](reduction-design.md)
records how later studies tested that choice.

## Research sources and limits

The original source review used the [Claude CLI reference](https://code.claude.com/docs/en/cli-usage),
[SDK sessions](https://code.claude.com/docs/en/agent-sdk/sessions),
[permissions](https://code.claude.com/docs/en/permissions),
[sandboxing](https://code.claude.com/docs/en/sandboxing) and
[data usage](https://code.claude.com/docs/en/data-usage). OpenCode references were its
[server API](https://opencode.ai/docs/server/), [CLI](https://opencode.ai/docs/cli/),
[permissions](https://opencode.ai/docs/permissions/) and [sharing](https://opencode.ai/docs/share/),
plus pinned `1.18.5` source for [agent permission merging](https://github.com/anomalyco/opencode/blob/v1.18.5/packages/opencode/src/agent/agent.ts),
[request preparation](https://github.com/anomalyco/opencode/blob/v1.18.5/packages/opencode/src/session/llm/request.ts),
[session handling](https://github.com/anomalyco/opencode/blob/v1.18.5/packages/opencode/src/session/session.ts)
and [message/part schema](https://github.com/anomalyco/opencode/blob/v1.18.5/packages/opencode/src/session/message-v2.ts).
Vendor documentation links describe the research basis and may have changed since inspection.

Raw native evidence stays private. These aggregates preserve conclusions and their limitations;
they do not make the historical local controls independently rerunnable from the public tree.
