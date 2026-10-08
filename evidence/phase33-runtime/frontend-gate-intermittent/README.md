# Frontend deterministic gate: intermittent test timing evidence

This directory records the failed gate runs, the local reproductions, and the
controlled instrumentation checks referenced by findings P33-M28 through
P33-M30 in the Phase 33 closure report. It is diagnostic evidence, **not** a
replacement for the certified CI run or the release-candidate evidence.

## Scope and source identity

- The pipeline source tree was certified by run `37700947418` on commit
  `32a7045c5e318dbe6ab8264c96894fc4d9779287`; its CI evidence commit is
  `1d6ab653fac2471f46a898e7ad87dfe32b79e46c`.
- The local Vitest comparisons used a checkout at `1d6ab65`. A direct Git diff
  confirmed `web/**` is byte-identical between `32a7045` and `1d6ab65`.
- The *unpatched* checkout contains no edits to `web/**`. The candidate fix is
  stored separately at
  `docs-for-user/phase33-cicd/frontend-gate-intermittent-failures.patch` and was
  applied only to a disposable local copy for measurement. It is **not** applied
  to the Phase 33 repository tree or release candidate.
- Environment during the durable post-recycle measurements: Node `v22.22.3`,
  2 online CPUs; frontend dependencies installed with `npm ci --legacy-peer-deps`.
  The test suite is 108 files / 427 tests.

## Measurements

All local commands were `npx vitest run --maxWorkers=N` from a Git checkout;
full stdout/stderr for every run is saved as deterministic gzip files below (decompress with `gzip -dc`) and summarized in
`measurement.json`.

| Checkout | Workers | Full-suite result |
| --- | ---: | --- |
| Unpatched | 4 | 3/3 passed, 427/427 each, in the post-recycle repeat. The initial pre-recycle hunt at four workers reproduced the linked-folder race once in three runs, but those `/tmp/vitest-par-*` logs were lost in the subsequent sandbox recycle; the post-recycle repeat is retained here rather than presenting the lost logs as durable evidence. |
| Unpatched | 8 | **0/6 passed**; all six runs failed. Every run had at least one timeout/early query in the timing-sensitive cases, most often `watching-browser` or `co-writer-lazy-notebook`; one also hit the default whole-test timeout in `settings-unified-draft`. This deliberately oversubscribes the 2-CPU sandbox and is a stress reproduction, not the CI worker setting. |
| Patched copy | 4 | **3/3 passed**, 427/427 each. This is the four-worker CI-parallelism replay used for comparison; the workflow invokes `npm run check:fast` and Vitest uses its runner worker pool. |
| Patched copy | 8 | 2/3 passed, 427/427. The remaining run timed out in the unrelated `i18n-audit.spec.ts` test at its own default five-second Vitest timeout. All four patched target suites passed in all six patched full-suite runs. |

The worker-8 outcome is reported in full, including its unrelated timeout; it is
not represented as a completely green stress run. The 4-worker baseline's
post-recycle 3/3 green outcomes likewise do not negate an intermittent result:
the two CI gate failures and the earlier one-in-three reproduction are
independent evidence, and the patch addresses the concrete synchronization /
wait-budget defect shown in the failing DOM and stack traces.

The local patch changes only synchronization and time budgets. The same accessible
element must still appear before the same first button is clicked; the downstream
assertions and tested operations are unchanged. The query waits up to five seconds
instead of Testing Library’s default one second, or the slow settings lifecycle
test receives a 20-second per-test budget instead of the default five seconds. No production
source, security gate, workflow, test expectation, or release policy was changed.
Because the proposed patch changes `web/**`, applying it would create a new
source revision and require a new candidate and full certification cycle under
the source-drift rule. That was intentionally not done in this close-out.

## CI evidence

`ci-runs.json` is generated from each run's own committed `release.json` and
`promotion.json`. It records the certified frontend run `37558387881`, the two
refusals `37690935036` and `37699029751`, and the passing run `37700947418`.
The two red CI runs predate the final instrumentation fixes, so neither has a
stage `failure_tail`; their `promotion.json` correctly names the frontend gate
in `blocking_failures`. In `37700947418`, all stage rows have the new `blocking`
field and the candidate is `RELEASABLE` with no blocking failures.

## Instrumentation probes

`instrument-probe/` contains two controlled fail-closed checks of the now-committed
instruments:

1. `rc_stage.py` runs a command that prints two sentinel lines and exits `7`.
   The document retains the tail, the output carries a bounded `::error`
   annotation, and the wrapper still exits `7` (`verification.json`).
2. `rc_evidence.py` receives a deliberately failing, non-policy stage. It records
   `required: false` *and* `blocking: true`, includes the stage tail, lists that
   stage in `blocking_failures`, and returns a `NOT_RELEASABLE` decision
   (`blocking-schema-verification.json`). Missing synthetic build inputs also
   fail closed; this fixture is clearly not a release candidate.
