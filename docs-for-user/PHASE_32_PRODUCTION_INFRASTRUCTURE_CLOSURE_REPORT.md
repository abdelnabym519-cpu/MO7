# MO7 — Phase 32 Production Infrastructure Closure Report

Production infrastructure for the certified MO7 release, built, operated and
certified on this host, including an executed deployment, an executed rollback
and eleven executed failure injections.

Everything below was executed against the deployment named in §2 and the
evidence each claim rests on is named with it; the evidence files live under
`/home/user/mo7-prod/evidence/`. Nothing here is inferred from a document: the
runtime evidence was produced in this phase, on this host.

---

## 1. Executive summary

Phase 32 turned the certified release into an operating production deployment and
then tried to break it.

The deployment runs the released artifact — a wheel built from source commit
`58f548b` — on a dedicated production host (`/home/user/mo7-prod`) that is
completely separate from the development checkout: its own release trees,
data root, secrets, ports, credentials, TLS material, backups and evidence. Four
long-running programs (API, frontend, scheduler, validation ingress) are owned by
supervisord with a host watchdog, storage and database integrity are swept on a
schedule, alerts are evaluated against eighteen documented rules, and a
deployment pipeline stages → smoke-tests → backs up → promotes → restarts →
health-gates → records every release, with automatic rollback when the health
gate fails.

What was demonstrated rather than asserted:

* **Deployment** of a new release (`1.6.12-58f548b`) end to end, with a measured
  outage window of **11 377 ms** and the application's own startup migrations
  recorded in the data inventory.
* **A refused deployment.** A deliberately broken build (`1.6.13-58f548b`: the
  readiness probe raises) was staged, failed its pre-promote smoke test with an
  honest `500` and no stack leak, was removed, and left the previous release
  serving. The deployment contract was restored automatically.
* **A rollback** (`1.6.12-58f548b` → `1.6.11-58f548b`) with a pre-rollback
  backup, a restart, nine smoke checks and the contract and release marker moved
  back in step.
* **A restore**, rehearsed from the newest backup every time: 10/10 checks,
  including booting a scratch backend against the restored root and reading
  restored account, notebook and reading data.
* **Eleven named failure injections**, 82/82 assertions, with the expected alert
  firing and clearing in each case and the deployment healthy at the end.
* **The full validation battery** from the final frozen state: infrastructure
  **107/107**, application validation **41/41** (+6/6 post-restart), security
  matrix **38/38** at the TLS origin and **38/38** against the API's private
  address, artifact wire probe **29/29** on both surfaces, browser suites
  **64 passed / 0 failed / 9 fixture-gated skips**.

The phase also found and fixed seven validation-instrument defects (never by
weakening an assertion, never by changing product behaviour) and survived a
second host recycle, which destroyed the runtime and required a rebuild from the
committed toolchain — documented in §26 because it affects how the artifact
identity chain reads.

Two things this environment cannot provide are recorded as external dependencies
rather than faked: a CA-issued certificate for the public origin (TLS terminates
at the platform edge, outside this host) and off-host alert delivery. Zero-
downtime deployment is **not** claimed: the restart window is measured, in every
deployment record.

**Final status: `PHASE 32 READY`.**

---

## 2. Production identity

| Item | Value |
| --- | --- |
| Environment | `production` (`PROD_ENVIRONMENT`) |
| Production root | `/home/user/mo7-prod` |
| Release id | `1.6.11-58f548b` (running) |
| Version | `1.6.11` |
| Source commit | `58f548b6131c079b169861edf438a59c8cdd5f3c` |
| Artifact | `deeptutor-1.6.11-py3-none-any.whl` |
| Artifact SHA256 | `45d24a7f63600b102b61e12632a602b78f5d238718c5774d9ec7722d760324c7` |
| Public origin | `https://3782-ibs8yw62kwbj7yqjc8pvh.e2b.app` (platform TLS edge) |
| Validation TLS origin | `https://127.0.0.1:8443` (self-signed, harness instrument) |
| API | `127.0.0.2:8001` (uvicorn, `--no-access-log` off; access log on) |
| Frontend | `0.0.0.0:3782` (Next.js standalone `server.js`) |
| Scheduler | `harness/prod_scheduler.py` (metrics, alerts, retention) |
| Process manager | supervisord 4.x from `ops-venv`, plus host watchdog |
| Accounts | `prod-admin`, `prod-tenant-a`, `prod-tenant-b` (`secrets/credentials.json`, 0600) |
| Secrets | `secrets/secrets.env` (0600), `secrets/credentials.json` (0600), `home/data/system/auth/auth_secret` (0600) |
| Release marker | `run/current-release.json` (release, artifact, hash, commit, promoted_at) |
| Deployment records | `run/deployments.log` (3 records: deploy N, deploy N+1, rollback) |

Identity is verified by the deployment, not asserted in prose:
`bin/prod.sh identity` → `traceable=True release=1.6.11-58f548b
artifact=45d24a7f63600b10… failures=0`, over fourteen checks that include the
artifact's hash, the release virtualenv's installed version, the materialised web
bundle compared file-by-file against the artifact's packaged bundle (3 188
identical, 0 mismatched, 0 missing), and the working directory of each running
process.

Evidence: `evidence/production-verify.json`, `evidence/final-loop.log`,
`run/current-release.json`, `run/deployments.log`.

---

## 3. Capability inventory

The prompt's capability list, each item with its demonstrated state. `AVAILABLE`
means it exists here, is configured in the deployment contract, and was exercised
in this phase; `PARTIAL` means it exists with a documented limitation;
`NOT AVAILABLE — EXTERNAL INFRASTRUCTURE DEPENDENCY` means this environment
cannot provide it and no substitute was fabricated.

| Capability | State | Basis |
| --- | --- | --- |
| Deployment automation | AVAILABLE | `bin/prod_deploy.sh`: VERIFY → STAGE → SMOKE → PUBLISH → BACKUP → PROMOTE → RESTART → HEALTH → SMOKE → RELEASE; used for both releases |
| Rollback automation | AVAILABLE | `bin/prod.sh rollback --to <id>`; executed, `current` flipped back, contract and marker rewritten |
| Backup automation | AVAILABLE | `prod_backup.py`: SQLite backup API per store (sha256 + `integrity_check` per store), configuration copy, storage archive; policy-driven retention |
| Restore | AVAILABLE | `prod_restore.py`: hash + integrity verification, scratch restore, scratch backend boot, authenticated data read; rehearsed 10/10 |
| Database strategy | AVAILABLE (SQLite, no server) | 24 stores live, 0 unhealthy; per-store integrity sweep in the collector; WAL-safe snapshots |
| Database server / failover | NOT AVAILABLE — EXTERNAL INFRASTRUCTURE DEPENDENCY | no PostgreSQL/Redis/MySQL server exists here (`evidence/production-fingerprint.json`) |
| Storage durability | AVAILABLE | storage roots per tenant, writable check in health, storage archived in every backup, read-only fault injection proves the failure and recovery path |
| Secrets management | AVAILABLE | three secret stores with 0600 modes and a documented required-secret list; `secret-missing` injection proves the start-script refusal |
| Process supervision | AVAILABLE | supervisord with `autorestart`, `startsecs`, `stopwaitasgroup`, `rlimit_nofile=65536`, plus the host watchdog for supervisord itself |
| Resource limits | PARTIAL | `rlimit_nofile=65536`, single worker per program, request-body and upload ceilings enforced by the application; no cgroup/container memory or CPU quotas (no container runtime here) |
| Network boundary for the API | PARTIAL | API bound to `127.0.0.2` (the platform publishes only `0.0.0.0` binds) and `/docs`, `/redoc`, `/openapi.json` are refused on the public origin; the platform's port bridge still mirrors every listening port, so this is a boundary, not a firewall |
| Real edge TLS | NOT AVAILABLE — EXTERNAL INFRASTRUCTURE DEPENDENCY | production TLS terminates at the platform edge (`https://3782-…e2b.app`); the sandbox cannot reach its own public hostname over TLS (`SSLZeroReturnError`), and the local 8443 ingress is self-signed and labelled as a validation instrument only |
| DNS / certificate provisioning | NOT AVAILABLE — EXTERNAL INFRASTRUCTURE DEPENDENCY | owned by the platform edge |
| Observability | AVAILABLE | collector with probes, process states, database integrity, disk, backups, secret modes, log errors; series in `run/metrics/series.ndjson` |
| Alerting | AVAILABLE (local) | 18 rules with condition, severity, response, owner, verification; fired and cleared under injection; no off-host paging transport |
| Alert delivery off-host | NOT AVAILABLE — EXTERNAL INFRASTRUCTURE DEPENDENCY | no paging/email transport in this environment |
| Zero-downtime deployment | NOT CLAIMED | restart window measured at 11 377 ms; recorded per deployment |
| Migration safety | AVAILABLE (process) | BACKUP before PROMOTE, health gate after, automatic rollback on failure; the N+1 deployment's `default-adoption` migrations appear in the data inventory and nothing else changed |
| Log aggregation | PARTIAL | per-program logs under `run/supervisor/`, rotated by supervisord (20 MB × 5); no external log store |
| Browser coverage | AVAILABLE | Chromium 153.0.8010.0 from the vendored `@sparticuz/chromium` build (Playwright CDN unreachable), 73 tests in 12 files, 64 passed / 9 fixture-gated skips |

---

## 4. Topology and process model

```
        platform edge (TLS, DNS)              <- external: not operated from this host
                 |  https://3782-<sandbox>.e2b.app
                 v
   published port 3782 (0.0.0.0)     frontend: Next.js standalone (node server.js)
   private  port 8001 (127.0.0.2)    backend:  uvicorn deeptutor.api.main:app (1 worker)
   validation port 8443 (0.0.0.0)    harness TLS ingress (self-signed, validation only)
   smoke port 8099 (127.0.0.2)       staged release under test during a deployment
                                       ^ additionally reached by the platform's port
                                         bridge, which mirrors every listening port
```

Process ownership: supervisord owns `backend`, `frontend`, `scheduler` and
`ingress-validation` (the last deliberately `autostart=false`); the watchdog owns
supervisord's liveness and reclaims programs left by a dead supervisord
generation — and only those it can prove supervisord started (§25, P32-H1).
Nothing runs as root: supervisord and the release inherit the deployment user.

Current state: `backend RUNNING pid 15327`, `frontend RUNNING pid 13228`,
`scheduler RUNNING pid 12519`, `ingress-validation RUNNING pid 14046`,
`watchdog running pid 12440`, `current -> releases/1.6.11-58f548b`, health
`healthy True`.

Evidence: `evidence/final-loop.log`, `evidence/production-fingerprint.json`,
`run/supervisord.log`.

---

## 5. Production dependency graph

Nodes are the components that must exist for one authenticated request to be
served and accounted for; edges are hard dependencies (a failed edge is an
outage of everything downstream).

```
platform edge ──> frontend(3782) ──> backend API(127.0.0.2:8001) ──> data root
      │                                     │                          ├─ per-user SQLite stores
      │                                     │                          ├─ workspace/storage tree
      │                                     └──> scheduler ──> metrics ──> alerts
      └──> (external) DNS, TLS cert                                           │
                                                                              └─> run/alerts/*
supervisord ──owns──> {backend, frontend, scheduler, ingress-validation}
watchdog    ──owns──> supervisord           (cycle: watchdog restarts supervisord,
                                             supervisord restarts programs)
release tree ──is──> current ──> release wheel + web bundle + venv
backups      ──derive from──> data root (SQLite API snapshots + storage archive)
```

Cycles are deliberate and reciprocal by design, and each is bounded:

* watchdog ⇄ supervisord (each restarts the other's absence);
* scheduler ⇄ backend (the scheduler probes the API; the API's health does not
  depend on the scheduler, so the cycle cannot deadlock the request path);
* deploy ⇄ health gate (a failed gate flips `current` back, which restarts the
  programs the gate just measured).

The graph was exercised edge by edge in the fault injections: each injection
removes one node or one edge and the harness asserts both the failure it causes
and that removing the injection restores the whole graph.

---

## 6. Deployment

Two releases were deployed through the pipeline in this phase.

| Field | N → first promotion | N+1 |
| --- | --- | --- |
| Release | `1.6.11-58f548b` | `1.6.12-58f548b` |
| Artifact SHA256 | `45d24a7f…` | `b75f1a01…` |
| Staged smoke | `/health/ready` 200 on 8099 | `/health/ready` 200 on 8099 |
| Pre-promote backup | `20261006T132248Z-pre-deploy` | `20261006T132548Z-pre-deploy` |
| Outage window | n/a (first start) | **11 377 ms** (probe every 200 ms) |
| Health gate | ready + frontend serving | ready + frontend serving |
| Post-promote smoke | 9/9 | 9/9 |
| Recorded in `deployments.log` | yes | yes |

The staging steps are real: the release is built in a scratch directory, its own
virtualenv is populated from the artifact, the web bundle is materialised from
the wheel, and the staged release is *started on a scratch port and required to
report ready* before the release directory is published and made immutable.

A refused deployment is a no-op on the host: the staging directory is removed,
`current` never moves, and the contract is restored from the running release's
manifest. §9 shows that path with a genuinely broken build.

Evidence: `evidence/rebuild-deploy-drill.log`, `run/deployments.log`,
`run/deploy-*.log`, `run/outage-*.log`.

---

## 7. Data and state graph

Persistent state: per-user SQLite stores (chat history, workspace/library and
reading catalogues, cron and sync stores), the per-user storage tree, the auth
store and secret files, and the runtime markers under `run/`.

The inventory tool walks that graph and records, per entry, the store's tables
and row counts, file hashes and sizes, so "did anything change" is answered by
facts rather than by inspection. It was used six times in this phase:

| Comparison | added | removed | changed | Verdict |
| --- | --- | --- | --- | --- |
| `pre-n1` → `post-n1` (deployment + migrations) | 4 (the app's own `default-adoption` migration records) | 0 | 0 | PASS |
| `pre-rollback` → `post-rollback` (rollback to N) | 0 | 0 | 0 (one volatile counter, §8) | PASS |

The state graph was also exercised across restart, crash, redeploy and restore:

* **restart** — `validate --phase post-restart` 6/6: notebook, library bytes
  (byte-for-byte), reading content, collection and account identity all survive;
* **crash** — the `backend-crash` and `frontend-crash` injections kill the
  processes with SIGKILL and assert that data is unchanged and the service
  recovers;
* **redeploy** — the inventory comparison above;
* **rollback** — the inventory comparison above, plus 9/9 post-rollback smoke;
* **restore** — the rehearsal reads restored notebook and reading data through a
  backend booted against the restored root.

---

## 8. Database and migration safety

Stores are SQLite files; 24 exist on the live host and all pass
`integrity_check` (`health` → `stores=24 unhealthy=0`).

* **Consistent snapshots.** `prod_backup.py` copies each store through the SQLite
  backup API (never a raw file copy of a live WAL database), then records a
  sha256 and runs `integrity_check` on the snapshot.
* **Coverage, not counts.** The verifier asks the question a backup policy has to
  answer — "is every store that existed when this backup was taken in it?" — and
  reports stores created afterwards separately.
* **Migration safety is the deployment sequence.** BACKUP (pre-migration control)
  → PROMOTE → HEALTH → SMOKE, with automatic rollback when the gate fails. The
  N+1 deployment ran the application's startup migrations; the inventory shows
  four added `.runtime/data-migrations/` records for the two provisioned users
  and nothing lost or altered.
* **A false positive was found and fixed in the instrument, not in the check.**
  The inventory compared raw table counts, so an authentication bookkeeping table
  that the application prunes and clears by design made a healthy rollback look
  like data change. The table is now classified volatile with its before/after
  values reported and the exit status unaffected; every other store, table, file
  and document is still compared for equality, and disappearances still fail
  (§25, P32-H6).

Evidence: `evidence/data-inventory-*.json`, `evidence/rebuild-deploy-drill.log`,
`evidence/restore-*.json`.

---

## 9. Promotion gate (§35)

The gate is the pipeline itself; here is the executed sequence, including the
refusal.

| Step | Result |
| --- | --- |
| VERIFY the artifact | sha256 recomputed and compared with the declared value; wheel metadata version matched; source commit present in the checkout |
| STAGE | release tree built in scratch, own virtualenv populated from the artifact, web bundle materialised |
| SMOKE the staged release | started on scratch port 8099 and required to answer `/health/ready` |
| **Broken build (`1.6.13-58f548b`, readiness raises)** | staged release answered `500 {"detail":"The request could not be completed.","type":"RuntimeError"}`; after 63 s the smoke gave up; **`FATAL: pre-promote smoke failed`**; staging directory removed; deploy exited `1` |
| Contract after the refusal | `PROD_RELEASE_ID` restored from `1.6.11-58f548b` to `1.6.12-58f548b` — the release that was actually running |
| Service during the refusal | the previous release kept serving: `identity` → `traceable=True release=1.6.12-58f548b failures=0`, health `healthy True` |
| BACKUP | pre-promote consistent snapshot |
| PROMOTE | atomic symlink flip of `current` |
| RESTART + HEALTH | programs restarted, readiness and frontend required; automatic flip-back if either fails |
| SMOKE | 9/9 post-promote checks |
| RELEASE | recorded with artifact hash, commit and measured outage |

Two gate properties are worth stating explicitly because they are what makes the
gate meaningful: it refuses on evidence from the *staged* release before touching
production, and a refusal leaves no trace on the running deployment other than a
log record.

Evidence: `evidence/rebuild-deploy-drill.log` (VERIFY→SMOKE→`FATAL`→contract
restore), `run/deployments.log`.

---

## 10. Rollback

Executed: `bin/prod.sh rollback --to 1.6.11-58f548b`, exit `0`.

1. pre-rollback backup `20261006T132801Z-pre-rollback-1.6.11-58f548b`
   (17 databases, 4 config files, storage archive, all `integrity=ok`);
2. `current -> 1.6.11-58f548b (was 1.6.12-58f548b)`;
3. programs restarted through supervisord;
4. health restored; 9/9 post-rollback smoke checks passed;
5. rollback recorded in `run/deployments.log` (`previous=1.6.12-58f548b`);
6. contract identity and `run/current-release.json` moved back in step;
7. data inventory `pre-rollback` → `post-rollback`: added 0, removed 0,
   changed 0 — no store, table, file or document lost or altered.

A rollback here is a release flip plus a restart, not a data restore: it does not
undo migrations. That is why the deployment takes a backup *before* promoting and
why the data comparison is the rollback's acceptance criterion. Rollback of the
deployment is proven; rollback of an arbitrary schema migration is not claimed —
the in-place migration path for that is the pre-promotion backup plus a restore
(§7 and §12), and the application's migrations were additive in this phase.

Evidence: `evidence/rebuild-deploy-drill.log`, `evidence/data-inventory-pre-rollback.json`,
`evidence/data-inventory-post-rollback.json`.

---

## 11. Backup and restore certification

Backups: 6 in this phase, each verified true, each with per-store hashes and
`integrity_check` results, configuration copies and a storage archive. Retention
follows `etc/backup-policy.json`.

Restore rehearsals: three, all **10/10**, each from the newest backup:

`restore-20261006T132419Z-rebuild-baseline`, `…T133736Z-final`,
`…T134442Z-final` (latest: `evidence/restore-20261006T134442Z-final-1791294287.json`).

The rehearsal is a restoration, not a copy check: it verifies the manifest hash,
re-hashes and integrity-checks every snapshot database, restores into a scratch
target, boots a backend against the restored root on a free port, authenticates a
restored account against the restored data, reads restored notebook and reading
data, and stops the scratch backend. It never writes to the live root, and
`prod.sh verify` refuses to certify a deployment with no passing rehearsal
record.

A backup that has not been restored is not certified — which is why the newest
backup, and not an arbitrary one, is the one rehearsed each time, and why the
verifier checks that the rehearsed backup *is* the newest.

Evidence: `evidence/restore-*.json`, `evidence/final-loop.log`, `backups/index.ndjson`.

---

## 12. Storage durability

Tenant storage lives outside the release tree (`home/data/users/<uid>/user/…`), so
a release flip cannot touch it; the `current` symlink and the data root are
siblings by construction. Health reports `storage writable=True` and the
collector watches the mode of the data root.

The failure path is exercised rather than assumed: the `storage-readonly`
injection made a tenant's storage directory read-only, asserted that an upload
fails as a sanitised `500` with no stack trace on the wire and no partial file
left behind, that the rest of the deployment keeps serving, and that restoring
the mode restores uploads. Storage is included in every backup as a gzip archive
with a recorded hash.

Evidence: `evidence/faults/storage-readonly-*.json`, `evidence/production-faults.json`.

---

## 13. Secrets management

| Secret | Location | Mode | Proven behaviour |
| --- | --- | --- | --- |
| Auth signing secret | `home/data/system/auth/auth_secret` | 0600 | `secret-missing` injection: the start script refuses to start and logs `FATAL: required secret missing or empty … (auth is enabled; refusing to regenerate it)` — the deployment does not silently mint a new secret and log everyone out |
| Deployment accounts | `secrets/credentials.json` | 0600 | the three accounts authenticate; the throttle probe uses a throwaway account so no real account is locked |
| Ops secrets | `secrets/secrets.env` | 0600 | mode asserted by the verifier and the collector |

`PROD_REQUIRED_SECRETS` in the contract names what must exist before a release may
start; `harden_permissions` on the host re-applies the documented modes. No secret
value appears in any evidence file.

Evidence: `evidence/faults/secret-missing-*.json`, `evidence/production-verify.json`.

---

## 14. Publishing surface: `/docs`, `/redoc`, `/openapi.json`

Decision: **the public origin refuses the API documentation surface; the API's own
address serves it.**

| Probe | Public origin (TLS ingress, host = public host) | API address (`127.0.0.2:8001`) |
| --- | --- | --- |
| `/docs` | not served (`307` to the login gate) | served (`200`) |
| `/redoc` | not served (`307`) | served (`200`) |
| `/openapi.json` | not served (`307`) | served (`200`) |
| `/api/docs` | not served | `404` |

Two mechanisms make that true: the API binds `127.0.0.2` rather than `0.0.0.0`
(the platform publishes `0.0.0.0` binds only), and the frontend origin's routing
gates the path. The API's documentation remains available to an operator on the
private address — the boundary is exposure, not capability.

Residual exposure, recorded rather than hidden: the platform's port bridge
mirrors every listening TCP port on the sandbox, so a firewall-class boundary
cannot exist here (§27, P32-K2).

Evidence: `evidence/production-security-tls.json`, `evidence/production-security-loopback.json`.

---

## 15. Authentication and session production behaviour

| Property | Observed |
| --- | --- |
| Cookie flags | `HttpOnly`, `Secure`, `SameSite=None`, `Path=/`, `Max-Age=86400` |
| Host-only | no `Domain` attribute |
| Cacheability | auth responses `no-store` |
| Token forgery | every forged or malformed token refused (`401`, no framework internals) |
| Sign-out | deleting an account revokes its live session |
| Throttling | repeated failed sign-ins throttled (`429` with `Retry-After`), without locking an unrelated account |
| Enumeration | a wrong password and an unknown account are indistinguishable |
| RBAC | non-admin refused the admin surface and account creation |
| Tenancy | every cross-tenant attempt refused; refusals disclose no other tenant's data and leave the owner's objects unchanged |
| Password storage | bcrypt (cost ~270 ms per verification per the source) |

The Phase 27 harness runs unchanged against production as the `[tls]` pass of the
security matrix (38/38), with the same suite re-run against the API's private
address (38/38).

Evidence: `evidence/production-security-tls.json`, `evidence/production-security-loopback.json`.

---

## 16. Failure injections

Eleven named cases, each injecting a real fault and asserting the observable
consequences, the recovery and the deployment's health afterwards. All eleven ran
in one `--keep-going` run against the certified release: **11/11 cases, 82/82
checks, deployment healthy after = true**.

| Case | Injected fault | What was asserted |
| --- | --- | --- |
| `backend-crash` | API process killed with SIGKILL | readiness fails then recovers, data unchanged, supervisor restarts it |
| `frontend-crash` | frontend process killed with SIGKILL | the published port stops accepting, then serves again |
| `supervisord-crash` | process manager killed with SIGKILL | the watchdog restarts it and re-adopts the programs |
| `watchdog-crash` | host watchdog killed | the watchdog supervisor path restores it without touching the programs |
| `ingress-outage` | validation ingress stopped | TLS-origin probes fail, the deployment itself keeps serving, restarting restores the probe |
| `storage-readonly` | tenant storage made read-only | upload fails as a sanitised 500, no partial file, service continues, restore returns uploads |
| `secret-missing` | required auth secret removed | the start script refuses to run (`FATAL … refusing to regenerate it`), then recovers when restored |
| `config-malformed` | deployment contract corrupted | tooling refuses to act on it instead of acting wrongly; restoring the contract restores operation |
| `database-corruption` | a *copy* of a production store corrupted | the collector and the `database-integrity` alert identify exactly the damaged copy; no production store touched |
| `tls-cert-near-expiry` | validation certificate replaced with a one-day certificate | `cert-expiry-validation-ingress` fires; restoring the certificate clears it |
| `disk-pressure` | a 2 MiB filesystem filled | the disk rule fires and the deployment keeps serving; the mount is removed afterwards |

Each case writes `evidence/faults/<case>-<epoch>.json`; the aggregate is
`evidence/production-faults.json` and the full log `evidence/rebuild-faults.log`.
A run lock (`run/faults.lock`) prevents two runs from fighting over one
deployment, and the harness re-checks the baseline before each case.

Evidence: above.

---

## 17. Observability and alerting

Collector (`harness/prod_metrics.py`) samples: supervisord program states (and
restarts per hour), liveness/readiness/frontend/ingress probes, database store
count and integrity, disk usage, backup inventory and ages, secret modes and
sizes, and backend/frontend log error counts. Series accumulate in
`run/metrics/series.ndjson`; each sample is one JSON document on disk.

Eighteen alert rules (`etc/alerts.json`), each with condition, severity,
documented response, owner (`mo7-platform-oncall`) and a verification path — and
each verified in this phase either by a fault injection or by the healthy-state
assertion in the verifier.

| Rule | Severity | Condition |
| --- | --- | --- |
| `process-down-backend`, `process-down-frontend`, `process-down-scheduler` | critical | program not `RUNNING` |
| `process-restarts-high` | warning | > 3 restarts in an hour |
| `probe-liveness`, `probe-readiness`, `probe-frontend`, `probe-ingress` | critical | probe not 200 |
| `database-integrity` | critical | any store fails `integrity_check` |
| `disk-usage-high` | warning/critical | disk above the documented thresholds |
| `backup-stale` | critical | newest backup older than the policy window |
| `backup-verification-failed` | critical | a backup fails its own verification |
| `secret-permissions` | critical | a secret is not 0600 |
| `secret-missing` | critical | a required secret is absent or empty |
| `release-identity` | critical | `current` and the release marker disagree, or the release does not resolve |
| `watchdog-down` | warning | the host watchdog is not running |
| `ingress-validation-down` | warning | the validation ingress is not running (expected outside a validation window) |
| `cert-expiry-validation-ingress` | warning | the validation certificate is < 21 days from expiry |
| `cert-expiry-production-edge` | warning | reports `None` with the documented reason: the edge certificate belongs to the platform and is not observable from inside the host |
| `application-errors` | warning | > 5 ERROR/CRITICAL/Traceback/FATAL lines in the backend log, counted after the newest fault-drill marker (§25, P32-H7) |
| `metrics-stale` | warning | the collector has not written a sample recently |

State now: **18 rules, 0 firing, 0 critical**; `health` → `healthy True`; disk
37.34 %; 24 stores, 0 unhealthy; 6 backups, all verified.

Escalation off-host (paging, an external monitoring system, an on-call rota) is an
external dependency: the deployment produces the signal, it does not deliver it
off-host.

Evidence: `evidence/final-loop.log`, `run/alerts/alerts.json`,
`run/metrics/latest.json`.

---

## 18. Resource limits

| Limit | Value | Where enforced |
| --- | --- | --- |
| Open files per program | 65 536 | supervisord `rlimit_nofile` (all four programs) |
| API workers | 1 per API process | release start script (uvicorn `--workers 1`) |
| Request body ceiling | application-configured (220 MB proxy ceiling in `next.config.js`) | application + frontend proxy; oversized requests are refused without a 5xx (asserted in the artifact probe) |
| WebSocket frame size | 43 341 141 bytes | uvicorn `--ws-max-size` in the release start script |
| Keep-alive | 300 s | uvicorn `--timeout-keep-alive` |
| Stop/restart grace | 20 s (`PROD_STOP_WAIT`) | supervisord `stopwaitsecs` |
| Upload ceiling | application-enforced | `oversized upload is rejected without a 5xx` (security matrix) |
| Memory/CPU quotas | **not available** | no container runtime or cgroup delegation in this environment |

Evidence: `etc/supervisord.d/*.conf`, `evidence/production-security-tls.json`,
`evidence/production-verify.json`.

---

## 19. Performance against the Phase 28 baseline

Measured on the live deployment (backend on its private address, frontend on its
published port, TLS ingress on 8443), 100 sequential keep-alive samples per
route after a warm-up, then a 240-request concurrent burst:

| Route | p50 | p95 | max | Phase 28 baseline (p50 / p95) |
| --- | --- | --- | --- | --- |
| `/health/live` | 2.20 ms | 3.01 ms | 3.49 ms | 2.621 / 3.172 ms |
| `/health/ready` | 2.02 ms | 2.31 ms | 2.96 ms | 2.808 / 3.284 ms |
| `/api/settings` (authenticated) | 8.35 ms | 12.55 ms | 126.40 ms | not measured in Phase 28 |
| frontend `/login` (HTML) | 2.77 ms | 6.34 ms | 14.31 ms | not measured (font fetch blocked in Phase 28) |
| ingress `https /login` | 5.98 ms | 7.23 ms | 7.89 ms | not measured |

Burst: 240 requests, 240 × `200`, 0 errors, 2.703 s → **88.8 rps** with no
request failing. Memory: backend 166 528 KiB and frontend 114 040 KiB before and
after the burst — growth 0 KiB for both, measured from `/proc` for the pids
supervisord owns.

Reading: the health routes are at or below the Phase 28 baseline on a real
deployment that has TLS, authentication and a reverse-proxied frontend in front of
the API, so no production latency regression is established. The authenticated
settings route carries an explicit session-validation cost (bcrypt-backed token
verification) and still lands under 13 ms at p95. No LLM-backed turn was measured
(no provider in this environment); nothing here should be read as a throughput
ceiling for LLM workloads.

Frontend route budgets (same build as the deployed bundle, `npm run perf:check`):
8 of 9 routes within budget; `/learning/reading/[workspaceId]/sessions/[sessionId]`
1129 KB against a 1120 KB budget — the carried Phase 26/28/31 known finding, +1 KB
from the 1128 KB recorded earlier in the same route, unchanged by this phase.

Evidence: `evidence/production-perf.json`, `evidence/final-loop.log`.

---

## 20. Browser validation

Against the deployed production frontend, through the validation TLS ingress, with
the repository's own Playwright suites (`bin/prod.sh browser --project=ui-audit
--project=epub-reader-chromium`):

**64 passed, 0 failed, 9 skipped (3.6 m)**, Chromium 153.0.8010.0 (the vendored
`@sparticuz/chromium` build; the Playwright CDN is unreachable here).

The nine skips are the repository's own fixture gates, not environment failures:
six `multi-worker-turns.audit.ts` cases require `DEEPTUTOR_MULTI_WORKER_E2E=1` and
a deterministic four-worker fixture, and three `turn-lifecycle.audit.ts` cases
require `DEEPTUTOR_TURN_E2E_FIXTURE=1`. Both are integration fixtures that do not
exist in this deployment; they are reported as skipped, never as passed.

Coverage includes the release UI matrix (themes × locales × viewports), settings
navigation, reading citation/location/annotation flows, the library release
matrix and the EPUB reader — the surfaces a production deployment exists to serve,
exercised in a real browser against the running release.

Evidence: `evidence/browser-production.log`.

---

## 21. Validation battery (final frozen state)

Run from the final state, in one loop, with the results below. "Frozen" means the
release, contract, toolchain and configuration did not change between the first
and the last command of the loop.

| Instrument | Result |
| --- | --- |
| `bin/prod.sh security` (TLS origin) | **38/38** |
| `bin/prod.sh security` (API address) | **38/38** |
| `bin/prod.sh artifact` (TLS) | **29/29** |
| `bin/prod.sh artifact` (API loopback) | **29/29** |
| `bin/prod.sh backup --label final` | exit 0 |
| `prod_restore.py` from the newest backup | **10/10** |
| `bin/prod.sh verify` (infrastructure) | **107/107** |
| `bin/prod.sh validate` (application) | **41/41** |
| `bin/prod.sh validate --phase post-restart` | **6/6** |
| `prod_perf.py` | **OK** |
| `prod_datainventory.py --label final` | recorded (11 stores / 15 databases / 11 files / 4 system entries) |
| `bin/prod.sh identity` | `traceable=True failures=0` |
| `bin/prod.sh health` | `healthy True`, 24 stores, 0 unhealthy, disk 37.34 % |
| `bin/prod.sh alerts` | 18 rules, **0 firing** (0 critical) |
| watchdog orphans | `[]` (4 supervised pids) |
| Failure injections | **11/11 cases, 82/82 checks** |
| Browser suites | **64 passed / 0 failed / 9 fixture-gated skips** |

The verifier's check count is not a constant: three of its checks only exist once
a rehearsal record and the storage it covers are present (107 with them, 105
without). This is by design and is recorded here so a later operator does not read
a changing total as a changing deployment.

A confirmation pass was then run from the frozen repository state (commit
`968e1ef`, the harness in `mo7-prod/harness/` byte-identical to the committed
sources): verify 107/107, validation 41/41 and 6/6, security 38/38 on both
surfaces, artifact probe 29/29 on both surfaces, `identity traceable=True
failures=0`, `healthy True`, 18 rules with 0 firing.

Evidence: `evidence/final-loop.log` and `evidence/final-loop-frozen.log`, plus the
per-instrument JSON files named in §29.

---

## 22. Recovery

| Scenario | Mechanism | Demonstrated |
| --- | --- | --- |
| Process crash (API/frontend) | supervisord `autorestart` + health gate | `backend-crash`, `frontend-crash` injections; recovery asserted |
| Process manager loss | host watchdog restarts supervisord and re-adopts programs | `supervisord-crash` injection |
| Watchdog loss | watchdog supervisor path restores it | `watchdog-crash` injection |
| Bad release | pre-promote smoke refuses; `current` untouched | broken `1.6.13-58f548b` refused (§9) |
| Bad release that passed staging | health gate flips `current` back automatically | implemented in the pipeline; the staging gate fired first in this phase |
| Post-release regression | `rollback --to <release>` | executed: N+1 → N, data unchanged (§10) |
| Data loss / corruption | restore from backup | rehearsed 10/10 from the newest backup, three times |
| Secret loss | start script refuses to serve with a regenerated secret | `secret-missing` injection |
| Storage unwritable | sanitised failure, no partial writes, service continues | `storage-readonly` injection |
| Disk pressure | disk rule fires, service continues, operator clears the volume | `disk-pressure` injection |
| Host loss | rebuild from the committed toolchain (`prod_bootstrap.sh`) | executed in this phase, after the recycle (§26) |

---

## 23. Redeployment repeatability

| Evidence | Result |
| --- | --- |
| Two full deployments through the same pipeline | both reached `RELEASE`; identical stage sequence, different artifacts and outage numbers |
| Staging trees are immutable and idempotent | a re-stage of an existing release id is refused/deduplicated; `.bak` leftovers are removed by the pipeline |
| Refused deploy leaves no partial release | staging directory removed; `releases/` contains only published trees |
| Release directories | `1.6.11-58f548b` (current), `1.6.12-58f548b` |
| Deployment records | three, each with artifact hash, commit, previous release and (for deployments) the measured outage |

---

## 24. Zero-downtime assessment

**NOT CLAIMED — not demonstrated.** Restarting the API and frontend through
supervisord costs a real outage window, measured at **11 377 ms** in the N+1
deployment (probe every 200 ms), and recorded in `run/deployments.log` as
`outage_ms`.

Why not zero-downtime here: the deployment runs one API worker and one frontend
process; there is no second instance to take traffic while the first restarts, no
load balancer to drain and no session-free hand-off. Achieving it would need
either a second release running side by side with connection draining, or an
external proxy controlling the switch — the frontend proxy is a Next.js process
and the edge belongs to the platform. Making the claim without that machinery
would be exactly the kind of unverified claim this phase forbids.

What *is* demonstrated is that a deployment never leaves the service down: the
staged release is smoke-tested before promotion, the health gate requires
readiness after the restart, and a failed gate flips the release back.

---

## 25. Findings

Every finding has exactly one classification. Instrument defects were fixed at the
smallest change point; no assertion was weakened, no product behaviour changed and
no test was deleted.

| ID | Classification | Symptom | Root cause | Evidence | Impact | Fix | Validation | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| P32-H1 | HARNESS DEFECT | The host watchdog's orphan sweep killed processes the deployment did not own: a staged release mid-smoke (`exit 143`) and the restore rehearsal's scratch backend (4 failed checks: `ready status=0`, connection refused) | The sweep identified "orphans" by working directory and marker heuristics, which a scratch backend started by the harness also matched | `evidence/rebuild-verify.log` (the failed run), `run/watchdog.log` | False failures in smoke/restore, and a real hazard for any process started outside supervisord | Sweep now requires proof the process belongs to supervisord (`SUPERVISOR_ENABLED=1` or `SUPERVISOR_PROCESS_NAME=` in `/proc/<pid>/environ`) | `prod_watchdog.py orphans` → `{"orphans": [], "supervised": [...]}`; restore rehearsal 10/10 on the same host | FIXED |
| P32-H2 | HARNESS DEFECT | The performance harness could not log in: connection refused on `127.0.0.1:8001` | It dialled loopback, which is the platform's port bridge, not the API (the API binds `127.0.0.2`) | `evidence/production-perf.json` before the fix | No production performance evidence at all | Harness dials `cfg.BACKEND_HOST` — the address every other probe uses | `prod_perf.py` → `production perf: OK` with the full latency table | FIXED |
| P32-H3 | HARNESS DEFECT | Verify reported "newest backup covers every store (13 of 17)" after a healthy deploy | It compared the backup's store count with a live recursive count, so a store legitimately created *after* the backup failed the check | `evidence/rebuild-verify.log` (106/107 run) | A check that goes red on healthy operation trains operators to ignore it | Compare against the stores whose mtime is ≤ the backup time; report newer stores separately | `bin/prod.sh verify` → **107/107** | FIXED |
| P32-H4 | HARNESS DEFECT | Verify reported "the newest backup covers file storage" with `files: 0` on a freshly rebuilt host | Same class as P32-H3: the check required the archive to be non-empty rather than to cover the storage that existed when the backup ran | `evidence/rebuild-verify.log` (first rebuild run) | As P32-H3 | Storage coverage is now measured against the files present at backup time, per the backup policy's `storage_paths` | `verify` 107/107 including that check | FIXED |
| P32-H5 | HARNESS DEFECT | `bin/prod.sh identity` failed on the rebuilt host: "the web bundle matches the artifact's packaged bundle — mismatched `__pycache__/__init__.cpython-311.pyc`" | CPython writes bytecode caches into whichever copy of the package it imports, and a `.pyc` embeds the source timestamp, so two caches of identical source never hash alike | First `identity` run after the rebuild | Traceability reported as broken on a healthy deployment | Bundle comparison skips `__pycache__/*.pyc` (interpreter artifacts); every shipped file is still compared | `identity` → `traceable=True failures=0`; 3 188 files identical, 0 mismatched | FIXED |
| P32-H6 | HARNESS DEFECT | The rollback's data comparison reported `changed: 1` — `login_attempts` rows `5 → 4` — and failed a healthy rollback | The table is the failed-sign-in throttle: `record_failed_login` prunes day-old buckets and `clear_login_throttle` deletes an account's bucket after a successful sign-in (`deeptutor/services/auth.py`), so the count moves in both directions during ordinary operation | `evidence/data-inventory-pre-rollback.json` vs `…post-rollback.json`; `evidence/rebuild-deploy-drill.log` | A healthy rollback looked like data loss — the most dangerous kind of false positive | The table is classified volatile with before/after values reported and no effect on exit status; all other stores, tables, files and documents are still compared strictly, and removals still fail | `--compare pre-rollback post-rollback` → added 0 / removed 0 / changed 0, `PASS` (volatile row reported separately) | FIXED |
| P32-H7 | HARNESS DEFECT | `verify` failed with "no alert is firing on the healthy deployment" on a healthy deployment, because the fault drills' own deliberate errors were still inside the log metric's 400-line window | The metric counts error lines in a log window; a drill writes deliberate errors into that same window and has no way to say "these are accounted for" | `evidence/final-loop.log` (106/107 run) | The deployment could not be certified immediately after a drill; worse, an operator could learn to ignore the alert | The drill appends a boundary marker (`phase32 fault drill complete`) after each case and at the end of the run; the collector counts errors written after the newest marker. Marker text contains no error keywords; errors from the deployment always count | Re-run of `faults --all` (11/11) → metric `recent_error_lines: 0`, alerts **0 firing**; `verify` **107/107** in the following loop | FIXED |
| P32-H8 | HARNESS DEFECT | The rebuilt host could not run the browser matrix: `bin/prod.sh browser` → `harness/prod_browser.sh: No such file or directory` | The bootstrap installed `harness/prod_*.py` but not the shell harnesses, so a rebuilt host silently lacked an instrument | `bootstrap.log` + the failed invocation | Two of the phase's instruments (browser suites) unavailable on a rebuilt host | Bootstrap installs `harness/prod_*.sh` as well | `bin/prod.sh browser --list` → 73 tests in 12 files; full matrix 64 passed | FIXED |
| P32-H9 | HARNESS DEFECT | The browser matrix failed on every test with `libnspr4.so: cannot open shared object file` | The wrapper hard-coded one host layout for the Chromium build and its NSS libraries; the recycle moved them | `evidence/browser-production.log` (first run), exit 127 | Whole browser matrix unusable, with a confusing failure mode | Wrapper discovers the Chromium build and its `libnspr4.so` directory (either layout), and exits `78` with an explicit message when absent | `MO7_CHROMIUM_HOME=/nonexistent` → `FATAL: no Chromium build found`, exit 78; real run → 64 passed | FIXED |
| P32-H10 | HARNESS DEFECT | The security matrix could not run its API-address pass: `ConnectionRefusedError` on `127.0.0.1:8001` | The harness built its base URL from a literal `127.0.0.1` even when the target host was given | `evidence/final-loop-pass1.log` | A whole pass of the security matrix silently unavailable | `raw()` gained a `host` parameter and the base URL is built from the target's host | API-address pass **38/38** | FIXED |
| P32-H11 | HARNESS DEFECT | The performance report printed `backend_growth: 0` while reading RSS from pid files that do not exist in this deployment (`backend: null`) | It read `run/backend.pid`, which supervisord does not write; a null reading was reported as no growth | `evidence/production-perf-before-rss-fix.json` | A memory claim that was not measured | RSS is read from `/proc` for the pids supervisord owns, and the report says `measured: true/false` explicitly, printing "NOT MEASURED" when it cannot | `rss_kib`: backend 166 528 KiB → 166 528 KiB, frontend 114 040 → 114 040, `measured: true` | FIXED |
| P32-K1 | KNOWN FINDING (carried) | The reading-session route bundle is 1129 KB against a 1120 KB budget | Phase 26/28 carried finding; the route's client bundle predates this phase | `npm run perf:check` on the deployed build | A documented performance budget exception, not an infrastructure defect | Not fixed here: this phase changes no web source | 8 of 9 routes within budget; number unchanged (±1 KB) from the recorded 1128 KB | CARRIED |
| P32-K2 | KNOWN FINDING | The API's private address is not firewalled from the platform's port bridge: the bridge mirrors every listening TCP port on the sandbox | No firewall, container network or reverse proxy exists in this environment to sit in front of the listening sockets | `evidence/production-fingerprint.json` (`listening_sockets`) | The `/docs` decision is a routing decision, not a network boundary | Recorded; the boundary is enforced where it can be (private bind + frontend routing), and the residual is stated rather than hidden | Probes on both surfaces | CARRIED |
| P32-K3 | KNOWN FINDING | The production edge's certificate expiry is not observable from inside the host: `cert-expiry-production-edge` reports `None` | The certificate belongs to the platform edge; the sandbox cannot reach its own public hostname over TLS (`SSLZeroReturnError`) | `evidence/production-fingerprint.json` (`tls.public_origin`) | An external expiry cannot page from here | Rule reports the documented reason instead of a fabricated number; edge monitoring belongs to the platform operator | Rule present and non-firing with `None` | EXTERNAL DEPENDENCY |
| P32-K4 | KNOWN FINDING | Nine browser tests skip by repository design | Six multi-worker and three turn-lifecycle cases are gated on integration fixtures (`DEEPTUTOR_MULTI_WORKER_E2E`, `DEEPTUTOR_TURN_E2E_FIXTURE`) that do not exist in this deployment | `evidence/browser-production.log`; the suite sources | No multi-worker turn coverage in this environment | Reported as skipped, never as passed | Counts stated in §20 | CARRIED |
| P32-K5 | KNOWN FINDING | No LLM-backed chat turn was exercised | No model provider is configured or reachable in this environment (carried from Phases 30/31) | Deployment configuration; carried evidence | The LLM path is not covered by production validation here | Stated as a limitation of this environment, not as a product result | — | EXTERNAL DEPENDENCY |

No finding in this phase was classified as flaky, and no failure was left without
a reproduction and a fix or an explicit classification above.

---

## 26. Host recycle (environment event)

Between work sessions this sandbox was recycled for the second time in the phase,
on a schedule nobody here controls. What survived and what did not:

* **Survived:** the git checkout with every committed phase document and the
  Phase 32 toolchain; the remote branch (with the phase's commits); the built
  wheels and the derived Chromium/font assets from this session.
* **Destroyed:** the production host `/home/user/mo7-prod` with its release trees,
  data root, backups, evidence, metrics and TLS material; the ops virtualenv; the
  Playwright browser cache.

Consequences, stated plainly:

1. Every runtime artifact referenced in this report was produced **after** the
   rebuild, so the whole certification battery in §21 was executed against a host
   rebuilt from the committed toolchain — not inherited from an earlier session.
2. The artifact hash in §2 is the hash of the wheel rebuilt from `58f548b` in this
   session (`45d24a7f…`). A wheel is not bit-reproducible across builds (archive
   timestamps and ordering), so this is a **new artifact for the same source
   commit**, and the identity chain records the source commit, not a hash from a
   previous session. The earlier artifact of the same commit does not exist any
   more; it is not claimed to be identical.
3. The rebuild path itself is now exercised evidence: `prod_bootstrap.sh` rebuilt
   the host, and the two gaps it exposed (P32-H8, and the missing shell harness)
   were fixed in the toolchain and re-verified by re-running the affected
   instrument.

---

## 27. External dependencies

| Dependency | Why it cannot exist here | What is claimed instead |
| --- | --- | --- |
| Production edge TLS certificate + DNS | Owned by the platform edge; the sandbox cannot reach its own public hostname over TLS | The public origin is used and its behaviour verified from inside (routing, headers, refusals); the certificate itself is the platform's, and its expiry is not observable here (P32-K3) |
| Off-host alert delivery (paging/email) | No such transport in this environment | Rules are evaluated, recorded and verified locally; escalation is documented |
| Container runtime / cgroup quotas | Not installed | Single-host process model under supervisord with rlimits; memory/CPU quotas reported as unavailable |
| Managed database (PostgreSQL/Redis) | No server installed; Redis is optional for this application and unused | SQLite stores with consistent snapshots, per-store integrity checks and rehearsed restores |
| LLM provider | Not configured/reachable here | The LLM path is out of production validation coverage and said so (P32-K5) |
| Package/font/browser CDNs | Several are TLS-blocked from this host | Artifacts are vendored from npm at build time (`@sparticuz/chromium`, `geist`, `@fontsource/lora`); the deployment itself never fetches them |

None of these was used to excuse a repository, deployment, configuration, harness
or test defect.

---

## 28. Deferred / out of scope

* Multi-host or high-availability topology, load balancing and zero-downtime
  switching (needs infrastructure this environment does not have).
* cgroup-level memory/CPU quotas and container packaging
  (`Dockerfile`/`compose.yaml` exist in the repository but no container runtime
  exists here, so the container path was not exercised in this phase).
* Off-host log aggregation and long-term metric storage.
* Capacity planning beyond the measurements in §19.
* Reversing an arbitrary destructive schema migration in place (the documented
  path is the pre-promotion backup plus a restore; §10).
* The Phase 26/28/31 route-budget item (P32-K1) — a product performance
  workstream, not infrastructure.

---

## 29. Evidence index

All under `/home/user/mo7-prod/evidence/` unless stated.

| File | Contents |
| --- | --- |
| `final-loop.log` | the frozen final validation loop (§21): security ×2, artifact ×2, backup, restore, verify, validate ×2, perf, inventory, identity, status, health, releases, backups, alerts, orphans, deployment records |
| `production-verify.json` | 107 infrastructure checks |
| `production-validation-pre.json`, `production-validation-post-restart.json` | 41 application checks; 6 persistence checks |
| `production-security-tls.json`, `production-security-loopback.json` | 38 checks each |
| `production-artifact-probe.json` | the 29-check wire probe (per surface) |
| `production-perf.json`, `production-perf-before-rss-fix.json` | performance before/after the RSS fix (§19, P32-H11) |
| `production-faults.json`, `faults/<case>-<epoch>.json`, `rebuild-faults.log` | the eleven injections, per-case and aggregate |
| `restore-*.json` | restore rehearsals (10/10 each) |
| `data-inventory-{pre-n1,post-n1,pre-rollback,post-rollback,final}.json` | the data/state graph snapshots |
| `rebuild-deploy-drill.log` | N → N+1 → broken-refused → rollback, with the inventory comparisons |
| `rebuild-verify.log`, `rebuild-security.log`, `bootstrap.log` | the post-rebuild certification runs and the host rebuild |
| `browser-production.log`, `browser-guard.log` | the browser matrix (64 passed) and the Chromium-guard behaviour |
| `production-fingerprint.json` | environment capability fingerprint, listening sockets, TLS observations |
| `/home/user/mo7-restore/classification.txt` | the recycled-workspace classification (remote commit vs working tree) |

Toolchain source of truth (in the checkout):
`docs-for-user/phase32-infra/` — the operational entry points, harnesses,
templates and the bootstrap; `docs-for-user/PHASE_32_PRODUCTION_INFRASTRUCTURE_RUNBOOK.md`
is the operator's document.

---

## 30. Final checklist

| Requirement | State |
| --- | --- |
| Capability discovery inventory | §3, `evidence/production-fingerprint.json` |
| Documented topology and process model | §4, runbook §1 |
| Production serving of the certified release | §2, §6, §21 |
| Real edge/TLS, never faked | §3, §27 (external dependency, self-signed ingress labelled as an instrument) |
| Database strategy and automated backups with demonstrated restore | §8, §11 |
| Storage durability | §12 |
| Secrets management | §13 |
| Network boundary incl. `/docs`, `/redoc`, `/openapi.json` decision | §14 (decision + residual exposure stated) |
| Auth/session production behaviour + Phase 27 harness | §15 (38/38 on both surfaces) |
| Observability + alerts with condition/severity/response/owner/verification | §17 (18 rules; off-host delivery external) |
| Deployment and rollback **demonstrated**, data integrity preserved | §6, §9, §10 |
| Zero-downtime only if demonstrated | §24 — **not claimed**, window measured |
| Migration safety (backup → migrate → validate → rollback) | §8, §10 |
| Resource limits | §18 (no memory/CPU quotas available; stated) |
| Performance vs Phase 28 | §19 — no regression established |
| Ten named failure injections (eleven executed) | §16 — 11/11, 82/82 |
| Full regression | §21 — verify/validate/security/artifact/browser/restore/perf all green |
| Per-capability machine-readable harness | §29 (JSON evidence per instrument) |
| Loop/graph engineering with a production dependency graph | §5 + §7 (components, edges, cycles, exercised edge by edge) |
| Data/state graph across deploy/restart/crash/redeploy/rollback/restore | §7 |
| Findings with exactly one classification each | §25 (11 fixed instrument defects, 5 known/external) |
| No fake success (§34) | Every claim above names its evidence; refused/failed runs are shown, not hidden |
| Promotion gate (§35) | §9, executed including the refusal |
| Final validation loop (§36) | §21, `evidence/final-loop.log` |
| Git closure (§37) | §31 |

---

## 31. Promotion assessment and repository state (§37)

| Gate node | Status | Evidence |
| --- | --- | --- |
| Production deployment operational | PASS | §2, §21 |
| Separate from the development checkout | PASS | dedicated root, data root, venv, credentials, ports, backups |
| Source/artifact identity traceable | PASS | `identity` → `traceable=True failures=0` |
| Deployment executed | PASS | §6, §9 |
| Bad release refused, previous release serving | PASS | §9 (broken `1.6.13-58f548b`) |
| Rollback executed with data intact | PASS | §10 |
| Backup restored (rehearsed) | PASS | §11 (10/10) |
| Fault injections executed | PASS | §16 (11/11, 82/82) |
| Security posture validated | PASS | §14, §15 (38/38 twice) |
| Observability effective | PASS | §17 (fired and cleared under injection; 0 firing now) |
| Performance not regressed | PASS | §19 |
| Browser coverage against production | PASS | §20 (64 passed; 9 fixture-gated skips documented) |
| Zero-downtime | NOT CLAIMED | §24 |
| External dependencies documented | PASS | §27 |

Repository state:

* branch `arena/01a0ff3f-mo7`, local HEAD == `origin/arena/01a0ff3f-mo7`;
* `main` (`a053fec`) and `arena/01a0dba1-mo7` untouched;
* working tree clean except for files that are generated by the build
  (`deeptutor_web/` package data, ignored by `.gitignore` — only `__init__.py` is
  source);
* no production runtime data anywhere in the checkout; the deployment lives
  entirely outside it;
* exactly one classification per finding; no finding marked flaky.

**Final status: `PHASE 32 READY`.**

Phase 33 is not started — HARD STOP.
