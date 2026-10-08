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
| GitHub Actions pipeline execution on push | **AVAILABLE** | runs `37477425697`, `37477864727`, `37479689679` (refused candidates) and `37486103941` (`RELEASABLE`), all `push` on `arena/01a0ff3f-mo7` |
| Pipeline triggering by `workflow_dispatch` | **UNAVAILABLE** | `gh workflow run` → HTTP 403 `Resource not accessible by integration`; the integration token has no `actions: write` |
| Reading run logs | **EXTERNALLY BLOCKED** | TLS EOF to `results-receiver.actions.githubusercontent.com`; evidence is published in-repository instead |
| Reading run/job/step metadata and check-runs | AVAILABLE | `api.github.com` `actions/runs`, `actions/runs/<id>/jobs`, `commits/<sha>/check-runs` |
| Downloading uploaded workflow artifacts | EXTERNALLY BLOCKED | the download endpoint redirects to `objects.githubusercontent.com`, which is TLS-blocked here |
| Pushing from inside the workflow (evidence commit) | AVAILABLE | run `37477425697` step 22 "Commit the evidence" succeeded; `evidence/ci/37477425697/**` is on the branch |
| Action secrets (`secrets.*`) | UNAVAILABLE | `403` on the secrets API; the pipeline therefore requires no secrets |
| Runner registration / self-hosted runner | UNAVAILABLE | `403` on runner registration endpoints |
| Google Fonts during the build | EXTERNALLY BLOCKED | `fonts.googleapis.com` / `fonts.gstatic.com` TLS EOF; the build uses the committed offline font mock (`font-mock.cjs`) with upstream `woff2` payloads |
| Playwright browser download | EXTERNALLY BLOCKED | `cdn.playwright.dev` TLS EOF |
| Browser binary | **AVAILABLE** (via npm) | `@sparticuz/chromium@153.0.0` from the npm registry (`npm install`, not `npm pack` — the packed tarball's build tree is not runnable), laid out where Playwright 1.57 expects it (`~/.cache/ms-playwright/chromium-1200/chrome-linux64/chrome` + `INSTALLATION_COMPLETE`); the package's `bin/al2023.tar.br` is inflated explicitly because it is only auto-inflated on Amazon Linux, and the resulting libraries are put on `LD_LIBRARY_PATH` by `prod_browser.sh`. `chrome --version` reports `Chromium 153.0.8010.0`; the full matrix reports 64 passed / 9 skipped |
| Running the browser suite against a deployment | **AVAILABLE** | `bin/prod.sh session <account>` then `MO7_CHROMIUM_HOME=<extracted> bin/prod.sh browser --project=<p>` |
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
  forbidden files, digest identity, and — with `--install` — a clean-venv install
  with the artifact's declared dependencies, import, process start and
  `/health/live` + `/health/ready` probe), `rc_scan.py` (forbidden material),
  `rc_evidence.py` (the documents and the verdict — and its exit status).
  The run publishes its own **build manifest** (`artifact-manifest.json.gz`, a
  digest map of the 4400 entries) with the evidence. That file is what lets
  another host verify that its rebuild is the same content as the artifact CI
  built: the wheel itself cannot be fetched from this sandbox, and a digest of
  digests would not be verifiable.
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
| Content identity | the entry-by-entry comparison of two build manifests, every difference explained by a checked rule — **not** a canonical digest. A canonical digest that embeds the build id only tells you two builds are the same once you already know they are the same; the comparison says *which* entries differ and why, and `unexplained` is the number that has to be zero | `reproducible: True`, `unexplained: 0` |
| Byte identity | the wheel's SHA-256, which is equal when the two builds ran in the same toolchain (Python/Node). Proven inside this toolchain; a cross-toolchain difference (Python 3.11.16/Node 22.23.3 in CI vs 3.11.2/22.22.3 here) is declared and classified rather than assumed away — see §13 | equal, or every difference classified |
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

`rc_stage.py`, `rc_faults.py` and `rc_faults_host.py` are supporting
instruments, and `bin/prod.sh ws` (`docs-for-user/phase32-infra/prod_ws.py`) is the
deployment-side one: it opens real WebSocket sockets against the host under test,
which is the only way to see an admission decision made before `accept()`. They are
listed here as instruments rather than as pipeline stages because they are:
the first records each pipeline stage, the second injects faults and requires
detect → block → recover → validate (local layer), and the third does the same on
the deployment host through `prod.sh` and `rc_release.py`.

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
| `artifact-manifest.json.gz` | the build's entry manifest (names, digests, canonical and digest-blind forms, per-entry variants), so another host can verify its own rebuild is the same content |
| `artifact-contract.json`, `artifact-scan.json`, `build-report.json`, `reproducibility.json`, `lint-product.json`, `lint-repo.json` | the stage reports the verdict was computed from, committed with it: a failed gate can be diagnosed from the evidence rather than from a log that is not reachable here |
| `evidence-digest.txt` | one digest over all of the above, so a later edit is detectable |

All documents carry `recorded_at`, `commit`, `version`, `environment`, `result`,
and the artifact sha256 where it applies.

## 8. Promotion gate and approval

`release-policy.json` (committed) declares:

* `required_gates` — the gates whose failure makes a candidate `NOT_RELEASABLE`;
* `exceptions` — **empty**. A gate that may be red with a named finding is a way
  for a red required gate to pass the table, and there is no longer a gate here
  that needs it: the pipeline's pytest run is green on this branch (8434 passed,
  0 failed, 0 errors), so `python tests` is required without qualification. (The
  repository's *scheduled* `Tests` workflow on `main` is red — runs
  `37448715493`, `37296467799`, `37193244526` — but that is a pre-existing
  condition of `main`, not of the release branch, and it is recorded as a finding
  rather than used to excuse the release gate.) An excepted gate, if policy ever
  declares one, still appears in the table as red with its finding id: nothing is
  silently downgraded;
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

`validate` refuses when the checkout does not contain the commit CI validated,
when product source (anything outside `evidence/` and `docs-for-user/`) changed
since that commit, when the tree is dirty, when the frozen CI evidence has
changed, when the rebuild was made by a different recipe or a different
`rc_manifest`/`rc_scrub_paths`/`prepare_web_package` than CI recorded, when the
rebuild is not the same content as the CI artifact, when the contract or scan
fails, or when there is no rollback target. The pipeline commits its evidence
*after* the build, so branch head is normally one commit ahead of the validated
commit; that is why the rule is "contains, and nothing outside evidence/tooling
changed" rather than "is exactly".

`ingest` returns non-zero when the candidate's CI verdict is not `RELEASABLE`,
and `rc_evidence.py`'s own exit status carries its verdict, so a shell that only
checks exit codes still refuses an unreleasable candidate.

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
* POST-DEPLOY VALIDATION: `bin/prod.sh verify`, `health`, `artifact`, `ws`
  (the streaming surfaces), `security`, `validate`, plus the browser suite where
  Chromium is available. `bin/prod.sh ws` opens real WebSocket connections
  through the deployment's own ingress and asserts admission (a session is
  admitted, an anonymous or forged one is refused), the subscription contract
  (an unknown book is refused by name and the socket stays open) and
  cross-account behaviour (another account's subscription is refused without
  revealing whether the book exists). It is also part of the pre-promote smoke:
  HTTP probes cannot see the difference between a refused upgrade and one that
  was silently degraded into a plain GET, which is exactly the failure the
  certification ingress had.

Rollback:

```bash
bin/prod.sh releases                      # what is staged and which is current
bin/prod.sh rollback --to <release-id>    # promote the previous release and verify
python rc_release.py rollback --to <release-id> --rc <rc-id>   # controller + state record
python rc_release.py verify --rc <rc-id>  # the frozen candidate has not moved
```

The controller writes the promotion's evidence next to the candidate:
`deployment.json` (what was deployed, the deploy log, the resulting release),
`post-deploy.json` (each probe, its exit code and its output tail),
`rollback.json` (whether a rollback was invoked, its result, and what the
deployment was serving after it) and `tag.json` (the annotated
`mo7-release-<rc id>` tag on the promoted commit, with the version, rc id and
artifact sha256 in its message).

Rollback is automated only where no human step is required. The deployment
pipeline's health-check rollback is automatic. A promotion whose post-deployment
validation fails triggers the controller's rollback automatically, and the host
fault-injection case measures whether recovery actually completed without a
human step: `faults-host.json` records `automatic` alongside `recovered`, so the
documentation says "manual" whenever a person was needed.

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
and a substitution of the validated artifact. Host cases (need a built
deployment and `PROD_ROOT`): a wrong declared hash, an undeclared commit, an
artifact without the packaged web server (a pre-promote failure), a hand-edited
release directory, a rollback to a release that was never staged, and a promotion
whose post-deploy validation fails — the last one needs `MO7_FAULT_RC_ID` set to
an `APPROVED` candidate and is the rollback test, not a simulation of it: it
breaks the origin the post-deploy probes measure through, runs the real
promotion, and requires the previous release to be serving again afterwards.
Each case must be detected, blocked, recovered from, and the recovered state
validated; the host layer is loaded lazily so the local layer runs anywhere.

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
* The deployment host described in §9–§11 **does not currently exist**: the third
  sandbox recycle destroyed `/home/user/mo7-prod` after certification (P33-M31,
  closure report). The host can be rebuilt from the checkout with §17; it is
  deliberately not rebuilt, because §38 forbids recovery work beyond what
  validating the release process required and that validation is complete and
  recorded in `evidence/phase33-runtime/`.
* The frontend deterministic gate has a **known intermittent repository-test
  defect** with a reproduced cause (P33-M28). Four test sites have too-short
  scheduling budgets under concurrent workers: `knowledge-linked-folders`
  queries synchronously after waiting only for the mock call; `watching-browser`
  uses Testing Library's default one-second query window for account/feed results;
  `co-writer-lazy-notebook` queries a dynamically imported dialog within that
  same window; and `settings-unified-draft` has a default five-second whole-test
  timeout. In the durable comparison, the unpatched eight-worker stress baseline
  failed in 6/6 full-suite runs; the patch passed the full suite 3/3 at the
  four-worker CI-parallelism replay. Those measurements are in
  `evidence/phase33-runtime/frontend-gate-intermittent/`.
  The patch — `docs-for-user/phase33-cicd/frontend-gate-intermittent-failures.patch`
  — changes synchronization and time budgets only; the same element is required, the same first button is clicked, and the downstream assertion expectations and operations are unchanged.
  It is **not applied** because it changes `web/**` and therefore creates a new
  source revision by the drift rule (P33-M12). Applying it is the first step of a
  new cycle: `git apply` that patch, run the pipeline, make a new candidate, and
  re-certify. Do not retry a refused run just to obtain a green result.
* The two historical failing runs (`37690935036`, `37699029751`) predate P33-M29's
  diagnostics and therefore contain no stage tail. For later runs, inspect the
  frontend gate's `failure_tail` in `release.json` and the GitHub error annotation;
  `blocking` (P33-M30) tells whether a red row blocks independently of whether
  the policy names it `required`. A failure remains a refusal either way.
* Reproducibility is verified **within a toolchain and between toolchains by
  classification**: the pipeline's runner (Python 3.11.16, Node v22.23.3) and this
  host (Python 3.11.2, Node v22.22.3) cannot be made identical here (python.org,
  nodejs.org and the OS package sources are TLS-blocked), so what is verified is
  that the two builds' entry sets and contents agree once the generated build id
  and the entries that record it are accounted for — every difference classified,
  none unexplained. A difference that the rules cannot explain is a refusal, not a
  note: it is the promotion gate. See §13 for how far that has been pushed.

---

## 13. Build reproducibility (measured)

The claim is deliberately narrow, and every part of it has a measurement:

| Property | Verdict | Measurement |
| --- | --- | --- |
| Two builds of one commit, one toolchain, **different directories** | content-identical: `reproducible: True`, `unexplained: 0`, **3 entries differ** (Next's generated secrets and the `RECORD` that digests them) | §13.3 |
| Two builds of one commit, **different machines and toolchains** (CI runner vs this host) | `reproducible: True`, `unexplained: 0`: 4 entries carry generated or builder-derived values, 64 differ only in the order their JSON was written, and the wheel's `RECORD` is accounted for by those 68 entries it indexes | §13.3, §13.7 |
| The wheel's own index of entry digests (`RECORD`) | explained **by entailment**, never by blindfold: only when the entry sets match and every other difference is already named | §13.7, verified against the frozen CI evidence and by a negative test |
| A gate that crashes **after** writing its result document | **fixed**: the contract, scan and reproducibility stages now run under `rc_stage.py`, whose document records the exit status itself | §13.8, verified by injection (§11) |
| The artifact carries no builder path | verified | `path-scrub.json`: every occurrence rewritten, every file the same size |
| The artifact carries no random build id and no random tsconfig name | verified | `build-metadata.json`: both derived from commit + `SOURCE_DATE_EPOCH` + version, every occurrence rewritten |
| Per-build generated **secrets** differ between builds | **true, by design, and not derived** | `prerender-manifest.json` (`preview.previewModeId`, `preview.previewModeSigningKey`), `server-reference-manifest.json` (`encryptionKey`) |
| The artifact records the **builder's network addresses** | **true**, masked in the comparison, recorded as a finding | `allowedDevOrigins` in `required-server-files.json` and in the standalone `server.js` (measured: `["127.0.0.1","169.254.0.21"]` here, a CI runner's own addresses there) |
| The artifact records the **builder's CPU count** | **fixed**: the recipe pins `CIRCLE_NODE_TOTAL` | `build-report.json`: `build_cpu_pin`; verified by building under a contradictory environment value |

### 13.1 What the comparison is

`rc_manifest.py` records every entry of the wheel (name, digest, size, and the
digest of the file with the build id masked). `rc_manifest.compare` then walks the
two manifests and puts every difference into a named class:
`names_differ_only_by_build_id`, `content_differs_only_by_build_id`,
`derived_and_generated_values`, `derived_entry_index`, `ordering_only_lines`,
`ordering_only_json`, `ordering_only_embedded_json`, `ordering_only_entries`, and
`unexplained`.
`reproducible` is true only when `unexplained == 0` and the entry sets match.
Nothing is ever excused silently: an entry that cannot be explained is a refusal.

### 13.2 The defects this found, and what fixed them

**P33-R1 — the build leaked the builder's directory into the artifact.** Two
builds of `e344c17` on this host, in two different directories, compared as
`98` renamed entries and `381` unexplained content differences (CI's artifact
compared against a local one: `376` unexplained). Webpack derives module and
chunk ids from module identifiers, which are absolute paths; scrubbing the path
out of the files afterwards cannot repair an id that was already hashed. Fixed by
building at a **canonical build root** — `rc_build.sh` mirrors the checkout into
`/tmp/mo7-build/src` and builds there, so every builder builds at the same
absolute path. Identity is still read from the real checkout.

**P33-R3 — the artifact recorded the builder's machine.** (its network addresses, and — the second half of the same defect — its CPU count, P33-R4) Two values in the
generated Next.js configuration come from the machine that builds it:
`allowedDevOrigins` (this machine's non-loopback IPv4 addresses, from
`web/next.config.js`) and `experimental.cpus` (`Math.max(1, os.cpus().length - 1)`
inside Next itself). Both are written into `required-server-files.json` and
embedded in the standalone `server.js`, so the artifact said how many cores the
builder had and which network it was on — and a rebuild on another machine could
never match it. The worker count is now **a declared build input**: the recipe
exports Next's own `CIRCLE_NODE_TOTAL` override (`MO7_BUILD_CPU_PIN`, default 2),
so the environment cannot supply the value; measured, a build run under
`CIRCLE_NODE_TOTAL=8` still records the pin and compares clean. The address list
is masked in the comparison (§13.6) because removing it means changing product
source, which this phase does not do.

**P33-R2 — the artifact carried random values.** Next.js generates a random build
id per build (measured: 433 files, 450 occurrences in one bundle) and writes a
random number of **random width** into the name of the temporary tsconfig it
records in `required-server-files.json` and the standalone `server.js` (measured
across builds: `…-4867.json`, `…-60173.json`). Fixed by deriving both from the
release identity — commit + `SOURCE_DATE_EPOCH` + version — rewriting every
occurrence, and always emitting a five-digit tsconfig number
(`rc_normalize_build.py`, which fails the build if an occurrence survives outside
Next's own webpack cache). The first version of this kept the generated width,
which meant the derived name still depended on a random value — the promotion gate
refused the candidate, and the width is now fixed. The build-id rewrite is
length-preserving; the tsconfig rewrite is length-preserving whenever the
generated name was five digits and shifts no offsets either way, because it is
applied before the bundle is packaged and both builders apply the same rewrite.

**P33-R4 — the artifact recorded the builder's CPU count.** Next derives
`experimental.cpus` from the machine it runs on (`os.cpus().length - 1`) and writes
it into the generated configuration, so a release artifact said how many cores the
builder had. See the P33-R3 note below and §13.6: the value is now a declared build
input.

**P33-M2 — the comparison could fail without the run noticing.** `rc_manifest.py`
has two output paths: the machine-readable document (`--json-out`, written first)
and the human-readable stream the pipeline tees into `reproducibility.txt`. The
stream read a class key that had been renamed (`build_id_derived_digests`), so it
aborted with a `KeyError` after four lines: the document was complete and green,
the step exited non-zero, and the run was still called releasable — the step was
tolerated (`continue-on-error` on a non-strict run) and the assembler only read
documents. Fixed: the stream prints every class with the labels
`rc_evidence.py` parses, and a class that is renamed now fails loudly with exit
`2` instead of shortening the evidence. Verified: the CLI exits `0` on the pair
that used to crash it, all nine classes print, and `rc_evidence.reproducibility`
still parses the stream.

**P33-M3 — a stage could crash after writing its result document.** The artifact
contract, the artifact scan and the reproducibility comparison write their result
document before printing their summary. A crash in that window left a green
document behind, and the assembler gates on documents, so the failure would not
have reached the verdict. Fixed: those three stages now run under `rc_stage.py`,
which records the command's own exit status into a stage document, and the
assembler reads those documents. Verified by injection against the real evidence
inputs: every stage document green → `RELEASABLE`, zero blocking rows; the
reproducibility stage's document recording a failed exit → `NOT_RELEASABLE`, with
that stage named in `blocking_failures` and the assembler exiting `1`.

### 13.3 What two builds of one commit actually produce

Measured after all fixes, one toolchain, two different build directories:
**4400 entries, identical entry sets, 3 entries differing** — the two generated
secrets (`prerender-manifest.json`, `server-reference-manifest.json`) and the wheel
`RECORD` that digests every entry. `derived_and_generated_values: 3`, everything
else `0`, `unexplained: 0`.

Measured across machines — the CI artifact of run `37527513491` against a rebuild
of the same commit (`8add754`) on this host: **4400 entries, identical entry sets,
69 entries differing**, and every one of them named:
`derived_and_generated_values: 4` (`required-server-files.json` and `server.js`
carrying the builder's addresses, the two generated secrets),
`ordering_only_embedded_json: 62` and `ordering_only_json: 2` (Next writes those
JSON maps in a different order per build), and `derived_entry_index: 1` (the wheel
`RECORD`, accounted for by the 68 entries it indexes — §13.7). `unexplained: 0`.
Byte identity is therefore **not** claimed, and the reason is not a leftover build
input: it is Next.js generating random secrets per build (see §13.5). The
comparison masks those fields **by name** and then requires byte identity of the
rest, so the claim is checkable: if any other byte in any other entry moved, the
rule does not apply and the difference is `unexplained`.

### 13.4 Across toolchains

The CI runner and this host cannot be made byte-identical environments here
(Python 3.11.16/Node 22.23.3 vs 3.11.2/22.22.3; the toolchain downloads are
TLS-blocked), so the cross-toolchain claim is the same comparison: the same
entries, the same content, zero unexplained differences — which is what the
promotion gate requires. The artifact's digest is meaningful together with the
toolchain recorded in `build-report.json` (`python`, `node`, `setuptools`,
`recipe_sha256`, `tool_hashes`), and the gate refuses a rebuild whose recipe or
tool hashes differ from the ones CI recorded. A rebuild that cannot reproduce the
content is a refusal.

### 13.5 Why the generated secrets differ, and why they were not "fixed"

`preview.previewModeId` and `preview.previewModeSigningKey` sign preview-mode
cookies; `encryptionKey` encrypts Server Action payloads. They are secrets, and
Next.js randomises them on every build. Deriving them from the commit — the trick
used for the build id and the tsconfig name — would make them **computable by
anyone with the repository**, which is a real weakening: a forgeable preview
cookie and craftable Server Action payloads. They are left random, and the
comparison says so instead of pretending otherwise.

### 13.6 The third class: values that belong to the builder's machine

`web/next.config.js` enumerates this machine's non-loopback IPv4 addresses into
`allowedDevOrigins` at build time, so a release artifact records the address of the
machine that built it. Measured: this host builds `["127.0.0.1","169.254.0.21"]`
and a CI runner builds its own, so `required-server-files.json`, the standalone
`server.js` (which embeds the whole config) and the wheel `RECORD` that digests
them differed between CI and every rebuild — the promotion gate refused the
candidate, three times, naming exactly those entries.

Two ways to close it: remove the value from the artifact, or mask it in the
comparison. Removing it means changing `web/next.config.js` — product source, and
`next start` ignores the setting, so the only thing the change would buy is a
cleaner artifact. That is a product decision, not a release-process one, so it is
**not** made here. Instead the comparison masks the array by field name, in the
same place it masks Next's generated secrets, and requires every other byte of
those entries to be equal — a difference anywhere else in them is still
unexplained. The residual is recorded: an artifact built on this host carries this
host's address, as finding P33-R3.

This is the deliberate line in this phase: non-determinism that is *identity*
(the build id, the temporary tsconfig name, the builder's path) is removed;
non-determinism that is *secret material* or *the builder's own environment* is
documented, and the promotion gate compares content with those fields masked by
name, requiring everything else to be byte-equal.

If a release were ever disputed, the verifiable chain is: commit → the manifest
published with the CI evidence (`artifact-manifest.json.gz`) → this host's rebuild
compared against it entry by entry → the artifact SHA-256 recorded in the release
candidate → the deployment contract's `artifact_sha256` → the release tree that is
running. Every link is a file in this repository or on the deployment host.
* Workflow logs and uploaded artifacts cannot be read from here; the in-repository
  evidence is the channel, and the API's run/job/step conclusions are the live
  signal.
* `workflow_dispatch` cannot be used to start a run in this environment; a push is
  the trigger.

### 13.7 The index the artifact keeps of itself

Every wheel carries `<dist-info>/RECORD`: one line per entry, giving the path, the
digest of that entry's bytes and its size. It is the one entry whose content
cannot be independent evidence — each line is a function of another entry, all of
which are compared where they belong, and more precisely than a digest of the
index could. This was the last remaining difference the promotion gate refused
(run `37527513491`, entry `deeptutor-1.6.11.dist-info/RECORD`), and the refusal was
correct: the index genuinely differs when the files it indexes differ.

The rule that explains it is entailment, not masking (`rc_manifest.compare`,
class `derived_entry_index`). It fires only when both conditions hold:

* the two builds contain exactly the same entries — nothing added, nothing
  removed; and
* every other entry whose content differs has already been named by a rule.

If any other difference is left unexplained the index stays unexplained with it,
so the rule can never cover a real change: the change itself is still reported, and
the release is still refused. The result records the arithmetic
(`index_entailment`): the number of differing entries the index covers, how many
of them were named by other rules, and that nothing was added or removed.

Measured against the frozen CI evidence for run `37527513491`: the index covers 68
differing entries, all 68 named, `unexplained: 0`, `reproducible: True`. Negative
test: with one entry's digests perturbed so that no rule can explain it, the result
is `unexplained: ['deeptutor-1.6.11.dist-info/RECORD',
'deeptutor_web/.next/server/middleware-manifest.json']` and `reproducible: False`
— the index is refused together with the change.

### 13.8 What the pipeline records about its own stages

A gate whose document is written before it prints can crash in between and leave a
green document behind; a stage whose exit status nobody records can fail while the
run still reports `RELEASABLE`. Both were observed here (P33-M2, P33-M3) and both
are now closed: the contract, scan and reproducibility stages execute under
`rc_stage.py`, which writes a stage document containing the command's own exit
status, and the assembler turns each stage document into a required gate. A
document that is missing is itself a blocking gate, so a stage cannot disappear
from the record. The failure injection for this is in §11.

## 14. Operating the release cycle

The whole cycle, in the order it is run (controller invocation in §6):

```bash
W=/home/user/mo7-cicd          # the release work directory
R=docs-for-user/phase33-cicd   # the tooling, from the checkout

python3 $R/rc_release.py ingest   --evidence evidence/ci/<run-id>   # CI evidence -> candidate
python3 $R/rc_release.py validate --rc <rc-id>                      # rebuild + verify + gate
python3 $R/rc_release.py approve  --rc <rc-id>                      # a VALIDATED candidate only
python3 $R/rc_release.py promote  --rc <rc-id> [--previous <id>]     # deploy + post-deploy gate
python3 $R/rc_release.py rollback --rc <rc-id> --to <release-id>     # restore + record
python3 $R/rc_release.py verify   --rc <rc-id>                      # the frozen candidate has not moved
```

Two things the operator needs to know before running any of it:

* The gate that runs `npm run check:fast` in the frontend can fail **without a
  defect in the change being certified** — the intermittent repository tests in
  §12 (P33-M28). For the post-instrumentation runs, read the stage's `failure_tail`
  in `evidence/ci/<run-id>/release.json` and the GitHub error annotation; those two
  failing historical runs predate that evidence fix, so their cause is established
  by the local reproduction in the evidence index instead. Do not re-run a refused
  candidate to get a green; apply the patch only as part of a new release cycle.
* Everything here runs against paths under `/home/user`; after a recycle the
  work directory and the host must be rebuilt first (§17). Check
  `python3 $R/rc_release.py status` before any controller operation, because a
  host-level `bin/prod.sh promote` does not update the controller's state.

State is a file, not a memory: `<workdir>/state/<rc-id>.json` carries the current
state (`CREATED` → `VALIDATED` → `APPROVED` → `PROMOTED`, or `REJECTED`,
`ROLLBACK_REQUIRED`, `ROLLED_BACK`) and the history of who moved it and why. Two
rules matter operationally:

* **A verdict is immutable.** A `REJECTED` candidate cannot be re-validated, and
  an `APPROVED` one cannot be re-approved: the fix belongs in a new commit, which
  means a new CI run and a new candidate. This is what keeps "which artifact was
  approved" answerable months later.
* **A promotion is what the post-deploy gate says it is.** The deploy script
  reporting success is not the end of the transition; the controller only records
  `PROMOTED` after `verify`, `health`, `identity`, `artifact` and `ws` all pass on
  the host that is now serving it. A failed gate leaves the candidate in
  `ROLLBACK_REQUIRED` with the failing probe named in `post-deploy.json`, and the
  release that is running is whatever the host's own manifest says — never
  assume the old one came back by itself.

Promotion takes a fresh pre-deploy backup and **rehearses it** (restore into a
scratch root, ten checks) before the symlink flips, because the deployment
verification requires the newest backup to have been rehearsed and taking a new
one would otherwise invalidate the check the promotion is measured against. A
**rollback** takes a `pre-rollback` backup for the same reason and now rehearses
that one too — it did not, so a correct rollback reported one red verification
check (`rehearsed=…-pre-deploy-…` vs `newest=…-pre-rollback-…`), which is the
same defect as the promotion's, found the same way.

## 15. Refusals (the negative cycle)

The negative cycle is executed, not inspected:

```bash
python3 docs-for-user/phase33-cicd/rc_negative.py     --artifact <a real wheel> [--case <name>] [--evidence <path>]
```

It builds five invalid candidates from a valid one in a scratch directory (never
in the checkout) and requires the real tooling to refuse each: a planted private
key, an artifact missing a required file, an artifact carrying two builds, a
version that disagrees with the metadata, and a candidate whose required gates
are red. A case that is *not* refused fails the suite, so a scanner that stopped
working, a contract check that was weakened, or a verdict that stopped failing
closed all show up as a negative-test failure rather than as silence.

The CI side of the same property is the pipeline's own fail-closed stage: a run
whose required gate fails ends `NOT_RELEASABLE`, and the job exits non-zero.

**Last executed:** 2026-10-07T22:12Z against the certified build of
`1.6.11-f3da23a-ci37558387881` — **5/5 refused**, each by the tool that owns the
check (`rc_contract.py` ×3, `rc_scan.py` ×1, `rc_evidence.py` ×1, the last one
with `blocking=[…six named gates…]`). Evidence:
`evidence/phase33-runtime/negative/negative-tests.json`.

## 16. Failure injection on the deployment host

```bash
python3 docs-for-user/phase33-cicd/rc_faults_host.py --layer host --rc <APPROVED rc-id> --case <name>
bin/prod.sh faults --list            # the host's own eleven cases
bin/prod.sh faults --phase inject --case <name>
```

A case is only recorded when all five steps happen: the fault is **injected**
into the running deployment, the host **detects** it (an alert, a failed probe),
the release process **blocks** on it, the deployment **recovers**, and the
recovery is **validated** afterwards. A case that is detected but does not block,
or that blocks but leaves the host unhealthy, is not a pass. The release
controller's own `MO7_FAULT_RC_ID` must name an `APPROVED` candidate — a
candidate that is already the current release cannot be the target of the
rollback case, because a rollback refuses to "return" to where it already is.
The target must therefore be a second, valid candidate: on 2026-10-07 the
certified release was live, and the earlier run `37554841809` (`6d7603c`) was
re-ingested, validated 16/16 and approved to serve as the injection target.

**Last executed:** 2026-10-07T22:39Z against the certified release —
**local 4/4, host 6/6**, every host case `detected/blocked/recovered/validated =
true`. The post-deploy case records `promote exit=1 state=ROLLED_BACK
rollback=rolled back by the deployment performed_by=deployment automatic=True
operator_rollback_exit=None fresh=True`, with the marker and the symlink both back
on the certified release, and the host still passes `verify --quick` 107/107 with
the injected residues removed afterwards.

## 17. Rebuilding the release environment after a recycle

This sandbox is recycled periodically; the workspace is snapshotted, `/tmp` and
the home-directory browser caches are not, and the plumbing that lives outside
the checkout (`/home/user/mo7-cicd`, `/home/user/mo7-prod`) can disappear. The
checkout is the source of truth, so recovery is a rebuild, in this order:

1. **Check the repository first**, and know that a recycle can leave it in
   either shape:
   * **Refs older than the disk** (recycle 2): the worktree holds work the refs do
     not. `git fetch origin`, compare the working tree with the recorded head *by
     content* (every tracked path hashed), take a named stash as the safety copy
     (`git stash push -u`), then fast-forward — never a reset. The two recoveries
     of 2026-10-07 cost no work and are recorded in the commit messages.
   * **Refs newer than the disk** (recycle 3, P33-M31): `.git` is a fresh clone
     (`git reflog` shows `clone: from https://…` then `checkout: moving from
     main`) while the worktree still has the session's files. Recover with
     `git fetch origin` then `git reset --mixed origin/<branch>`: the index and
     the branch pointer move to the remote tip and **the worktree is not touched**,
     so nothing is lost. Take the snapshot *before* the reset
     (`tar --exclude=.git --exclude=node_modules -czf /home/user/recycleN-worktree-snapshot.tgz .`)
     and read the resulting `git status` file by file — it should be only the
     edits you remember making. In recycle 3 those were the initial
     `knowledge-linked-folders` and `watching-browser` test edits. They were
     preserved as the core of the P33-M28 patch; the later disposable-copy stress
     run exposed two more timing-budget sites (`co-writer-lazy-notebook` and
     `settings-unified-draft`), which were added to the unapplied diagnostic patch.
     Everything else matched the remote tip exactly, including this document.
2. **Controller**: `rebuild_controller.sh` — `/home/user/mo7-cicd` with `venv`
   (build, wheel, setuptools), `gate-venv` (ruff + pyyaml), `fonts/` + the offline
   `font-mock.cjs` (`npm pack geist@1.7.2 @fontsource/lora@5.3.0`; the mock serves
   those payloads because Google Fonts is TLS-blocked), and a worktree wheel of the
   commit the host is bootstrapped from (`rc_build.sh`).
3. **Chromium**: `rebuild_chromium.sh` — `npm install @sparticuz/chromium@153.0.0`
   and lay the result out twice, because two consumers want different shapes:
   `$MO7_CHROMIUM_HOME/chrome-linux/chrome` (+ `chromium-deps/**/lib` on
   `LD_LIBRARY_PATH`) for `prod_browser.sh`, and
   `~/.cache/ms-playwright/chromium-1200/chrome-linux64/chrome` +
   `INSTALLATION_COMPLETE` for Playwright itself. Two details cost build cycles and
   are worth knowing: the package is **ESM-only** (`"type": "module"`, an
   `exports` map and no `main`, so `require()` of it fails — import
   `build/index.js`), and the bundled archives are **brotli** (`bin/al2023.tar.br`
   is a brotli stream, not zlib). `chrome --version` must print
   `Chromium 153.0.8010.0` before the matrix is trusted.
4. **Deployment host**: `rebuild_host.sh` runs the sequence below. It is not
   decorative, and two steps exist because the obvious order does not work on a
   host that has never been configured:

   ```
   prod_bootstrap.sh <wheel> --release-id <id> --commit <sha>   # stages release 1
   prod.sh promote --release <id> --skip-smoke                  # see below
   prod.sh init-config                                          # needs a current release
   prod.sh ingress start
   prod.sh provision                                            # creates the accounts
   prod.sh promote --release <id>                               # complete gate
   prod.sh session admin                                        # mints the browser session
   prod.sh verify --quick
   check_host_convergence.sh
   ```

   The **first** promotion has to run with `--skip-smoke`: auth is disabled until
   `init-config` writes the settings, and `init-config` needs a current release, so
   a virgin host cannot pass the post-promote smoke — the smoke authenticates an
   account that cannot exist yet. Run without the flag and the deployment flips the
   symlink, fails the smoke, dies, and writes neither `run/current-release.json`
   nor its deployment-log line; `verify --quick` then reports four failures
   (deployment-log identity ×3 and marker/symlink mismatch) against a release that
   is in fact serving — observed on 2026-10-07, which is why this script exists.
   The same release is re-promoted with the complete gate four steps later, and
   every subsequent promotion — including the certified one — runs the full gate.
   `session` is what mints the browser harness's storage state; without it the
   whole Chromium matrix fails to launch (64 tests in the 2026-10-07 rebuild).
   The bootstrap installs the harness from the checkout — so a host rebuilt from
   a stale checkout runs stale instruments. That is not cosmetic: a host whose
   `prod_promote.sh` predates the marker-ordering fix will promote a release and
   leave the contract naming the previous one, which fails every identity check.
   Re-copy the harness (`prod_*.py`, `prod_*.sh`, `ingress_tls_proxy.js`,
   `playwright.production.config.ts`) after any recycle or checkout update — and
   note that the operational entry points are **copies** too: `bin/prod_promote.sh`,
   `bin/prod_deploy.sh` and `bin/prod_rollback.sh` are what `bin/prod.sh` runs, so
   patching `harness/` alone changes nothing. Copy both (they are installed from
   the same repository files) and keep the modes executable.

## 18. Claim → instrument → evidence

| Claim | Instrument | Evidence |
| --- | --- | --- |
| The commit builds a valid artifact | `.github/workflows/release.yml` | `evidence/ci/<run-id>/{release,artifact,test-summary,security-summary}.json` |
| The artifact is what it says it is | `rc_contract.py`, `rc_scan.py` | `artifact-contract.json`, `artifact-scan.json` in the same directory |
| Two builds of one commit agree | `rc_manifest.py` | `build-report.json` → `reproducibility` (`unexplained: []`) |
| The candidate may be promoted | `rc_evidence.py` | `promotion.json` → `verdict`, `blocking` |
| The frozen candidate did not move | `rc_release.py verify` | `state/<rc-id>.json` → `verify` |
| What is deployed, and from what | Phase 32 host | `<prod>/run/current-release.json`, `bin/prod.sh identity` |
| The release was healthy after promotion | `bin/prod.sh verify`, `health`, `identity`, `artifact`, `ws` | `rc/<rc-id>/evidence/post-deploy.json` |
| A rollback worked without a human | `rc_release.py rollback`, host faults | `rc/<rc-id>/evidence/rollback.json`, `faults-host.json` |
| No browser regression | `prod_browser.sh` | `web/test-results/` plus the reporter summary (64 passed / 9 skipped) |
| The pipeline refuses invalid input | `rc_negative.py`, the CI fail-closed stage | `negative/` in the work directory, the run's own verdict |

