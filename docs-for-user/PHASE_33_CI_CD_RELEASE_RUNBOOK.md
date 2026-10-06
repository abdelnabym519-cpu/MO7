# PHASE 33 — CI/CD & RELEASE PROCESS RUNBOOK

How MO7 is released: the pipeline that decides whether a commit is fit to be a
release candidate, the artifact identity that decision rests on, the promotion
gate, the deployment path, and the rollback. Every step in this document is a step
that ran; where a capability is not available in this environment it says so
instead of implying a check that never happens.

---

## 1. What is authoritative here

| Concern | Authority |
| --- | --- |
| Does this commit build a valid artifact? | `.github/workflows/release.yml` (GitHub Actions, `ubuntu-latest`) |
| What exactly was built? | `evidence/ci/<run-id>/artifact.json` committed by the run |
| Is the artifact safe to release? | `rc_scan.py` + `rc_contract.py` results inside the same evidence |
| Is the candidate fit to promote? | `promotion.json` verdict `RELEASABLE` / `NOT_RELEASABLE` |
| What is deployed, and from which commit and artifact hash? | `<prod>/run/current-release.json`, written by the Phase 32 deployment pipeline |
| Who decided? | `rc_release.py` state history (`<workdir>/state/<rc_id>.json`) |

Two facts shape the design:

1. **Run logs are not reachable from this environment.** TLS to
   `results-receiver.actions.githubusercontent.com` fails, so `gh run view --log`
   cannot be used. The pipeline therefore writes its evidence **into the
   repository** and commits it to the branch it ran on. Run metadata (status, job
   and step conclusions, check-run annotations) is still readable through the
   GitHub API and is used as the live channel.
2. **A GitHub-hosted runner cannot reach this deployment host** (the sandbox
   accepts no inbound connection, and the Actions artifact store is not routable
   from here). Promotion is therefore driven from the host that owns the
   deployment, and the two halves are joined by evidence each side can *verify*:
   the controller rebuilds the same commit with the same recipe and requires the
   two builds to be content-identical before it promotes anything.

## 2. Capability inventory (measured, not assumed)

| Capability | Classification | Evidence |
| --- | --- | --- |
| GitHub Actions pipeline execution on push | **AVAILABLE** | runs `37477425697`, `37477864727`, `37479689679` (`push`, head `arena/01a0ff3f-mo7`) |
| Pipeline triggering by `workflow_dispatch` | **UNAVAILABLE** | `gh workflow run` → HTTP 403 `Resource not accessible by integration`; the integration token has no `actions: write` |
| Reading run logs | **EXTERNALLY BLOCKED** | TLS EOF to `results-receiver.actions.githubusercontent.com`; evidence is published in-repository instead |
| Reading run/job/step metadata and check-runs | AVAILABLE | `api.github.com` `actions/runs`, `actions/runs/<id>/jobs`, `commits/<sha>/check-runs` |
| Downloading uploaded workflow artifacts | EXTERNALLY BLOCKED | the download endpoint redirects to `objects.githubusercontent.com`, which is TLS-blocked here |
| Pushing from inside the workflow (evidence commit) | AVAILABLE | run `37477425697` step 22 "Commit the evidence" succeeded; `evidence/ci/37477425697/**` is on the branch |
| Action secrets (`secrets.*`) | UNAVAILABLE | `403` on the secrets API; the pipeline therefore requires no secrets |
| Runner registration / self-hosted runner | UNAVAILABLE | `403` on runner registration endpoints |
| Google Fonts during the build | EXTERNALLY BLOCKED | `fonts.googleapis.com` / `fonts.gstatic.com` TLS EOF; the build uses the committed offline font mock (`font-mock.cjs`) with upstream `woff2` payloads |
| Playwright browser download | EXTERNALLY BLOCKED | `cdn.playwright.dev` TLS EOF; the browser suite uses the `@sparticuz/chromium` build when a Chromium is required |
| npm registry | AVAILABLE | used for `npm ci`, `npm pack geist@1.7.2 @fontsource/lora@5.3.0`, `@sparticuz/chromium` |
| PyPI | AVAILABLE | `pypi.org` and `files.pythonhosted.org` |
| Deployment host (Phase 32) | AVAILABLE | `/home/user/mo7-prod` — supervisord-managed release tree with the deployment pipeline |

**PIPELINE CONFIGURED is not PIPELINE EXECUTED.** This document only claims what
the classifications above rest on: a workflow that ran, with step conclusions and
committed evidence, or an explicit blocker with the error that produced it.

## 3. Release architecture

```
SOURCE ──▶ VALIDATION ──▶ BUILD ──▶ ARTIFACT ──▶ ARTIFACT VERIFICATION ──▶ RC
  ▲            │             │           │                  │              │
  │       (fails closed)     │           │                  │              ▼
  │                          │           │                  │        PROMOTION GATE
  │                          │           │                  │          │        │
  │                          │           │                  │        pass     fail
  │                          │           │                  │          ▼        ▼
  │                          │           │                  │      DEPLOYMENT  REJECT
  │                          │           │                  │          │     (new RC
  │                          │           │                  │          ▼      required)
  │                          │           │                  │   POST-DEPLOY VALIDATION
  │                          │           │                  │          │        │
  │                          │           │                  │        pass     fail
  │                          │           │                  │          ▼        ▼
  │                          │           │                  │       RELEASE  ROLLBACK
  └──────────────────────────┴───────────┴──────────────────┘                   │
                                                                                ▼
                                                                    re-validated previous release
```

* **SOURCE** — `rc_source_gate.py`: correct repository, branch, event, full commit
  sha, the checkout *is* the event commit, the tree is clean, and a version is
  declared. It writes `source.json` and refuses with a named reason.
* **VALIDATION** — toolchain install, product-tree Ruff (lint + format), import
  boundary contracts (`lint-imports`), architecture check
  (`scripts/check_architecture.py`), import/lazy-startup/isolated-worker checks,
  the Python test suite, Bandit, `pip check`, `npm ci`, and the frontend
  deterministic gate (`npm run check:fast`). Each stage runs through
  `rc_stage.py`, which records stage name, result, exit code, duration, failure
  reason and a log tail.
* **BUILD** — `rc_build.sh`, the single build recipe used by CI *and* by the
  deployment host. It cleans generated package data, removes the previous
  `.next`, removes the setuptools staging directory, builds the web bundle with
  offline fonts, scrubs the build-host path, materialises `deeptutor_web`, builds
  the wheel with `SOURCE_DATE_EPOCH` pinned to the commit timestamp, and writes
  `build-report.json`.
* **ARTIFACT + VERIFICATION** — `rc_manifest.py` (entry manifest, canonical form,
  second-build comparison), `rc_contract.py` (structure, metadata, required and
  forbidden files, digest identity, and — with `--install` — a clean-venv install,
  import, process start and `/health/live` + `/health/ready` probe), `rc_scan.py`
  (forbidden material), `rc_evidence.py` (the documents and the verdict).
* **RC** — a release candidate is identified by `(version, commit, pipeline run)`:
  `<version>-<commit7>-ci<run_id>`. It is immutable: new evidence, a new commit or
  a new artifact means a new RC.
* **PROMOTION GATE** — `promotion.json` / `release-candidate.json`: a required gate
  that fails makes the verdict `NOT_RELEASABLE`, and the pipeline fails the job.
* **DEPLOYMENT / POST-DEPLOY / ROLLBACK** — the Phase 32 pipeline
  (`prod.sh deploy`, `bin/prod_deploy.sh`) driven by `rc_release.py`.

## 4. Versioning and release identity

One strategy, applied everywhere:

| Field | Source | Example |
| --- | --- | --- |
| Application version | `deeptutor/__version__.py` (`__version__`) | `1.6.11` |
| Release number (RC id) | version + commit + pipeline run | `1.6.11-51f7219-ci37479689679` |
| Git tag (release) | `mo7-release-<rc id>` on the promoted commit — version, commit and pipeline run in one name | `mo7-release-1.6.11-51f7219-ci37479689679` |
| Commit identity | full 40-character sha, recorded in every document | `51f72191f0ef85e36fccb4cd71bad01be76d398a` |
| Artifact version | wheel metadata `Version` (must equal the application version) | `1.6.11` |
| Artifact identity | `sha256` of the wheel — the bytes that shipped | 64 hex characters |
| Content identity | canonical manifest digest — the same content under a different build id | 64 hex characters |
| Pre-release identifiers | `-rc.<n>` suffix reserved for a candidate that is not promoted | `1.6.12-rc.1` |
| Downstream publication | not part of this phase: `pypi-release.yml` and `docker-release.yml` trigger on a *published GitHub Release* with a `v<version>` tag, and both need external configuration (PyPI trusted publishing, GHCR credentials) that cannot be verified from here. The release tag deliberately carries the candidate identity instead, so a Phase 33 promotion cannot silently attempt an unverifiable publication. | — |
| Build identity | `SOURCE_DATE_EPOCH` = the commit timestamp | `1791297088` |

Every release is uniquely identifiable by the tuple **(version, commit, artifact
sha256, pipeline run id)**; no two releases share it, and none of the four is
reconstructed after the fact.

## 5. The ten named harnesses

Each harness validates **behaviour** — it runs the thing it judges and reads the
result — not the existence of a file.

| # | Harness | Behaviour it validates |
| --- | --- | --- |
| 1 | `rc_source_gate.py` | The release is built from the intended repository, branch, event and commit, with a clean tree, and a refusal names the criterion that failed |
| 2 | `rc_lint.py` | Ruff lint and format over the product tree (blocking) and over the whole checkout (recorded), with counts and a log tail |
| 3 | `rc_import_checks.sh` | Entry points import without pulling tool implementations into the process, and the isolated worker protocol executes real work (`operator:add 20+22 == 42`) |
| 4 | `rc_build.sh` | One recipe produces the wheel from a clean tree, with generated data cleared, the build path scrubbed and the epoch pinned; a failure aborts the build |
| 5 | `rc_manifest.py` | Two builds of one commit are compared entry by entry and every difference is classified by a checked rule; unexplained differences fail |
| 6 | `rc_contract.py` | The artifact contains what it must, excludes what it must not, carries the right identity, and (with `--install`) installs into a clean venv, starts, and answers liveness and readiness |
| 7 | `rc_scan.py` | Secrets, private keys, credential files, repository/bytecode metadata, debug configuration and build-host paths are detected and block; benign observations are recorded with a finding id |
| 8 | `rc_evidence.py` | The gate table, the machine-readable documents and the verdict are produced from the real inputs; a red required gate makes the candidate unreleasable |
| 9 | `rc_negative.py` | Invalid candidates are refused by the real tooling: a planted private key, a missing required file, two builds in one artifact, a version disagreement, and a red required gate |
| 10 | `rc_release.py` | The candidate lifecycle: ingest → validate (rebuild + equivalence + contract + scan) → approve → promote → post-deploy validation → rollback, with a frozen artifact that cannot change unnoticed |

`rc_stage.py` and `rc_faults.py` are supporting instruments: the first records each
pipeline stage, the second injects faults and requires detect → block → recover →
validate.

## 6. Running the release pipeline

Push a commit to a release branch (`main`, `dev`, or the working branch used
here). The pipeline starts on its own; `workflow_dispatch` is unavailable in this
environment (403), so a push is the trigger.

```bash
git push origin <branch>                     # starts the run
```

Watch it:

```bash
gh run list --branch <branch> --limit 3
gh run view <run-id>                          # metadata; --log is unavailable here
```

Read what it produced (this is the authoritative record):

```bash
git pull --ff-only
ls evidence/ci/<run-id>/
cat evidence/ci/<run-id>/promotion.json      # the gate table and the verdict
cat evidence/ci/<run-id>/artifact.json       # what was built, and the second-build comparison
```

## 7. Evidence documents

Every run writes, and commits, these files under `evidence/ci/<run-id>/`:

| File | Contents |
| --- | --- |
| `release.json` | top-level record: repository, branch, commit, version, artifact, pipeline identity, result, stage list |
| `artifact.json` | artifact name, bytes, sha256, entry count, manifest digest, canonical digest, build environment, build report |
| `test-summary.json` | pytest totals from the JUnit XML, Ruff state for both scopes, and the stage results the pipeline produced |
| `security-summary.json` | artifact scan result and counts, code scan (Bandit) counts, dependency check |
| `promotion.json` | the gate table, policy and exceptions, blocking failures, verdict |
| `release-candidate.json` | the candidate: id, identity, verdict, stages, reproducibility, immutability rule and digest |
| `evidence-digest.txt` | one digest over all of the above, so a later edit is detectable |

All documents carry `recorded_at`, `commit`, `version`, `environment`, `result`,
and the artifact sha256 where it applies.

## 8. Promotion gate and approval

`release-policy.json` (committed) declares:

* `required_gates` — the gates whose failure makes a candidate `NOT_RELEASABLE`;
* `exceptions` — a gate that is allowed to be red **only** with a named finding, a
  classification and the evidence for it. An excepted gate still appears in the
  gate table as red, with its finding id: nothing is silently downgraded;
* `approval` — the approval model. It is automatic after the gates, because every
  condition a human approver would check is machine-checked first
  (source, gates, contract, scan, equivalence, rollback target). `rc_release.py
  approve --by <name>` records a named human approver instead, for a policy that
  wants one; no extra friction was added.

Promotion:

```bash
cd /home/user/MO7/docs-for-user/phase33-cicd
python rc_release.py status
python rc_release.py ingest   --evidence /home/user/MO7/evidence/ci/<run-id>
python rc_release.py validate --rc <rc-id>        # rebuilds, compares, contract-checks, scans
python rc_release.py approve  --rc <rc-id>
python rc_release.py promote  --rc <rc-id>        # deploy through the Phase 32 pipeline
python rc_release.py deployed                     # what the host is running now
```

`validate` refuses when the checkout is not the commit CI validated, when the
frozen CI evidence has changed, when the rebuild is not content-identical to what
CI built, when the contract or scan fails, or when there is no rollback target.

## 9. Deployment, post-deploy validation and rollback

Deployment is the Phase 32 pipeline, unchanged and integrated:

```
VERIFY ─▶ STAGE ─▶ PRE-PROMOTE SMOKE ─▶ BACKUP ─▶ PROMOTE ─▶ RESTART
       ─▶ HEALTH ─▶ POST-PROMOTE SMOKE ─▶ RELEASE
```

* VERIFY recomputes the artifact hash and compares it with the declared one, and
  records the commit.
* STAGE installs the wheel into the release's own virtualenv and materialises the
  web bundle.
* PRE-PROMOTE SMOKE starts the staged release on a scratch port and probes it
  before it can take traffic.
* BACKUP is the pre-migration control.
* PROMOTE is an atomic symlink flip; a failed health check rolls the symlink back
  automatically and re-verifies the previous release.
* POST-DEPLOY VALIDATION: `bin/prod.sh verify`, `health`, `artifact`, `security`,
  `validate`, plus the browser suite where Chromium is available.

Rollback:

```bash
bin/prod.sh releases                      # what is staged and which is current
bin/prod.sh rollback --to <release-id>    # promote the previous release and verify
python rc_release.py rollback --to <release-id> --rc <rc-id>   # controller + state record
python rc_release.py verify --rc <rc-id>  # the frozen candidate has not moved
```

Rollback is automated only where no human step is required: the deployment
pipeline's health-check rollback is automatic; a deliberate rollback to an older
release is a single command whose verification is automated, and it is documented
as an operator action rather than as an unattended one.

## 10. Failure handling

* A required gate failure ⇒ verdict `NOT_RELEASABLE` ⇒ the pipeline job fails
  closed, and the failure is committed as evidence before the job exits.
* A failed deployment ⇒ `rc_release.py` records `DEPLOY_FAILED` and refuses to
  continue; the previous release keeps serving.
* A failed post-deploy validation ⇒ record `DEPLOY_FAILED` (or roll back), never
  "released".
* A change to the source, the artifact or the evidence after validation ⇒ the
  candidate is no longer promotable: a new run and a new RC are required.

## 11. Failure injection

```bash
python rc_faults.py --layer local --artifact <wheel> --workdir /tmp/faults --evidence <dir>
python rc_faults.py --layer host  --artifact <wheel> --workdir /tmp/faults --evidence <dir>
```

Local cases: a red required gate, a failing build, a failing artifact contract,
and a substitution of the validated artifact. Host cases: a deployment failure
(wrong declared hash), a pre-promote smoke failure, a post-deploy validation
failure, and a rollback invocation. Each case must be detected, blocked, recovered
from, and the recovered state validated.

## 12. Known limits (stated, not implied)

* Byte-level reproducibility of the web bundle is **not** achieved and is not
  claimed: Next.js generates a random build id and a random Server Actions
  encryption key per build, and pinning the build id would be a product
  configuration change. What is verified instead is that two builds of one commit
  ship identical content once those generated identifiers and the ordering of
  generated manifest maps are accounted for — with **zero unexplained
  differences** — and the released bytes are pinned by the artifact sha256.
* The build requires the offline font mock, because Google Fonts is TLS-blocked
  from this environment.
* Workflow logs and uploaded artifacts cannot be read from here; the in-repository
  evidence is the channel, and the API's run/job/step conclusions are the live
  signal.
* `workflow_dispatch` cannot be used to start a run in this environment; a push is
  the trigger.
