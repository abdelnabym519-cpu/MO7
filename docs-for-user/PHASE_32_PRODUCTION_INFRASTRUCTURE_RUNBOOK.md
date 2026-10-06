# MO7 — Phase 32 Production Infrastructure Runbook

Audience: the operator who has to run, inspect, back up, deploy, roll back or
rebuild the MO7 production deployment on this host.

Every command below is the command that was actually executed during Phase 32;
the evidence each one produced is named in
`PHASE_32_PRODUCTION_INFRASTRUCTURE_CLOSURE_REPORT.md`. Nothing here is
aspirational: if a capability needs infrastructure this environment does not
have (external DNS, a managed TLS edge, a database server, a container runtime),
it is called out as an external dependency rather than wrapped in a local
substitute that would look like production.

Root: everything lives under `PROD_ROOT` (`/home/user/mo7-prod`). No production
file is inside the git checkout; the checkout only carries the toolchain in
`docs-for-user/phase32-infra/`.

---

## 1. Topology

```
        platform edge (TLS, DNS)            <- EXTERNAL: not operated from this host
                 |  https://3782-<sandbox>.e2b.app
                 v
   published port 3782 (0.0.0.0)            frontend: Next.js standalone (node server.js)
   private  port 8001 (127.0.0.2)           backend:  uvicorn deeptutor.api.main:app
   validation port 8443 (0.0.0.0)           harness-only TLS ingress (self-signed)
   smoke port 8099 (127.0.0.2)              staged release under test, during a deployment
```

Process model: **supervisord** (installed in `ops-venv`) owns the long-running
programs — `backend`, `frontend`, `scheduler` and the optional, deliberately
`autostart=false` `ingress-validation`. A host-level **watchdog**
(`harness/prod_watchdog.py`, started by `bin/prod.sh start`) restarts a dead
supervisord with the same configuration and reclaims programs a dead supervisord
generation left behind.

Why the API is bound to `127.0.0.2`: the platform publishes any port that binds
`0.0.0.0` by dialing `127.0.0.1:<port>`. Binding the API to `127.0.0.2` keeps it
— and with it FastAPI's documentation surface (`/docs`, `/redoc`,
`/openapi.json`) — off the published surface while the frontend, which must be
public, stays on `0.0.0.0:3782`. `PROD_BACKEND_HOST` in the contract is therefore
the address of every API probe; a probe that dials `127.0.0.1:8001` is measuring
the bridge target, which is refused by design.

TLS: production TLS terminates at the **platform edge** and is outside this
host's control. The local ingress on 8443 exists only so validation harnesses
and the browser suites can exercise the deployment over a TLS origin; it is
self-signed and is never presented as production TLS.

## 2. Layout & contract

| Path | Contents |
|---|---|
| `bin/prod.sh` | the operational entry point (all verbs below) |
| `bin/prod_deploy.sh`, `bin/prod_promote.sh`, `bin/prod_rollback.sh` | deployment pipeline stages |
| `bin/templates/` | supervisord program templates, contract template, alert/backup policies, systemd unit |
| `harness/` | Phase 32 tools: verify, validate, security, artifact probe, faults, backup, restore, metrics, alerts, watchdog, scheduler, session, browser wrapper, data inventory, contract identity |
| `etc/production.env` | the deployment contract: identity, addresses, ports, limits, policies |
| `etc/supervisord.conf`, `etc/supervisord.d/*.conf` | rendered process definitions |
| `etc/alerts.json`, `etc/backup-policy.json` | alert rules (18) and backup retention policy |
| `secrets/` | `secrets.env`, `credentials.json` (0600) — never committed, never logged |
| `releases/<release-id>/` | immutable release trees (`venv/`, artifact, `manifest.json`, `wheel.sha256`, start scripts, materialised web bundle) |
| `current` | symlink to the release that is live |
| `home/` | the persistent data root (`data/users/<uid>/…`, `data/system/…`) |
| `backups/` | labelled snapshots + `index.ndjson` |
| `run/` | runtime state: supervisord socket/pid, `current-release.json`, `deployments.log`, metrics, alerts, watchdog log, outage/deploy phase logs |
| `evidence/` | machine-readable evidence written by every harness |
| `ops-venv/` | host tooling: supervisord, supervisorctl, and the interpreter the harnesses run on |
| `restore-rehearsal/` | scratch target of the most recent restore rehearsal (never production data) |

`etc/production.env` is the single source of truth every tool reads. The five
identity keys (`PROD_RELEASE_ID`, `PROD_VERSION`, `PROD_ARTIFACT_NAME`,
`PROD_ARTIFACT_SHA256`, `PROD_SOURCE_COMMIT`) are kept consistent with the
release that is running: `prod_promote.sh` and `prod_rollback.sh` rewrite them
from `run/current-release.json` after they succeed, and `prod_deploy.sh` restores
them if a deployment is refused before it publishes anything. An operator may
still edit them first to declare the artifact under test — `VERIFY` then enforces
the declared hash.

Host modes: `PROD_ROOT`, `etc`, `secrets`, `backups`, `run/supervisor`,
`evidence` and the per-user data directories are 700; secrets are 600. `bin/prod.sh harden`
re-applies the documented modes after a manual change.

## 3. Daily operations

```bash
cd /home/user/mo7-prod

bin/prod.sh status                  # supervisor states, listeners, watchdog
bin/prod.sh health                  # liveness, readiness, frontend, ingress, storage, db, disk
bin/prod.sh identity                # commit -> release -> artifact -> running processes
bin/prod.sh logs backend 200        # supervisor log tail (backend|frontend|scheduler|ingress-validation)
bin/prod.sh metrics                 # one metrics sample to stdout (run/metrics/latest.json)
bin/prod.sh alerts --json           # every rule with its state and the metric it read
bin/prod.sh watchdog status         # host watchdog
bin/prod.sh ingress start|stop      # the validation TLS ingress (harness component)
bin/prod.sh harden                  # re-apply documented filesystem modes
```

Bring the stack up / down:

```bash
bin/prod.sh start        # supervisord + programs + watchdog (idempotent)
bin/prod.sh restart      # restart the supervised programs
bin/prod.sh stop         # stop the programs and supervisord (data is untouched)
```

## 4. Deploy / redeploy / rollback

```bash
# 1. declare the artifact (optional but it is what VERIFY enforces)
#    edit PROD_RELEASE_ID / PROD_ARTIFACT_SHA256 / PROD_VERSION in etc/production.env

# 2. deploy it
bin/prod.sh deploy /path/to/deeptutor-<version>-py3-none-any.whl

#    VERIFY  recompute sha256, compare with the declared hash, check the wheel's
#            metadata version and that the source commit exists in the checkout
#    STAGE   build releases/.staging-<id>-$$ (venv + artifact + materialised web bundle)
#    SMOKE   start the staged release on 8099 and require /health/ready = 200
#    PUBLISH rename the staging tree to releases/<id> (immutable from here on)
#    BACKUP  consistent pre-deploy snapshot (the pre-migration control)
#    PROMOTE atomic symlink flip of `current`
#    RESTART restart backend/frontend/scheduler, measuring the outage window
#    HEALTH  require readiness + frontend; on failure flip back automatically
#    SMOKE   post-promote smoke (health, auth, settings, data read)
#    RELEASE record release/artifact/commit/outage in run/deployments.log

# 3. roll back (the target defaults to the previous release in deployments.log)
bin/prod.sh rollback --to <release-id>
bin/prod.sh releases                # staged releases and which one is current
```

A refused deployment is a no-op on the host: the staging tree is removed, the
contract identity is restored from the running release's manifest, and
`current` never moved. A successful deployment records `outage_ms` — a restart
window exists (measured, ~11 s on this host), so **zero-downtime deployment is
not claimed**; see the closure report's classification.

## 5. Database

Stores are SQLite files under `home/` (per-user `chat_history.db`, workspace
catalogues, `data/system/auth/login_attempts.sqlite3`, `data/cron/jobs.sqlite3`,
…). There is no database server in this environment, so there is no failover and
no point-in-time recovery: durability comes from the backup policy and the
restore path below, which is why a restore is rehearsed rather than assumed.

```bash
bin/prod.sh metrics | grep -o 'db=[0-9]* unhealthy=[0-9]*'
ops-venv/bin/python harness/prod_metrics.py --once --skip-db   # skip the integrity sweep
```

Migration safety is the deploy sequence itself: BACKUP (pre-migration control) →
PROMOTE → HEALTH → SMOKE, with automatic rollback to the previous release on a
failed health gate. The application applies its own data migrations on start;
they ran inside the rehearsed deploys of this phase and the data inventory before
and after both deploys and the rollback recorded no lost or altered store (see
the closure report).

## 6. Storage

Uploads live in `home/data/users/<uid>/user/workspace/library/files/` with rows
in the sibling `library.db`; reading material lives under `.../workspace/reading/`.
`bin/prod.sh health` reports `storage writable=…`; the `storage-readonly` fault
injection proves the failure path (a sanitised 500, no partial file, no loss of
service, full recovery when the mode is restored).

Backups include the storage tree as a gzip archive with a recorded sha256
(`manifest.json → storage`).

## 7. Backup & recovery

```bash
bin/prod.sh backup --label before-change     # snapshot now (fails loudly if it cannot be complete)
bin/prod.sh backups                          # index with verification state and age
bin/prod.sh restore --backup backups/<dir> --target restore-rehearsal --force
```

`prod_backup.py` snapshots every SQLite store with the SQLite backup API (a
consistent copy, not a file copy), records a sha256 and `integrity_check` per
store, archives the storage tree, writes `manifest.json` and appends to
`index.ndjson`; a backup that cannot be completed removes its partial directory
and exits non-zero. Retention follows `etc/backup-policy.json` (7 by default).

`prod_restore.py` is the certification path, not just a copy: it verifies the
manifest hash, re-hashes and integrity-checks every snapshot database, restores
into a scratch target, boots a backend against the restored root, authenticates a
restored account against the restored data and reads restored notebook and
reading data — then stops the scratch backend. It never writes to the live root.
A rehearsal that has not passed every check is not evidence, and `prod.sh verify`
will not certify a deployment that has no passing rehearsal record.

## 8. Observability

```bash
bin/prod.sh metrics            # collector: programs, probes, database, disk, backups, secrets, logs
bin/prod.sh alerts --json      # 18 rules: condition, severity, response, owner, verification
```

`etc/alerts.json` carries, per rule, the metric and threshold, the severity, the
documented response, the owner (`mo7-platform-oncall`) and how the rule is
verified — the fault injections drive those same rules and assert that the
expected rule fired (and cleared).

The `application-errors` rule reads the backend log's last 400 lines and counts
ERROR/CRITICAL/Traceback/FATAL lines. Because a fault drill *deliberately*
produces such lines, the drill writes a boundary marker
(`phase32 fault drill complete`) into the backend log when each case finishes and
when the run ends; the collector counts errors written after the newest marker.
Errors from the deployment itself always count, and the marker text contains no
error keywords. The scheduler (`harness/prod_scheduler.py`)
runs the collector and evaluator on an interval, prunes retention and writes
`run/metrics/series.ndjson`.

Escalation beyond this host (paging, an external monitoring system, an
on-call rotation) is an **external dependency** and is not claimed: the
deployment produces the signal, it does not deliver it off-host.

## 9. Failure injections (evidence, not theatre)

```bash
bin/prod.sh faults --list
bin/prod.sh faults --all --keep-going      # 11 named injections, one run at a time
bin/prod.sh faults --case <id>             # a single injection
```

Each case injects a real fault, asserts the observable consequences, restores
what it changed and asserts the recovery (including the drill's own log-marker
boundary, section 8); the run takes a lock
(`run/faults.lock`) so two runs can never fight over one deployment, and it
re-checks the baseline before each case. Per-case JSON lands in
`evidence/faults/<case>-<epoch>.json`; the aggregate is
`evidence/rebuild-faults.log`, and `prod.sh verify` consumes the recorded cases.

## 10. Validation harnesses

```bash
# infrastructure (107 checks): topology, lifecycle, database, resources, network,
# backups, secrets, observability, deployment, processes
bin/prod.sh verify

# application contract, auth/RBAC/tenancy/workflows (41 checks), then the
# post-restart persistence pass (6 checks)
bin/prod.sh validate
bin/prod.sh validate --phase post-restart

# security matrix (38 checks) through the TLS ingress, and again against the
# API's private address (same rules without the edge in front)
bin/prod.sh security
PROD_BACKEND=127.0.0.2:8001 PROD_BACKEND_API=127.0.0.2:8001 bin/prod.sh security

# release-artifact wire probe (29 checks): cookie flags, header hardening, CORS,
# request ambiguity, framing, path normalisation — at the ingress and at the API
bin/prod.sh artifact
PROD_ARTIFACT_TARGET=127.0.0.2:8001 bin/prod.sh artifact

# capability fingerprint, performance, data inventory
ops-venv/bin/python harness/prod_fingerprint.py
ops-venv/bin/python harness/prod_perf.py
ops-venv/bin/python harness/prod_datainventory.py --label <label>
ops-venv/bin/python harness/prod_datainventory.py --compare <label-a> <label-b>

# browser suites against the deployed frontend through the validation ingress
bin/prod.sh session admin            # mint the browser session state (re-mint after a deploy)
bin/prod.sh browser --project=ui-audit --project=epub-reader-chromium
```

`MO7_CHROMIUM_HOME` points at a directory holding `chrome-linux/chrome` and
`chromium-deps/lib/libnspr4.so`; without it the wrapper looks in
`/home/user/mo7-build/chromium` and `/home/user/mo7-prod-build/chromium` and
exits `78` with a clear message rather than letting every test fail on a missing
shared library.

The browser suites need a Chromium build and its libraries, because the
Playwright CDN is unreachable from this host: `harness/prod_browser.sh` locates
the extracted `@sparticuz/chromium` build (`MO7_CHROMIUM_HOME`) and puts its
`libnspr4.so` directory on `LD_LIBRARY_PATH`, then runs the repository's own
Playwright config against the production origin.

## 11. Rebuild the host from scratch

```bash
# one command: directory contract, deployment contract, supervisord, policies,
# secrets, accounts, the validation instruments, TLS material for the validation
# ingress, and the first release staged through the deployment pipeline
PROD_ROOT=/home/user/mo7-prod \
MO7_SOURCE_CHECKOUT=/home/user/MO7 \
bash docs-for-user/phase32-infra/prod_bootstrap.sh \
  /path/to/deeptutor-<version>-py3-none-any.whl \
  --commit "$(git -C /home/user/MO7 rev-parse HEAD)" \
  --public-origin https://<published-host>

cd /home/user/mo7-prod
bin/prod.sh promote --release <release-id>     # first promotion
bin/prod.sh init-config                        # render the app's own settings (auth on, origins, ports)
bin/prod.sh ingress start                      # validation window only
bin/prod.sh provision                          # create the deployment accounts
```

The bootstrap installs the harness instruments (`harness/prod_*.py` and the
shell harnesses such as `harness/prod_browser.sh`), renders the process
definitions, materialises the auth secret, issues the validation-ingress
certificate and stages the first release through the same deployment pipeline the
operator uses.

The bootstrap is idempotent: it never overwrites an existing contract, secret or
release directory, and never touches production data. A rebuild that has to
recreate the data root is a restore, not a bootstrap — restore from
`backups/<dir>` (section 7) first.

## 12. Cleanup / teardown

```bash
bin/prod.sh stop                       # stop the programs + supervisord + watchdog
bin/prod.sh ingress stop               # outside a validation window
rm -rf releases/.staging-* releases/*.bak   # leftover staging trees (published releases are immutable)
rm -rf restore-rehearsal               # scratch target of the last rehearsal
```

Never delete `home/`, `backups/`, `secrets/` or `etc/` as a cleanup step. The
repository keeps no runtime data: production lives entirely outside the checkout.

## 13. Known limitations of this environment

See the closure report for the classification of each item; in short:

* External DNS and the production TLS certificate are **external
  infrastructure dependencies** — they belong to the platform edge and are not
  operated from this host. The local 8443 ingress is a validation instrument,
  self-signed, and must never be presented as production TLS.
* No container runtime, no init system, no packaged reverse proxy, no database
  server and no external object storage exists here; the deployment therefore
  runs the release the supported single-host way (wheel → venv → uvicorn +
  standalone Next.js) under supervisord.
* No zero-downtime deployment is claimed: restarting the programs costs a
  measured outage window (`outage_ms` in `run/deployments.log`).
* Alert delivery off-host (paging/email) does not exist here: the rules are
  evaluated and recorded locally, and the escalation path is documented instead.
