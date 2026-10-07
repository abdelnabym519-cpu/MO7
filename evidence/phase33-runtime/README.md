# Phase 33 runtime evidence

Copied out of the working and production roots by
`docs-for-user/phase33-cicd/collect_phase33_evidence.sh`, and committed because
this environment recycles: the roots outside the checkout are not preserved, so
anything a reader needs in order to check a claim in the closure report has to
live here.

## What is current

The certification is

| Field | Value |
| --- | --- |
| Release candidate | `1.6.11-f3da23a-ci37558387881` |
| Validated commit | `f3da23aff1d0e62d8636a7f601277636fc45ce0c` |
| CI run | `37558387881` (`RELEASABLE`) |
| Artifact | `deeptutor-1.6.11-py3-none-any.whl` |
| Frozen artifact sha256 | `af1ab3df13b5425dcbced39715fb516c65bb7f602499b7cbee1b878ec134b017` |
| Release tag | `mo7-release-1.6.11-f3da23a-ci37558387881` → `f3da23a` (`host/tag-target.txt`) |
| Live on the host | yes (`host/current-release.json`) |

* `candidates/1.6.11-f3da23a-ci37558387881/` — the candidate ledger and its
  evidence documents: `state.json`, `deployment.json`, `post-deploy.json`,
  `rollback.json`, `tag.json`.
* `candidates/1.6.11-6d7603c-ci37554841809.json` — the candidate that served as
  the fault-injection target (`APPROVED`, never promoted).
* `negative/` — the executed refusals and injections: the five invalid candidates
  (`negative-tests.json`), the four local injections (`faults-local.json`), the
  six host injections (`faults-host.json`), and the two rollback records that
  document defects found by running the path — before the rollback rehearsed its
  own backup (`rollback-before-rehearsal-fix.json`) and before the artifact probe
  survived a refusal-by-close (`rollback-before-probe-reset-fix.json`).
* `host/` — the deployment's own final state: verification, identity, artifact
  probe, websocket probe, the deployment contract, the release marker, the
  deployment ledger, the release tags as the repository holds them and the commit
  the current release tag must name.
* `logs/` — the promotion logs of the certified cycle and of the injected
  failures, plus the browser matrix summary.

## What is historical

* `candidates/1.6.11-45d419a-*.json` and
  `candidates/1.6.11-45d419a-ci37546936253/` describe the earlier certified cycle.
  That release was live until 2026-10-07T21:37Z, when the sandbox was recycled for
  the second time and destroyed the deployment host with it (P33-K4). The records
  are kept because they are the evidence those report rows were written from; the
  release they describe is no longer staged anywhere.
* `negative/faults-host.json` is overwritten by every host-suite run; the copy
  here is the final-state run (`6/6`). Earlier runs are described in the report
  by their output, which is quoted verbatim in the findings table.
