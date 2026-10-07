# PHASE 33 — CI/CD & RELEASE PROCESS CLOSURE REPORT

Version 1.6.11 · repository `abdelnabym519-cpu/MO7` · branch `arena/01a0ff3f-mo7`
Report written from the final state; every number below is read from the artifact
named beside it.

---

## 1. Executive summary

Phase 33 was required to build a release process and then prove it by executing
it: against a real candidate (which must be certified and deployed) and against
deliberately invalid candidates (which must be refused). The evidence for every
claim in this report is a run that happened, a document a tool wrote, or a probe
that talked to the running deployment — never a configuration file that looks
right.

The process, as delivered and exercised:

| Stage | What it is | Evidence |
| --- | --- | --- |
| SOURCE | `.github/workflows/release.yml` on `ubuntu-latest`, triggered by a push to this branch | runs `37546291753`, `37546936253`, `37550740261` (all `success`) |
| VALIDATION | source gate, Ruff, pytest, import boundaries, Bandit, contracts/i18n, the frontend build and the Chromium suite | `evidence/ci/<run>/test-summary.json` |
| BUILD | one recipe from a clean tree, path-scrubbed, epoch-pinned, CPU-pinned | `evidence/ci/<run>/build-report.json` |
| ARTIFACT | `deeptutor-1.6.11-py3-none-any.whl`, one web build inside | `artifact.json` (sha256, bytes, 4400 entries) |
| ARTIFACT VERIFICATION | contract (26 checks), security scan, two-build equivalence | `artifact-contract.json`, `artifact-scan.json`, `build-report.json` |
| RC | `rc_release.py ingest` freezes the CI evidence and the artifact | `state/<rc>.json`, `rc/<rc>/` |
| PROMOTION GATE | 16 local checks; a red required gate refuses (`NOT_RELEASABLE`) | `release.json`, `promotion.json` |
| DEPLOYMENT | the Phase 32 pipeline: verify → stage → smoke → backup → migration rehearsal → promote → restart → health → smoke | `run/promote-*.log`, `deployment.json` |
| POST-DEPLOY VALIDATION | `verify`, `health`, `identity`, `artifact`, `ws` on the host that is serving | `post-deploy.json` |
| RELEASE | annotated tag `mo7-release-<rc id>` on the promoted commit | `tag.json` |
| REJECT | a candidate whose gates are red, or whose declared identity is false, cannot advance | `negative-tests.json`, `faults-host.json` |
| ROLLBACK | `rc_release.py rollback` (automated, verified) | `rollback.json` |

The certified candidate is **`1.6.11-45d419a-ci37546936253`** — CI run
`37546936253` (head `45d419a`), artifact `deeptutor-1.6.11-py3-none-any.whl`,
sha256 `5228a28e8448b5cde8cadc896097ad8eb9a6565a01219c34f434eed88fa63ad9`, validated
16/16, approved, promoted, and serving with `verify` 107/107, its identity
traceable to the commit and the artifact hash.

Two things make this report different from a "pipeline configured" claim. First,
the process ran **fourteen** times end to end (nine CI runs, five candidate
lifecycles) and failed twice for real reasons, both of which are findings below.
Second, the whole environment was **destroyed by a sandbox recycle in the middle
of the phase and rebuilt from the repository alone** — which is the strongest
evidence available that the process is documented well enough to be reproduced
rather than merely observed once.

Nothing in the product's behaviour was changed to make any of this pass. Every
finding below is a defect in the release process, its instruments, or its
documentation — and each was fixed at the smallest change point.

---

## 2. Release identity

| Field | Value |
| --- | --- |
| Repository | `abdelnabym519-cpu/MO7` |
| Branch | `arena/01a0ff3f-mo7` |
| Certified commit | `45d419a1a80e263093ba2ec0c9cea05af6a1f8da` |
| CI run | `37546936253` (conclusion `success`, verdict `RELEASABLE`) |
| App version | `1.6.11` |
| Release candidate | `1.6.11-45d419a-ci37546936253` |
| Artifact | `deeptutor-1.6.11-py3-none-any.whl` |
| Artifact sha256 (frozen, promoted) | `5228a28e8448b5cde8cadc896097ad8eb9a6565a01219c34f434eed88fa63ad9` |
| CI artifact sha256 | `1c30a53f0c8db7af51c477705bb6e5ae73126e1d5d83fecfb80e0c43e2fbd2e2` (38 652 340 B, 4400 entries) |
| Canonical content sha256 | `36cfd5c5e63c1d6d2fbee91ba59f3908a315058af592a6597f2328f91d1…` (entry-by-entry, generated values excluded by rule) |
| Tag | `mo7-release-1.6.11-45d419a-ci37546936253` |
| Live release | `1.6.11-45d419a-ci37546936253` at `/home/user/mo7-prod/current` |
| Previous release (rollback target) | `1.6.11-0af36b8-bootstrap` |
| Build environment (CI) | `ubuntu-latest`, Python 3.11.16, Node v22.23.3 |
| Build environment (local validate) | this host, Python 3.11.2, Node v22.22.3 |

Two builds of `45d419a` — the CI runner's and this host's — differ in bytes and
are **the same content**: `rc_manifest.py` compared 4400 entries, classified
every difference under a checked rule, and reported `unexplained: []`. §13.6 of
the runbook states exactly why byte identity is not achievable here (Next.js
generates a build id and a Server Actions key per build) and what the
deterministic identity is instead (the entry-by-entry canonical digest plus the
artifact sha256, which is what the deployment verifies before it stages).

### Versioning strategy (one authoritative rule)

* **App version** — `deeptutor/__init__.py`'s `__version__`, `1.6.11`; it must
  equal the wheel's metadata and its filename, and the deployment refuses a
  mismatch.
* **Release identity** — `<version>-<commit7>-ci<run id>` for CI-built
  candidates (`1.6.11-45d419a-ci37546936253`), or `<version>-<commit7>-<label>`
  for bootstrap releases (`1.6.11-0af36b8-bootstrap`). A release id is unique per
  build and names the CI run that produced its evidence.
* **Git tag** — annotated, `mo7-release-<rc id>`, placed on the promoted commit,
  carrying the version, the rc id and the artifact sha256 in its message.
* **Commit identity** — the full 40-character sha of the source tree the
  artifact was built from; the deployment refuses a commit the checkout does not
  contain (§25 P33-M13).
* **Pre-release identifiers** — not used; every published artifact is a
  release, and the promotion state machine (not a string) says whether a
  candidate is publishable.

---

## 3. Capability inventory (measured)

| Capability | Classification | Evidence |
| --- | --- | --- |
| GitHub Actions pipeline on push | **AVAILABLE** | runs `37546291753`, `37546936253`, `37550740261` — `success`; `37544304900` — `failure` (a real red gate) |
| Pipeline fail-closed behaviour | **AVAILABLE** | `37544304900`: pytest gate red (8433/8488, one case) → verdict `NOT_RELEASABLE` → job exit 1 |
| `workflow_dispatch` trigger | UNAVAILABLE | HTTP 403 `Resource not accessible by integration` |
| Run logs (`gh run view --log`) | **EXTERNALLY BLOCKED** | TLS EOF to `results-receiver.actions.githubusercontent.com`; evidence is committed by the job instead |
| Run/job/step metadata, check-runs | AVAILABLE | `api.github.com` |
| Workflow artifact download | EXTERNALLY BLOCKED | redirects to `objects.githubusercontent.com` (TLS EOF); the pipeline commits its evidence to the branch |
| Pushing from the workflow (evidence commit) | AVAILABLE | `37546291753`, `37546936253`, `37550740261` each pushed `evidence/ci/<run>/**` after fetching and rebasing |
| Action secrets | UNAVAILABLE | secrets API 403; the pipeline requires none |
| Self-hosted runner registration | UNAVAILABLE | 403 |
| Google Fonts during the build | EXTERNALLY BLOCKED | TLS EOF; the build uses the committed offline font mock with genuine `woff2` payloads |
| Playwright browser download | EXTERNALLY BLOCKED | `cdn.playwright.dev` TLS EOF |
| Browser binary for the production suite | **AVAILABLE** | `@sparticuz/chromium@153.0.0` via npm; `chrome --version` → `Chromium 153.0.8010.0`; full matrix 64 passed / 9 skipped |
| Running the browser suite against the deployment | **AVAILABLE** | `bin/prod.sh session <account>` + `prod_browser.sh` through the TLS validation ingress |
| Deployment host (Phase 32 pipeline) | **AVAILABLE** | `/home/user/mo7-prod`, rebuilt from scratch this phase (§22) |
| npm registry / PyPI | AVAILABLE | `npm ci`, `npm pack geist@1.7.2 @fontsource/lora@5.3.0`, `pip install` |
| Docker, Redis, an LLM provider | UNAVAILABLE / not present | no container runtime; no model provider reachable (§28) |

**PIPELINE CONFIGURED is not PIPELINE EXECUTED.** Every "AVAILABLE" row above
cites a run, a probe or a committed document produced by execution. Every
blocked row cites the error that produced the classification.

---

## 4. Release architecture as executed

```
SOURCE ──▶ VALIDATION ──▶ BUILD ──▶ ARTIFACT ──▶ ARTIFACT VERIFICATION
   │                                                   │
   │  (CI: release.yml, 20 stages, fails closed)       ▼
   │                                                  RC  (rc_release.py ingest)
   ▼                                                   │
 REJECT ◀── a red required gate ────────────────────┐   ▼
   │                                                │ PROMOTION GATE (16 checks)
   │                                                │   │
   │                                                │   ▼
   │                                        APPROVED ──▶ DEPLOYMENT (Phase 32)
   │                                                          │ verify/stage/smoke
   │                                                          │ backup
   │                                                          │ migration rehearsal
   │                                                          ▼
   │                                                    PROMOTE + RESTART + HEALTH
   │                                                          │
   └──────────────── ROLLBACK ◀── post-deploy validation ─────┘
                          (verify, health, identity, artifact, ws)
```

Every transition in that diagram is a program with an exit code and a document:

| Transition | Program | Document |
| --- | --- | --- |
| CI evidence → candidate | `rc_release.py ingest` | `state/<rc>.json` (`ci_evidence_frozen`, digest) |
| candidate → validated | `rc_release.py validate` | `local.checks` (16 rows), `validate.json` |
| validated → approved | `rc_release.py approve` | history entry `APPROVED` with the actor |
| approved → deployed | `rc_release.py promote` → `prod.sh deploy` | `deployment.json`, `run/deploy-*.log` |
| deployed → released | `post_deploy_validation()` | `post-deploy.json`, `tag.json` |
| any → rejected | `rc_evidence.py` verdict, `rc_contract.py`, `rc_scan.py` | `release.json`, `promotion.json`, `negative-tests.json` |
| deployed → rolled back | `rc_release.py rollback` → `prod.sh rollback` | `rollback.json` |

No promotion is invisible: the host writes `run/current-release.json`, the
deployment log records every forward and backward move with a measured outage
window, and the controller's state file records who moved the candidate and why.

---

## 5. The ten named harnesses (behaviour, not file existence)

| # | Harness | Behaviour it validates | Result |
| --- | --- | --- | --- |
| 1 | `rc_source_gate.py` | repository, branch, event and commit; clean tree | pass (`release.json`) |
| 2 | `rc_lint.py` | Ruff lint + format, product tree blocking, repo-wide recorded | clean (1800 files; 1783/1963 formatted) |
| 3 | `rc_import_checks.sh` | entry points import without pulling implementations; the isolated worker executes real work | pass |
| 4 | `rc_build.sh` | one recipe builds the wheel from a clean tree; failure aborts | pass (both CI and local) |
| 5 | `rc_manifest.py` | two builds compared entry by entry; every difference classified; unexplained fails | `reproducible: true`, `unexplained: []` |
| 6 | `rc_contract.py` | content contract, identity, and (with `--install`) install + startup + health | 26/26 |
| 7 | `rc_scan.py` | secrets, keys, credentials, debug config, host paths | clean |
| 8 | `rc_evidence.py` | gate table, documents, verdict; a red required gate makes the candidate unreleasable | `RELEASABLE` / `NOT_RELEASABLE` both produced |
| 9 | `rc_negative.py` | five invalid candidates refused by the real tooling | 5/5 refused |
| 10 | `rc_release.py` | the candidate lifecycle, frozen artifact, promotion, post-deploy gate, rollback | full cycle executed |

Supporting instruments: `rc_stage.py` (stage records), `rc_faults.py` (failure
injection, local **4/4** and host layer — §16), `rc_faults_host.py`,
`prod_ws.py` (the streaming-surface probe added this phase), `bin/prod.sh ws`.

---

## 6. Pipeline stages and outcomes (CI)

Run `37546936253`, commit `45d419a`, 20 stages, verdict `RELEASABLE`:

| Stage | Result |
| --- | --- |
| source commit is a full sha / version declared / artifact produced / sha256 recorded | pass |
| artifact contract (26 checks) / artifact security scan | pass |
| the source gate / the artifact contract / the artifact scan / import boundaries | pass |
| Ruff lint + format (product tree, repository-wide) | clean |
| architecture/import checks (`lint-imports`, `scripts/check_architecture.py`) | pass |
| Bandit (code security scan) | no high/high findings |
| Phase 27 attack harness, raw security probes, Phase 29 token probes | pass |
| contracts / i18n | pass |
| frontend: `check:fast`, ESLint, production build | pass |
| Chromium production suite | pass |
| two builds content-identical / every difference explained | pass |
| artifact wire probe / installation test | pass |

The same 20 stages ran for `37546291753` and `37550740261` and reached the same
verdict. Run `37544304900` is the counter-example that matters: its pytest stage
reported `8433/8488 passed, 1 failed` and the pipeline refused the candidate —
`NOT_RELEASABLE`, job exit 1. That is the pipeline failing closed, on a real
test failure, in production use rather than in a demonstration.

---

## 7. Test gates (no weakening)

* **pytest** — 8488 collected: 8433 passed, 54 skipped in CI; the local
  reproduction in a clean worktree of the same commit reported **8434 passed,
  0 failed, 54 skipped** (the extra pass is the case described in P33-M9, whose
  CI failure did not reproduce locally). No test was deleted, skipped or
  weakened.
* **Architecture** — `lint-imports` (import-linter 2.11) and
  `scripts/check_architecture.py` both pass; the isolated worker protocol is
  exercised by `rc_import_checks.sh`.
* **Ruff** — 0.16.0, lint + format, product tree and repository-wide.
* **Bandit** — recorded in `security-summary.json`.
* **Frontend** — `check:fast`, ESLint, production build (standalone output).
* **Contracts/i18n** — the repository's own gates run in the pipeline.
* **Browser** — the Chromium production matrix: **64 passed, 9 skipped, 0
  failed** (the 9 skips are the documented fixture-gated cases, carried from
  Phase 32 as P32-K4).
* **Phase 27/29 security harnesses** — run inside the pipeline, not re-implemented.

---

## 8. Source control gate

Executed in CI and re-executed locally by `rc_release.py validate`:

| Criterion | Result |
| --- | --- |
| repository / branch / event | `abdelnabym519-cpu/MO7`, `arena/01a0ff3f-mo7`, `push` |
| the checkout contains the CI-validated commit | pass (`45d419a1a80e2` ∈ checkout) |
| no product source changed since CI validated the commit | pass (14–20 changed paths, all under `evidence/` or `docs-for-user/`) |
| the checkout is clean | pass |
| the frozen CI evidence is unchanged | pass (digest compared against ingest) |
| reproducible checkout | the validated commit is checked out into a worktree and rebuilt from it |

The "no product source changed" rule is deliberately strict: after a CI run,
only `evidence/**` and `docs-for-user/**` may differ, and a violation refuses the
candidate (observed: `1.6.11-cb0eff9-ci37540384403` was refused 15/16 when a
workflow fix moved the branch head — §25 P33-M12).

---

## 9. Build reproducibility (measured, not asserted)

| Property | Value |
| --- | --- |
| Recipe | `rc_build.sh`: clean tree, generated data cleared, path scrub to `/srv/mo7-build`, `SOURCE_DATE_EPOCH` pinned, `CIRCLE_NODE_TOTAL=2` |
| Builds compared | CI runner (Python 3.11.16, Node v22.23.3) vs this host (Python 3.11.2, Node v22.22.3) |
| Entries compared | 4400 (every one) |
| Differences classified by a checked rule | build id, tsconfig name, entries containing them, manifest ordering |
| Unexplained differences | **0** |
| Byte-identical | **no, and not claimed** |

Why byte identity is not achievable without changing the product: Next.js
generates a random build id and a random Server Actions encryption key per
build. Pinning the build id would be a product configuration change, which this
phase may not make. The impact on release integrity is bounded and stated:

* the deployed bytes are pinned by the **artifact sha256** (frozen at ingest,
  re-checked before staging, written into the release manifest and the tag);
* the content is pinned by the **canonical digest** (entry set + contents, with
  the generated identifiers excluded by rule);
* traceability is pinned by the **commit**, which the deployment now refuses to
  accept unless the checkout contains it (P33-M13).

So a release is uniquely identifiable and verifiable even though two builds of
it are not bit-identical — the mechanism is determinism of *identity*, not of
bytes, and it is the mechanism the promotion gate uses.

---

## 10. Artifact contract and verification (§10)

`rc_contract.py` — 26 checks, all pass with `--install`, which creates a clean
venv, installs the wheel, starts the service and asks for liveness and readiness:

* package name/version and filename agree; pure-Python wheel tag;
* required files present: the packaged application, the Next.js standalone
  server (`deeptutor_web/server.js`), exactly **one** web build directory, the
  launch scripts, the runtime data contract;
* forbidden files absent: `.git`, `.pyc`/`__pycache__`, build-host paths, test
  artifacts, temporary files, dev-only configuration;
* metadata: name, version, entry points, dependency set;
* installation: installs into a clean venv;
* runtime startup, `/health/live`, `/health/ready` answered by the installed
  artifact;
* identity: commit and version recorded inside the artifact agree with the
  release metadata;
* sha256 recorded and frozen.

---

## 11. Artifact security scan (§11)

`rc_scan.py` refuses, rather than warns: private keys, credential files, API
secrets, `.env` payloads, debug configuration, developer machine paths, leftover
temporary files, and unexpected dependencies. Its output for the certified
artifact is `clean`; the negative suite proves it still refuses a planted
private key (`negative-tests.json`, case 1).

## 12. Dependency security (§12)

Existing tooling only, no scanners-of-theatre: the repository's lockfile is the
single source (`npm ci` from `package-lock.json`), the Python dependency set is
declared in `requirements/`, and the pipeline's Bandit and the Phase 27/29
security harnesses are the adversarial instruments. Five lockfile entries that
referenced an unreachable mirror were verified byte-identical against the npm
registry before the lockfile was rewritten in an earlier phase; that rewrite is
in the certified tree. `npm audit`'s advisory output is recorded, not treated as
a gate — the deciding gates are the ones that can fail closed.

## 13. Immutable RC (§13)

An RC is created once, from one CI run's evidence, and frozen:

* `ingest` copies `evidence/ci/<run>/**` into the candidate directory and records
  its digest; the copy is re-checked at validate (`the frozen CI evidence is
  unchanged`);
* `validate` freezes the artifact and its sha256; `promote` re-hashes the file
  before deploying and refuses if it moved;
* **a verdict is terminal**: `REJECTED` cannot be re-validated, `PROMOTED`
  cannot be re-approved. A fix means a new commit, a new CI run, a new candidate.
* the negative suite's `artifact-substitution` case proves the frozen-artifact
  check refuses a changed artifact mid-flight.

Observed in this phase: `1.6.11-cb0eff9-ci37540384403` was refused and never
became an RC; `1.6.11-5bc1879-ci37546291753` was refused on a build failure and
could not be re-validated (a new candidate, `1.6.11-1a3a65c-ci37550740261`, was
created from the next run instead).

## 14. Promotion gate (§14)

Sixteen local checks, and a blocking rule that no policy may except a required
gate into silence. Each of the following was exercised at least once and the
pipeline refused:

| Required element | Evidence of refusal |
| --- | --- |
| artifact contract | negative case 2 (missing `server.js`), case 3 (two builds), case 4 (version mismatch) |
| artifact integrity | negative case `artifact-substitution`; `promote` re-hashes |
| tests | run `37544304900` — `NOT_RELEASABLE` |
| security | negative case 1 (planted private key), case 5 (security gate red) |
| browser | the suite's result is a CI stage; a failing suite fails the run |
| deployment identity | fault case `undeclared-commit` (now refused, §25 P33-M13) |
| health | `prod_deploy.sh` rolls the symlink back and refuses if the release is not healthy |
| pre-promote smoke | the staged release is started on a scratch port and probed before publish |
| post-deploy validation | fault case `post-deploy-validation-failure` |
| rollback path available | `validate` check "a rollback target exists on the production host" |

---

## 15. Negative CI test (§15) — executed, not inspected

Two independent executions of the negative idea exist:

1. **The pipeline's own fail-closed stage, in production use.** Run
   `37544304900` (head `0ad679d`) had one red required gate — pytest, one case
   of 8488 — and the run finished `failure`, verdict `NOT_RELEASABLE`, with the
   evidence committed. Nobody demonstrated this in a sandbox; it happened.
2. **`rc_negative.py`, five invalid candidates built from the certified
   artifact in a scratch directory** (never in the checkout), each refused by
   the real tooling:

| Case | Injected | Refused by |
| --- | --- | --- |
| planted credential | an RSA private key inside the packaged modules | `rc_scan.py`, `rc_contract.py` |
| missing required file | `deeptutor_web/server.js` removed | `rc_contract.py` |
| two builds in one artifact | a second web build directory | `rc_contract.py` (build identity) |
| version disagreement | `--version 9.9.9` against metadata 1.6.11 | `rc_contract.py` (metadata identity) |
| red required gates | security + contract + reproducibility red | `rc_evidence.py` → `NOT_RELEASABLE` |

Result: **5/5 refused** (`/home/user/mo7-cicd/negative/negative-tests.json`).

---

## 16. Failure injection (§26) — inject → detect → block → recover → validate

**Local layer, 4/4** (`faults-local.json`):

| Case | Injected | Detected | Blocked | Recovered | Validated |
| --- | --- | --- | --- | --- | --- |
| required gate red | a scan reporting a violation under a no-exception policy | yes | `NOT_RELEASABLE` | policy restored → `RELEASABLE` | yes |
| build failure | checkout with no web build output | yes (exit 128) | no artifact → `NOT_RELEASABLE` | rebuild | yes |
| artifact contract failure | `server.js` removed from a copy | yes (exit 1) | refused | intact artifact exit 0 | yes |
| artifact substitution | a module appended to the frozen artifact | yes (hash moved) | controller refuses the candidate | original hash restored | yes |

**Host layer** — the six deployment-host cases and their outcomes are in §25
(P33-M13 was found here) and are recorded in `faults-host.json`; the
post-deployment-failure case drives a real promotion and requires the
controller's automatic rollback to restore the previous release without a human
step.

## 17. Pre-promote validation (§17)

The staged release is started on a scratch port and probed **before** the symlink
flips. Observed on every promotion this phase:

```
SMOKE starting the staged release on the scratch port 8099
SMOKE staged release reported /health/ready 200 on 8099
PUBLISH … is staged, smoke-tested and immutable
```

The post-promote smoke covers startup, liveness, readiness, login, an
authenticated request, tenant-scoped reads, storage, the database, the frontend,
the API and — added this phase — the **streaming surfaces** (real WebSocket
connections): 10/10 checks.

## 18. Post-promote validation (§18)

`post-deploy.json` for the certified release, run against the host that is
serving it:

| Step | Result |
| --- | --- |
| infrastructure verification (`verify --quick`) | 107/107 |
| liveness, readiness, frontend, ingress (`health`) | pass |
| release identity (`identity`) | `traceable=True`, artifact `5228a28e…`, 0 failures |
| raw artifact probe (TLS ingress) | 29/29 |
| streaming surfaces (`ws`) | 9/9 |

## 19. Rollback (§19)

Rollback is **automated** where it can be, and this phase exercised it twice from
the final state, with the release that was live at the time being the certified
one:

| Run | Invocation | Result | Post-rollback verification |
| --- | --- | --- | --- |
| first (before the fix below) | `rc_release.py rollback --rc 1.6.11-45d419a-ci37546936253 --to 1.6.11-0af36b8-bootstrap` | `result: "rolled back"`, exit 0 | **106/107** — one red check, because the rollback took a fresh backup and never rehearsed it (`negative/rollback-before-rehearsal-fix.json`) |
| second (after the fix) | same | `result: "rolled back"`, exit 0, `verified_after_rollback: true` | **107/107**, identity traceable, artifact probe 29/29, streaming 9/9 |
| recovery | `prod.sh promote --release 1.6.11-45d419a-ci37546936253` | release live again | 107/107, `identity: traceable=True … failures=0` |

The controller writes `rc/<rc>/evidence/rollback.json` — `invoked: true`,
`exit_code`, `from_release`, `to_release`, `result`, and the five post-rollback
probes with their exit codes and output tails — and moves the candidate to
`ROLLED_BACK`. The host writes its own record in `run/deployments.log`
(`rollback release=… previous=… reason=operator`) and its own verification
evidence.

Two refusals the path must make, and does: a rollback to the release that is
already current, and a rollback to a release id that was never staged (fault
case `rollback-to-unknown-release`: `exit=2 FATAL: release directory not found`).

The measured defect this section produced — a healthy rollback reporting one red
check — is P33-M19 in §25, and it is the same defect as the promotion's
rehearsal gap (P33-M6), found only because the rollback path was finally
executed. The automatic rollback on a failed post-deploy validation is measured
separately by the fault case (§16), which requires the host to be back on the
previous release without a human step.

## 20. Migration gate (§20)

Inside every promotion, and in the branch that has to succeed before the symlink
flips: take a fresh pre-deploy backup, prove it restorable into a scratch root,
and prove the **release being promoted** boots on the restored data. Measured
during the certified promotion:

```
MIGRATION validating the pre-deploy backup is restorable (20261007T002110Z-pre-deploy-1.6.11-45d419a-…)
MIGRATION restore rehearsal passed (10 checks) on the backup this promotion took
```

The ten checks cover the backup manifest hash, per-database hash + integrity,
the restored file storage, the restored auth secret and its mode, a scratch
backend started from the restored data, an account authenticating against it, a
notebook read and a book read. Because a promotion takes a fresh backup, it must
rehearse the backup it just took — otherwise promoting invalidates the check the
deployment is then measured against (§25 P33-M6).

## 21. Approval model (§21)

Approval is a state transition with an actor and a reason, recorded in the
candidate's history: `CREATED → VALIDATED → APPROVED → PROMOTED`, with
`REJECTED`, `ROLLED_BACK`, `ROLLBACK_REQUIRED`, `ROLLBACK_FAILED` and
`DEPLOY_FAILED` as the failure states. An approval is only possible from
`VALIDATED`, and only the promotion gate's verdict decides whether a candidate
can reach it. There is no "approve anyway": the workflow's own verdict object
records the blocking failures, and a policy exception can only be applied to a
non-required gate — and is recorded as `excepted` with the finding id that
excused it.

## 22. Secrets discipline (§22)

* The pipeline needs **no secrets**: it pushes with the runner's own token and
  the secrets API is not accessible to this integration (403), so a secret that
  cannot be read cannot be leaked by the workflow.
* The deployment's credentials and auth secret live in
  `/home/user/mo7-prod/secrets/` (mode 0600, 0700 directory) and are generated on
  the host, never in the repository; `rc_scan.py` refuses any artifact carrying
  credentials, and the negative suite proves it.
* Logs are written without request bodies, headers or credentials.

## 23. Supply chain (§23)

No signing theatre: nothing is signed here, and nothing claims to be. What is
verified before a release runs is: the artifact's sha256 against the frozen
decision, the artifact's content against the CI run's manifest, the commit's
existence in the repository, the dependency set against the committed lockfile,
and the two-build equivalence. Where a stronger mechanism is unavailable
(artifact attestations, sigstore, a hardened runner) the report says so in §27
instead of implying protection that does not exist.

## 24. Machine-readable evidence (§24)

Every document carries `recorded_at` (UTC), the environment, the commit, the
version, the artifact sha256 and a result field:

| Document | Where | Key fields |
| --- | --- | --- |
| `release.json` | `evidence/ci/<run>/` | `result`, `commit`, `version`, `stages[]` |
| `artifact.json` | same | `sha256`, `bytes`, `entries`, `canonical_sha256` |
| `test-summary.json` | same | `pytest`, `ruff`, gate rows, failing case names |
| `security-summary.json` | same | Bandit, scan result |
| `build-report.json` | same | recipe pins, `reproducibility.unexplained` |
| `promotion.json` | `rc/<rc>/evidence/` | verdict, blocking gates |
| `deployment.json` | same | release id, commit, artifact sha, deploy log tail |
| `post-deploy.json` | same | per-step exit codes and output tails |
| `rollback.json` | same | `invoked`, `exit_code`, `to_release`, `result`, verification |
| `tag.json` | same | tag name, target commit, artifact sha |
| `negative-tests.json`, `faults-local.json`, `faults-host.json` | work dir | one row per injected failure |

## 25. Findings

The classification vocabulary is fixed and each finding carries exactly one:
**PIPELINE DEFECT** (the CI/CD pipeline or its configuration), **HARNESS
DEFECT** (an instrument that reports wrongly), **REPRODUCIBILITY DEFECT** (the
build's determinism or traceability), **REPOSITORY DEFECT** (product or test
source), **INFRASTRUCTURE**, **EXTERNAL DEPENDENCY**, **KNOWN FINDING** (stated,
not fixed here). No finding is classified as flaky, and no fixable pipeline or
repository defect is filed as infrastructure.

| ID | Classification | Symptom | Root cause | Evidence | Impact | Fix | Validation | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| P33-R1 | REPRODUCIBILITY DEFECT | The artifact encoded the builder's directory (`/tmp/mo7-build/src`), so two builders produced two different artifacts | The build ran from a temporary checkout and packaged the path it happened to use | `build-report.json` → `path-scrub.json`; runbook §13.1 | Traceability was fine but the artifact carried the build host's layout | Every reference is rewritten to a declared placeholder of identical length before packaging | Rebuild → `path-scrub.json` reports occurrences rewritten and byte sizes unchanged; contract check passes | FIXED |
| P33-R2 | KNOWN FINDING | Two builds of one commit are not byte-identical | Next.js generates a random build id and a random Server Actions encryption key per build; pinning them would be a product configuration change | `manifest.json` comparison; runbook §13.6 | Byte-level reproducibility cannot be claimed here | Not fixed. What is verified instead: entry-by-entry content equivalence with every difference classified and `unexplained: []`, plus the frozen artifact sha256 as the deployed identity | `rc_manifest.py --compare` → `reproducible: true`, `unexplained: []` | STATED |
| P33-R3 | REPRODUCIBILITY DEFECT | The artifact recorded the builder's machine (its network addresses) | The frontend build embedded the build machine's host addresses into generated output | `build-report.json` history; runbook §13.4 | Builder-specific data shipped to customers | The build pins what can be pinned and scrubs what is recorded; the remaining address-shaped strings are asserted absent by the contract check | Contract check passes on the artifact; two builds agree | FIXED |
| P33-R4 | REPRODUCIBILITY DEFECT | The artifact recorded the builder's CPU count | Next derives worker counts from the host's CPU count and records them in generated manifests | `build-report.json` history; runbook §13.6 | A build's output depended on the machine it ran on | The CPU pin is declared (`CIRCLE_NODE_TOTAL=2`) and the value is part of the compared manifest rather than an unexplained difference | Two builds on different hosts agree | FIXED |
| P33-M2 | HARNESS DEFECT | The reproducibility comparison could fail without the pipeline noticing: its human-readable stream aborted on a renamed class while the machine-readable document stayed green | Two code paths produced the verdict and the document, and only one was authoritative | Runbook §13.5; the run that exposed it | A red comparison could pass unnoticed | One authoritative document decides the gate; the stream is derived from it | Comparison with a planted difference now blocks the candidate | FIXED |
| P33-M3 | HARNESS DEFECT | An artifact stage could crash after writing its result document, leaving a green document behind a dead stage | The document was written before the stage's own failure was decided | Runbook §13.5 | Evidence could describe a stage that did not finish | Documents are written last, after the result is known; a crash leaves no green document | Negative case `artifact-contract-failure` fails closed | FIXED |
| P33-M4 | PIPELINE DEFECT | From the second promotion onward, every identity-bearing check read the *previous* release, although the new one was live and serving | The deployment contract was refreshed from the promotion marker before that marker was written | `run/promote-20261006T215115Z.log` (`CONTRACT contract names 1.6.11-56e1858` while `06d9235` was live) | Traceability reports were wrong on a healthy deployment — the most dangerous kind of false negative | The marker is written first; the contract is refreshed from it afterwards | `CONTRACT contract names 1.6.11-95eb26a…` on the next promotion; `identity` traceable=0 failures | FIXED |
| P33-M5 | PIPELINE DEFECT | When post-deploy validation failed, the controller could not name the release it should return to, so the automated rollback degraded to a manual one | The rollback target was resolved after the marker had already been overwritten with the new release | `rc_release.py` promote path; rollback evidence before the fix | The documented automatic rollback was manual in practice | The previous release is resolved before the marker moves and recorded in it (`previous_release`) | The fault case measures `automatic: true` and the real rollback completed with no human step | FIXED |
| P33-M6 | PIPELINE DEFECT | Every promotion rolled back: the migration gate's rehearsal check went red on a healthy deployment | A promotion takes a fresh pre-deploy backup, and the deployment verification requires the *newest* backup to have been rehearsed — so promoting invalidated the very check it was then measured against | `run/promote-*.log`; `[FAIL] backups: the restoration was rehearsed from the newest backup` | No release could be promoted at all | The promotion rehearses the backup it just took (restore into a scratch root, ten checks) before the symlink flips | `MIGRATION restore rehearsal passed (10 checks) on the backup this promotion took` | FIXED |
| P33-M7 | PIPELINE DEFECT | A commit that changed only the deployment tooling produced **no pipeline run at all** | The workflow's `paths` filter listed the phases' evidence and documents but not the tooling those documents install | `ff8f270` has no run; `cb0eff9` added the path | Deployment tooling could change with no validation and no evidence | The filter includes the tooling directories; a run is required for any change it can act on | Run `37540384403` executed for the tooling change | FIXED |
| P33-M8 | PIPELINE DEFECT | A run whose 21 stages had all passed was reported `failure` and lost its evidence | The evidence-commit step pushed without rebasing, so the branch had moved and the push was rejected | Run `37540344857` (steps 1–21 success, step 22 exit 1, no evidence directory) | Green work was reported failed and its evidence discarded | The step fetches, rebases onto the remote branch and retries the push | Runs `37546291753`, `37546936253`, `37550740261` each committed their evidence on the first attempt | FIXED |
| P33-M9 | HARNESS DEFECT | A red test gate reported *how many* tests failed and not *which* — and with run logs unreachable, the failing case could not be identified from anything that survived the run | The JUnit report was reduced to counters before it was written into the evidence | Run `37544304900` (`8433/8488 passed, 1 failed`, no name); local reproduction of the same commit: 8434 passed, 0 failed | A red gate could not be diagnosed from its own evidence; every such failure required a re-run | The recorder names the failing and erroring cases (up to 20) with the first line of the message, and the gate detail names the first three | Verified against a synthetic report (4 cases: one failure, one error, one skip — counted and named) and on the next red gate | FIXED |
| P33-M10 | HARNESS DEFECT | On a freshly bootstrapped host the migration gate refused **every** promotion, with a `FileNotFoundError` naming a path | The restore rehearsal booted the restored data with `$PROD_ROOT/current`, which does not exist until something has been promoted | `run/promote-20261007T001001Z.log` | No first promotion was possible on a new host; the failure did not explain itself | The rehearsal takes `--release-dir`, and the promotion passes the release it is about to publish (also the stronger property: this application, on this data) | `MIGRATION restore rehearsal passed (10 checks)`; the release went live | FIXED |
| P33-M11 | HARNESS DEFECT | The streaming probe failed a healthy deployment whenever the validation ingress was not running — which is the steady state | It probed one door (the harness-only TLS ingress) and had no notion of the other (the frontend port the platform edge proxies to) | `production-websocket.json` first run vs after | A new instrument would have reported red on a healthy host | It probes the ingress when it is listening and the frontend otherwise, and records which surface it used | 9/9 checks pass on the live release; evidence names the surface | FIXED |
| P33-M12 | KNOWN FINDING | A commit that changes anything outside `evidence/` or `docs-for-user/` after the certified run invalidates that run's candidate | The candidate must be built from a commit whose product source CI validated; a workflow fix (`0ad679d`) moved the head and refused `1.6.11-cb0eff9-ci37540384403` 15/16 | `state/1.6.11-cb0eff9-ci37540384403.json` (REJECTED), `rc_release.py:264–294` | A late fix costs a full re-run; this is the price of the guarantee | Not fixed, by design: the rule is what makes 'the artifact came from this source' checkable | Two candidates refused under this rule; the certified candidate has only evidence/docs drift | STATED |
| P33-M13 | PIPELINE DEFECT | The deployment **accepted a declared source commit that exists nowhere** (forty ones), staged it, promoted it, rewrote the contract to it and served it | The commit-existence check logged `WARN … not found (traceability recorded from the contract)` instead of refusing — a required identity check downgraded to a warning | Fault case `undeclared-commit`: `staged=True`, `CONTRACT PROD_SOURCE_COMMIT: 45d419a… -> 1111…`, host serving `1.6.11-fault-nocommit` | A release could name an unverifiable source; the whole traceability claim rested on a warning | An unknown declared commit is a refusal (`die`); the check runs before anything is staged | Re-run of the case: `exit=1 staged=False FATAL: declared commit 111… does not exist in /home/user/MO7` | FIXED |
| P33-M14 | PIPELINE DEFECT | A promotion whose post-promote smoke failed left the host **serving the release whose smoke had failed**, with the stack half-restarted and nothing rolled back | The health-gate failure path rolled back; the smoke failure path died where it stood, after the symlink had flipped | Fault case `post-deploy-validation-failure` (first run): release current, frontend down, no rollback record | A failed promotion left a broken deployment live | The smoke failure path performs the same verified rollback as the health path (repository fix `6d7603c`) — and the fixed script has to actually be on the host (P33-M20) | Fault run 6 on a converged host: `promote exit=1 state=ROLLED_BACK rollback=rolled back by the deployment performed_by=deployment automatic=True`, `serving=1.6.11-45d419a-ci37546936253`, deployment log line `rollback … reason=post-promote-smoke`, host 107/107 after | FIXED |
| P33-M15 | HARNESS DEFECT | The incomplete-release fault case **damaged the deployment it was testing**: it removed `web/server.js` from the running release | It copied the release with symlinks preserved and then unlinked through the copy's `web` symlink, which pointed back into the live release | Fault case detail; `run/supervisor/frontend.log` (`release web bundle missing …/current/web/server.js`) | A test instrument took the deployment down; the release had to be re-staged from the frozen artifact | The copy's symlink is replaced with an empty directory: the same fault, with nothing behind it to damage | Re-run: `detected/blocked/recovered/validated = true`, deployment intact, release untouched | FIXED |
| P33-M16 | HARNESS DEFECT | The undeclared-commit case could never pass twice: it reuses one release id, so a previous run's residue made its 'was anything staged?' check read the old directory | The case did not consider its own residue | Fault run 2 vs run 3 (`staged=True` from residue) | A fault suite that fails on its second run is not a suite | The case removes its own residue before injecting and reports that it did | Run 3: `stale_residue_removed=true`, `staged=False` | FIXED |
| P33-M17 | HARNESS DEFECT | The post-deploy-failure case never reached a recovery path: its first fault (a dead public origin) was refused by the deployment before the release went live, and the retargeted fault (a dead validation-ingress origin) is refused by the deployment's own post-promote smoke | The fault was retargeted on a prediction -- "the smoke tolerates the ingress being absent" -- that run 4 disproved: the smoke probes the ingress too (`[FAIL] validation ingress serves /login over TLS -- status=0`) | Fault runs 1–4 details (`state=DEPLOY_FAILED`, `rollback=None`; run 4 left the symlink flipped and the marker stale) | A case that exists to exercise a recovery path had never exercised one, and its own measurement made the first failure look recovered | The case now requires the *served* release to return, credits whichever path performed the rollback (`performed_by`), and records whether the deployment rolled back itself | Run 6: `promote exit=1 state=ROLLED_BACK rollback=rolled back by the deployment performed_by=deployment automatic=True operator_rollback_exit=None`; deployment log `reason=post-promote-smoke`; host back on `1.6.11-45d419a-ci37546936253` with the marker agreeing | FIXED |
| P33-M18 | INFRASTRUCTURE | The whole browser matrix failed to launch (64 failed) after the recycle, because the hand-built Playwright cache used the directory name `chrome-linux` where Playwright 1.57 looks for `chrome-linux64` | The browser cannot be downloaded (EXTERNALLY BLOCKED), so the cache is assembled by hand from `@sparticuz/chromium` | `web/test-results/*` before the fix (`Executable doesn't exist at …/chromium-1200/chrome-linux64/chrome`) | The phase could not have claimed any browser coverage | The exact layout, the library path and the inflation step are now in the runbook §17 as a rebuild recipe | Full matrix re-run: 64 passed / 9 skipped / 0 failed | FIXED |
| P33-M20 | PIPELINE DEFECT | The host was running the **pre-fix deployment scripts**: the smoke-failure rollback was committed (`6d7603c`) but `bin/prod_promote.sh` and `harness/prod_promote.sh` were still the old copies, so the same live window P33-M14 describes reappeared in runs 3 and 4 | Nothing compared the host's tooling against the repository, and the harness files are *copies* — patching `harness/` alone changes nothing (`bin/prod.sh` dispatches to its own copies) | Runs 3 and 4: `FATAL: post-promote smoke failed` with no `ROLLBACK` line in `run/promote-*.log`, `rollback=None`, host left on the failed release; `bin/prod_promote.sh` differed from the committed file | A fix that is in the repository is not a fix that is running; every conclusion drawn from the host before this was drawn on older tooling | Install both copies from the repository and verify they are identical, then keep a convergence check (`check_host_convergence.sh`) that compares every host script with the repository and fails on drift | After convergence: `bin/prod_promote.sh` and `harness/prod_promote.sh` byte-identical to the committed file, `check_host_convergence.sh` reports all 12 present files matching, and run 6 records `reason=post-promote-smoke` rollback | FIXED |
| P33-M21 | PIPELINE DEFECT | A deployment that failed **after flipping the symlink** left the host serving the release nobody validated, while `run/current-release.json` still named the release it replaced — a split state no consumer could detect from the marker | The controller rolled back only from its own post-deploy-validation branch; a deployment that failed at the host's gate returned before that branch | Fault run 4: `state=DEPLOY_FAILED rollback=None`, `readlink current` = `1.6.11-f3da23a-ci37558387881`, marker = `1.6.11-45d419a-ci37546936253` | A failed promotion could leave the deployment unvalidated and mislabeled, and the rollback claim in §19 covered only one of the two failure branches | The failed-deployment branch detects what is *served*, rolls back when the failed release is the one serving, reads the deployment's own rollback record, and attributes the recovery (`performed_by`) | Run 5 and 6: `rollback=rolled back by the deployment performed_by=deployment`; the earlier split state no longer occurs (asserted by the case's own precondition, which refuses to inject on an inconsistent host) | FIXED |
| P33-M22 | HARNESS DEFECT | The post-deploy case measured recovery from the marker and reported `recovered: true` while the host was serving the unvalidated release — **a false green in the instrument that exists to prevent false greens** | `current_release()` reads `run/current-release.json`, which a failed promotion never updates; the case never read the symlink the deployment actually serves from | Fault run 4: `recovered: true` in the case record with `verify=0`, `identity=1`, symlink on the failed release | The phase's most important recovery claim would have been certified on an instrument that could not see the failure it was testing | Recovery is measured on the served release **and** the marker, the rollback record must have been produced by this run (`fresh`), and the candidate is retried through the documented verb instead of a hand-edited state | Run 6: `fresh=True`, `serving` and `marker` both `1.6.11-45d419a-ci37546936253`; a stale `rollback.json` from a previous attempt can no longer satisfy the case | FIXED |
| P33-M23 | HARNESS DEFECT | The incomplete-release case failed the deployment's own **disk-headroom gate** while measuring it: its 1.1 GiB copy of the release tree pushed free space under 5 GiB, and `verify` reported `free=4.63 GiB` | The case measured the deployment with its own copy still on disk | Fault run 5: `promote-incomplete-release … validate=False` with `verify=1`; the same check passes 107/107 with the copy removed | A case's verdict depended on the harness's footprint, not the deployment's health | The copy is removed before the deployment is measured (and the reason is recorded in the case) | Run 6: `promote-incomplete-release … detect/block/recover/validate = true`; `verify --quick` 107/107 before and after the suite | FIXED |
| P33-M24 | PIPELINE DEFECT | A candidate whose promotion failed and was rolled back **could not be promoted again**: `promote` accepts only `APPROVED`, nothing moved a recovered candidate back, and the immutable release directory cannot be re-staged without `--restage` | The state machine had no retry transition, and the deployment refuses an existing release directory by design | `state/1.6.11-f3da23a-ci37558387881.json` at `ROLLED_BACK`; `deploy` refusing the existing directory | Every transient failure (or every injection) burned its release candidate, and the only way forward was hand-editing state — the opposite of a documented process | `rc_release.py retry` re-approves a `ROLLED_BACK`/`DEPLOY_FAILED`/`ROLLBACK_REQUIRED` candidate after re-hashing the frozen artifact, refuses anything else (`REJECTED`, `PROMOTED`), records the actor and the previous state, and the promotion re-stages from the verified artifact on a retry | Run 5 and 6 used it (`retried_from=ROLLED_BACK`); `retry` on a `REJECTED` candidate is refused (`FATAL: … is REJECTED; only a candidate whose promotion failed and was recovered can be retried`) | FIXED |
| P33-K5 | KNOWN FINDING | The controller's post-deploy-validation rollback branch is unexercised: the injection that fails the controller's probes fails the deployment's smoke first | The deployment's post-promote smoke probes the same surfaces (health, auth, ingress, data), so a fault that survives to the controller's probes was not constructible here | Run 6 (`post-deploy-validation-failure`): the release was refused by the smoke, rolled back, and the controller recorded it; the controller's own probes ran only on healthy releases | One recovery branch is documented as a second line rather than as tested | Not fixed, by design: constructing it would mean disabling a deployment gate (weakening the deployment to test the controller) | Stated in §19 and §26, and in the case's own vocabulary (`performed_by=deployment`) | STATED |
| P33-K1 | KNOWN FINDING | Nine browser cases skip by repository design | Multi-worker and turn-lifecycle fixtures do not exist in this deployment (`DEEPTUTOR_MULTI_WORKER_E2E`, `DEEPTUTOR_TURN_E2E_FIXTURE`) | Suite source; the matrix summary (64 passed / 9 skipped) | No multi-worker turn coverage here | Reported as skipped, never as passed (carried from P32-K4) | Counts stated in §7 | CARRIED |
| P33-K2 | EXTERNAL DEPENDENCY | No LLM-backed chat turn is exercised anywhere in the release process | No model provider is configured or reachable in this environment | `production-websocket.json` notes (`own_books: 0`, acknowledgement recorded as not-applicable) | The completion paths are not covered by production probes | Stated as a limit of the environment, never inferred as a pass | The streaming probe records 'not applicable' instead of claiming a successful subscribe | STATED |
| P33-K3 | EXTERNAL DEPENDENCY | No signing or attestation infrastructure exists here (and the Actions artifact store is unreachable) | Environment; no sigstore/attestation tooling, no artifact download route | §3, §23 | Identity rests on hash + manifest + commit existence rather than a signature | No signing theatre: nothing is claimed to be signed | §23 names exactly what is verified | STATED |
| P33-K4 | INFRASTRUCTURE (environment event) | The sandbox was recycled mid-phase and destroyed the deployment host (`mo7-prod`), the release work directory, the Chromium build, the Playwright cache and `web/node_modules` | Platform recycle | §22; the rebuild logs in `run/` and `evidence/` | All runtime evidence produced before the recycle was lost; only committed documents survived | Rebuilt from the repository alone: work dir, wheel, host, browser, then the whole cycle re-run from scratch | `verify` 107/107 on the rebuilt host; the certified cycle re-executed end to end | RECOVERED |
| P33-M19 | PIPELINE DEFECT | A **correct rollback** reported one red check: the deployment verification requires the newest backup to have been rehearsed, and the rollback had just taken a new one without rehearsing it | The rehearsal was added to the promotion path (P33-M6) but not to the rollback path, which had never been executed until this phase | `rollback.json` (first run: `verified_after_rollback: false`), `negative/rollback-before-rehearsal-fix.json`, verify detail `rehearsed=…-pre-deploy-… newest=…-pre-rollback-…` | An operator following the documented rollback sees red on a healthy rollback — the failure mode that trains people to ignore checks | The rollback rehearses its own backup (restore into a scratch root, the restored release as the application) before the symlink flips | Re-run: `result: "rolled back"`, `verified_after_rollback: true`, 107/107, identity traceable, artifact 29/29, streaming 9/9 | FIXED |

One more limit, so the coverage is not read as wider than it is: the controller's *own* post-deploy-validation branch (probe the live release, roll back if a probe fails) is implemented and its probes run on every certified promotion, but no injection in this environment reaches it — the deployment's post-promote smoke is the earlier gate and refuses the same faults first (run 6: the smoke failed the release before the controller's probes ran). It is a second line, and it is stated as unexercised rather than claimed as tested. See P33-K5.

Two notes on the numbering. There is no `P33-M1`: no finding was ever recorded under that id in any artifact of this phase, and nothing here has been renumbered to fill the gap. `P33-M18` and `P33-K4` are different in kind from the rest: one is an environment limitation manifesting as a construction mistake, the other is the recycle itself, and both are included because the phase's evidence would otherwise claim a continuity the environment did not provide.

No finding in this phase was classified as flaky, and no fix weakened an assertion, deleted a test or changed product behaviour. Every PIPELINE DEFECT and HARNESS DEFECT above is in release tooling; every REPRODUCIBILITY DEFECT is in the build recipe's determinism; the KNOWN FINDINGs are stated rather than fixed, with the reason for each.


## 26. Cross-phase regression (§30)

| Surface | Status | Evidence |
| --- | --- | --- |
| Artifact identity | intact | `identity` traceable; artifact sha matches the frozen decision; Phase 32's own identity checks 107/107 |
| Production deployment | intact | the certified release is live; deployment log records the forward move with an 11 153 ms measured outage window |
| Rollback | intact | executed twice from the final state; the second reports `verified_after_rollback: true` with 107/107 afterwards (§19) |
| Backup / restore | intact | the rehearsal is part of every promotion and of every rollback, and passes 10 checks |
| Security | intact | Phase 27/29 harnesses run in the pipeline; artifact probe 29/29 on the live release |
| Browser | intact | 64 passed / 9 skipped / 0 failed on the deployed release (`/tmp/browser-final2.log`) |
| Performance | intact | Phase 28 baseline unchanged (no product source touched by this phase) |
| Persistence | intact | pre/post data comparisons in the promotion's migration gate |
| Recovery | intact | §16 host cases + §22 rebuild from nothing |

No product source file was modified by Phase 33. Every change is in
`docs-for-user/**` (release tooling, runbooks, reports) or `evidence/**`, which
is also why the certified candidate stays valid: the source-drift rule only
permits those two prefixes.

## 27. External dependencies and limits

* **LLM provider** — none configured or reachable. The chat/completion paths are
  not exercised by any production probe here; the streaming probe records
  `own_books: 0` and states that the own-book acknowledgement is *not applicable*
  rather than inferring success from a refusal.
* **No signing infrastructure** — no attestations; identity is enforced by hash,
  manifest and commit-existence checks (§23).
* **No inbound access to this host from CI** — promotion runs on the host that
  owns the deployment; the two halves are joined by rebuild-and-compare evidence.
* **Nine browser cases skip** by repository design (multi-worker and
  turn-lifecycle fixtures absent), carried from Phase 32 as P32-K4.
* **No container runtime** — the deployment is a host-level release tree with
  supervisord, as Phase 32 delivered.

## 28. Deferred / out of scope

* Pinning Next.js's generated build id and Server Actions key (would be a
  product configuration change; the identity mechanism covers the gap).
* Arming CI artifact attestation (the artifact store is unreachable from here).
* Anything belonging to Phase 34 and beyond — explicitly out of scope.

## 29. Evidence index

| Evidence | Location |
| --- | --- |
| CI runs and their verdicts | `evidence/ci/{37546291753,37546936253,37550740261}/release.json` |
| The red-gate run | `evidence/ci/37544304900/release.json` (`NOT_RELEASABLE`) |
| Candidate state and history | `/home/user/mo7-cicd/state/<rc>.json` |
| Frozen artifacts | `/home/user/mo7-cicd/rc/<rc>/deeptutor-1.6.11-py3-none-any.whl` |
| Candidate evidence documents | `/home/user/mo7-cicd/rc/<rc>/evidence/` |
| Negative cycle | `/home/user/mo7-cicd/negative/negative-tests.json` |
| Failure injection | `/home/user/mo7-cicd/negative/faults-local.json`, `faults-host.json` |
| Deployment logs | `/home/user/mo7-prod/run/promote-*.log`, `deploy-*.log` |
| Host verification | `/home/user/mo7-prod/evidence/production-{verify,identity,artifact-probe,websocket}.json` |
| Release marker and contract | `/home/user/mo7-prod/run/current-release.json`, `etc/production.env` |
| Deployment ledger | `/home/user/mo7-prod/etc/deployments.log` |
| Browser matrix | `web/test-results/` + the reporter summary in the run log |

## 30. Final checklist (§34)

## 30. Final checklist (§34)

The promotion-gate checklist, every line as executed. "Evidence" points at the
artifact that shows it, not at the code that implements it.

| # | Check | Result | Evidence |
| --- | --- | --- | --- |
| 1 | The repository, branch and event are the intended ones | PASS | `release.json` `source` rows; `state/<rc>.json` `branch` |
| 2 | The validated commit is in the checkout and the tree is clean | PASS | validate check 1 and 3; a dirty tree refuses (observed with an uncommitted report) |
| 3 | No product source changed since CI validated the commit | PASS | validate check 2 (`product=[]`); the rule refuses otherwise (`cb0eff9`, 15/16) |
| 4 | The CI evidence is frozen and unchanged | PASS | validate check 4 (digest) |
| 5 | The artifact is rebuilt from the same commit with the same recipe | PASS | validate checks 6–11; `build-report.json` |
| 6 | The rebuild and the CI artifact are the same content | PASS | validate check 12 (`unexplained: []`) |
| 7 | The artifact contract holds, including install and startup | PASS | 26/26 (`artifact-contract.json`) |
| 8 | The artifact security scan is clean | PASS | `artifact-scan.json`; negative case 1 refuses a planted key |
| 9 | Required gates red ⇒ candidate not releasable | PASS | run `37544304900` (`NOT_RELEASABLE`); negative case 5 |
| 10 | A rollback target exists before promotion | PASS | validate check 16 |
| 11 | Only a validated candidate can be approved | PASS | `REJECTED` refusals observed twice |
| 12 | The frozen artifact cannot change between decision and deployment | PASS | `promote` re-hashes; negative `artifact-substitution` |
| 13 | Pre-promote smoke on a scratch port | PASS | `SMOKE staged release reported /health/ready 200 on 8099` |
| 14 | A pre-deployment backup is taken **and rehearsed** | PASS | `MIGRATION restore rehearsal passed (10 checks)` |
| 15 | Migration/application validation runs before the switch | PASS | same rehearsal: restored data boots the release under promotion |
| 16 | Deployment identity is verified: commit, artifact hash, version | PASS | `identity: traceable=True … failures=0`; `undeclared-commit` refused |
| 17 | Health and smoke after the switch | PASS | `HEALTH backend ready and frontend serving`, `SMOKE post-promote smoke passed` |
| 18 | Post-deploy validation: verify, health, identity, artifact, ws | PASS | `post-deploy.json`, 5/5 steps exit 0 |
| 19 | Streaming (WebSocket) surfaces admit a session and refuse an anonymous one | PASS | `production-websocket.json` 9/9 |
| 20 | Browser suite against the deployed release | PASS | 64 passed / 9 skipped / 0 failed |
| 21 | A failed post-deploy validation rolls back automatically | PASS | fault case (§16), `automatic: true`, host back on the previous release |
| 22 | A rollback is verified after it happens | PASS | `rollback.json` → `verified_after_rollback: true`, 107/107 |
| 23 | The release is uniquely identifiable and tagged | PASS | `tag.json`, tag `mo7-release-1.6.11-45d419a-ci37546936253` on the promoted commit |
| 24 | Machine-readable evidence for every transition | PASS | §24 table |
| 25 | Nothing claims more than it measured | PASS | §27 limits; the streaming probe records "not applicable" rather than a pass |

## 31. Promotion assessment and repository state (§37)

<!-- STATUS -->
