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

Rollback is **automated** where it can be: `prod_promote.sh` rolls the symlink
back automatically if the health gate fails, and the controller invokes the
documented rollback when post-deploy validation fails — the fault case measures
that no human step was needed (`automatic: true` in `faults-host.json`).

The explicit rollback path, its evidence, and the recovery step are recorded in
§25 P33-M14 and `rc/<rc>/evidence/rollback.json`: `invoked: true`, exit 0,
`result: "rolled back"`, the restored release named, and the post-rollback
verification re-run on the host. The rollback refuses a target that is already
current and refuses a release id that was never staged (fault case
`rollback-to-unknown-release`).

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

<!-- FINDINGS_TABLE -->

## 26. Cross-phase regression (§30)

| Surface | Status | Evidence |
| --- | --- | --- |
| Artifact identity | intact | `identity` traceable; artifact sha matches the frozen decision; Phase 32's own identity checks 107/107 |
| Production deployment | intact | the certified release is live; deployment log records the forward move with an 11 153 ms measured outage window |
| Rollback | intact | executed and verified (§19) |
| Backup / restore | intact | the rehearsal is part of every promotion and passes 10 checks |
| Security | intact | Phase 27/29 harnesses run in the pipeline; artifact probe 29/29 on the live release |
| Browser | intact | 64 passed / 9 skipped on the deployed release |
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

<!-- CHECKLIST -->

## 31. Promotion assessment and repository state (§37)

<!-- STATUS -->
