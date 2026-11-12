# Inspecting repeated operations across compaction

This investigation asked whether operations after compaction repeated an earlier lookup, returned
changed information, or lacked comparable saved evidence. It used ordinary Codex and Copilot
sessions without reconstructing requests or running recorded tools.

## Evidence and checks

The selected sources were a Codex `0.166.0` rollout and a Copilot `1.0.107` event log with producer
`copilot-agent`, schema `1`. Independent source review fixed two Codex examples and three Copilot
file-view examples before comparing inspector reports. Earlier mining aggregates were discarded
because their windowing and matching rules had defects; they supply no qualification or prevalence
claim here.

Each selected call had a distinct native identity and a paired completion within its adjacent
compaction epoch. Source review checked exact recorded arguments, successful compaction, the
nearest eligible predecessor and the first eligible occurrence after the boundary. Raw sessions,
paths, arguments, identifiers and captured content remain private. The table retains only line
anchors and result relations needed to explain the findings.

| Example | Call lines before → after | Captured result |
| --- | --- | --- |
| Codex tool-catalog lookup | 11130 → 11555 | Equal `output`; 10,129 canonical UTF-8 bytes on each side |
| Codex agent-status lookup | 2415 → 3816 | Different `output`; 5,877 bytes before and 5,579 after |
| Copilot file view A | 247 → 479 | Equal `content` and equal `detailedContent` |
| Copilot file view B | 346 → 479 | Equal `content` and equal `detailedContent` |
| Copilot file view C | 247 → 502 | Equal `content` and equal `detailedContent` |

The Copilot request records contain multiple calls, so a call line alone is not a unique tool
identity. Their completion lines are respectively 270 → 490, 354 → 493 and 271 → 512; native call
identities were checked independently. Content and detailed display are separate scopes, not
interchangeable encodings of one result. A length difference between them does not prove truncation.

## Use the report

List successful boundaries in an explicit saved source, then select one of the returned IDs:

```bash
ithrl inspect reacquire SESSION --format json
ithrl inspect reacquire SESSION --boundary BOUNDARY_ID --format json
```

Verify `source.sha256`, then use `boundary` and each group's `before`/`after` call lines,
completion lines and record digests to locate the evidence in the private source. Default reports
omit arguments and result text; `--show-text --max-text-bytes 4096` permits bounded, escaped detail.

For the catalog example, the report establishes that the same recorded operation returned the
same captured payload. Manual inspection identified the generic Codex call as a catalog lookup;
the inspector leaves generic code unclassified. A developer can now examine whether the relevant
catalog entry survived in the compaction summary and whether the later task required a fresh
lookup. The report alone cannot answer those questions or justify deleting that operation.

In the status example, matching arguments returned different status, so repetition alone cannot
establish redundancy. Inspect what changed and whether the later task needed a refresh. A captured
status difference does not establish a change in the underlying resource.

The three Copilot pairs establish repeated recorded file views with equal captures in each native
field. A developer can inspect the summary and the later request to determine why the agent opened
those files again. Historical file state was not snapshotted, so equality of captures does not
prove that the underlying files stayed unchanged or that the views were avoidable.

## Qualification and limits

The source-checkout CLI, an installed wheel and a wheel rebuilt from the sdist produced identical
JSON reports for the three selected boundaries. All five examples agreed with the independent
source oracle, including anchors, result digests and canonical UTF-8 sizes. Both wheel origins
contained identical runtime modules. Source bytes, inode, size and modification time remained
unchanged. This qualifies the recorded native profiles, not arbitrary future schema variants.

A separate ordinary Copilot source contained five assistant messages without turn IDs. All five
had sufficient native request/start/completion identities for passive validation. Passive boundary
listing accepted that source, while experimental parsing retained its missing-turn refusal. The
source contained no compaction, so it supplied no additional cross-boundary match evidence.

Sanitized controls cover changed, missing, truncated, empty and status-only captures; failed or
partial compaction; straddling calls; copied history; unknown records; malformed identities;
source changes, symlinks and input/report limits. Qualification corrected empty exec wrappers
being counted as content and aligned Unicode byte counts with the canonical encoding used for
digests. Complete captured fields still do not prove complete tool output or historical resource
state.

During direct CLI checks, Python audit hooks restricted file opens to the selected source,
interpreter/package assets and read-only OS entropy. The hooks were configured to deny network,
subprocess and application-write events; successful runs recorded no denials. Static review and
in-process controls checked the passive call path as well.
This is application-level evidence, not a kernel sandbox or a complete audit of interpreter
startup and native system calls. No harness or recorded action was started, and no provider call
was made. Usage stays unknown; repeated bytes are not token or cost savings.

Normalized and heuristic matching, causal effects of compaction, resume/fork/handoff comparison
and active Codex compaction reconstruction remain outside the qualified scope.
