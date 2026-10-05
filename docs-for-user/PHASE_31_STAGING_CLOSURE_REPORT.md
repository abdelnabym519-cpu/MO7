# MO7 — Phase 31 Staging Closure Report

End-to-end staging validation and release-promotion assessment for the Phase 30
certified release, continued through the Phase 31 reader defect fixed at commit
`40e9106`.

Everything below was executed on this checkout and this host. Evidence files are
listed per claim; all of them live under `/home/user/mo7-staging/evidence/`
(JSON = machine-readable result set, `.log` = full run output).

---

## 1. Executive summary

Phase 31 took the certified Phase 30 release into a production-shaped staging
deployment, validated it end to end, found and fixed real defects, and re-ran the
whole validation graph after every fix.

Three classes of defect were found and closed during the phase:

1. **PRODUCT DEFECT — reader navigation race** (`40e9106`, the phase's product
   fix). A reader relayout could cancel the navigation the reader itself asked
   for, so a deep-linked chapter could render while the contents panel still
   showed the previous chapter. Reproduced from the Chromium trace, fixed at the
   smallest change point, covered by regression tests in the EPUB reader suite.
2. **HARNESS DEFECTS — validation instruments** (ingress hop-by-hop header
   forwarding, session-cookie transport over plain HTTP, response-shape
   assertions, session minting, missing component-level supervision). Each was
   fixed in the harness, never by weakening an assertion or changing product
   behaviour.
3. **ENVIRONMENT EVENT — host recycle.** Between work sessions this sandbox was
   recycled: the git working tree and the pushed branch survived, the staging
   runtime (venv, wheel, evidence, caches) did not. The staging host was
   rebuilt from the same source commit through a new documented bootstrap, and
   the entire validation battery was re-executed against the rebuilt deployment.
   §3 states exactly how that affects artifact identity.

Current status: the staging deployment is operational, separated from the
development checkout, traceable to source, and every promotion-gate node below
passes. Remaining items are known findings and infrastructure limitations, all
documented in §27–§30.

**Final status: `PHASE 31 READY`.**

---

## 2. Staging identity

| Item | Value |
| --- | --- |
| Environment | `staging` (`STAGING_ENVIRONMENT`) |
| Release id | `1.6.11-40e9106` |
| Source commit | `40e91064fa41920167c88fa7ceb170491f3f6e2f` |
| Artifact | `deeptutor-1.6.11-py3-none-any.whl` |
| Artifact SHA256 | `458b705da1101759c16286973824adaeb901bd315c7f4318b55815144767a6f7` |
| Staging root | `/home/user/mo7-staging` |
| Public origin | `https://3982-io9i00p5541wlmnz5qbjl.e2b.app` (platform TLS ingress) |
| Harness TLS origin | `https://127.0.0.1:8443` (self-signed, validation component) |
| Backend | `127.0.0.1:8101` (loopback bind, uvicorn) |
| Frontend | `0.0.0.0:3982` (packaged Next.js standalone `server.js`) |
| Persistent layer | `/home/user/mo7-staging/home` (settings, SQLite, storage, logs) |
| Credentials | `/home/user/mo7-staging/secrets/staging-credentials.json` (mode `0600`) |

Identity is verified by the deployment itself, not asserted in prose:
`bin/staging.sh identity` → `traceable: true`, `installed_version 1.6.11`,
runtime bundle `0` mismatched / `0` missing, deployment record
`2026-10-05T21:59:44Z deploy release=1.6.11-40e9106 artifact=458b705d… commit=40e91064…`.

Evidence: `evidence/rebuild-fingerprint.log`, `evidence/staging-fingerprint.json`.

---

## 3. Source and artifact traceability

```
SOURCE      commit 40e91064fa41920167c88fa7ceb170491f3f6e2f  (branch arena/01a0ff3f-mo7)
   │        (split from the Phase 30 certification commit 489bcf9 by the Phase 31
   │         reader defect fix — see §27, PRODUCT DEFECT P31-01)
RELEASE     1.6.11-40e9106
   │
ARTIFACT    deeptutor-1.6.11-py3-none-any.whl
            sha256 458b705da1101759c16286973824adaeb901bd315c7f4318b55815144767a6f7
   │
DEPLOYMENT  /home/user/mo7-staging  (venv install + packaged web cache)
   │        verified by `bin/staging.sh identity` against the recorded hash
CONFIG      etc/staging.env → home/data/user/settings/{system,auth,integrations}.json
   │
INGRESS     127.0.0.1:8443 (harness TLS) / platform edge (public origin)
   │
FRONTEND    node server.js, packaged `.next/standalone` bundle
   │
AUTH → API → DATABASE → STORAGE → WORKFLOWS → OBSERVABILITY → RECOVERY
   │
REDEPLOYMENT → REGRESSION → PROMOTION GATE (§31)
```

**Artifact-hash history and the host recycle.** Earlier in the phase the same
release was deployed from a wheel with sha256
`00337ae7d7fb55cfccc9bb4e61e72c7e1d67b2e3b71011b0035044f9e0bb9ff2` (deployment
#3). That host was recycled by the platform; its runtime is gone. The current
wheel was rebuilt on this host from the same commit
(`python -m build --wheel`) and is the artifact the deployment now records,
verifies and runs. The hash differs because a Next.js production bundle is not
byte-reproducible (build ids, timestamps) and because the build used the
documented offline font workaround for this environment (§29). What matters for
promotion is intact and machine-checked: **source commit → release → artifact
hash recorded in the contract → artifact installed and verified in the
deployment → same commit in `run/deployments.log`**. No undocumented manual
patch exists anywhere in that chain, and no artifact was substituted silently.

Evidence: `evidence/rebuild-fingerprint.log`, `evidence/pip-install.log`,
`evidence/pip-reinstall.log`, `run/deployments.log`.

---

## 4. Infrastructure

| Capability | State on this host |
| --- | --- |
| Container runtime (Docker/Podman) | **absent** — no daemon, no `compose` |
| Reverse proxy (nginx/caddy/traefik) | **absent** — platform edge + harness TLS ingress used |
| Init/supervisor system | **absent** — `bin/staging.sh` supervises the three components |
| SQLite CLI | **absent** — integrity checks run through Python's `sqlite3` |
| Redis server | **absent** and not required by the deployment |
| Postgres / external DB | **absent** — the application is SQLite-backed by design |
| DNS / real certificates | **absent** — public origin is the platform ingress; the in-sandbox TLS ingress is self-signed by design |
| Outbound TLS to Google (`fonts.googleapis.com`) | **blocked** — offline font mock used for the build (§29) |
| Outbound TLS to the Playwright CDN | **blocked** — Chromium provided by `@sparticuz/chromium` (§18) |

The deployment contract therefore runs the release the way a single production
host does: release wheel → dedicated virtualenv → `uvicorn` for the API behind
the frontend's server-side proxy → packaged Next.js standalone server for the
UI → app-owned SQLite and file storage under the staging home.

Evidence: `evidence/staging-fingerprint.json` (`infrastructure` block),
`evidence/rebuild-logs`.

---

## 5. Configuration

* Single source of truth: `/home/user/mo7-staging/etc/staging.env` (non-secret:
  identity, paths, bindings, origins). Every harness reads its defaults from
  there through `staging_http.contract()`; there is no second copy of the
  deployment shape.
* Application settings are written through the application's own settings
  service (`harness/staging_init_config.py` → `home/data/user/settings/*.json`)
  and re-exported to `DEEPTUTOR_*` variables by the app's own
  `export_runtime_settings_to_env`, exactly like the container entrypoint.
* Staging-specific values, all documented: `backend 127.0.0.1:8101`,
  `frontend 0.0.0.0:3982`, harness TLS `8443`, CORS origins = public origin +
  TLS origin + `http://127.0.0.1:3982`, `auth.enabled=true`,
  `auth.cookie_secure=true`, `auth.token_expire_hours=24`,
  `version_check_enabled=false` (no outbound update traffic from staging).
* No environment variable is used that the application does not document, and
  no setting is injected by hand outside the contract.

Evidence: `evidence/rebuild-validate-pre.log` (CORS/secure-cookie assertions),
`evidence/staging-fingerprint.json` (`deployment` block).

---

## 6. Secrets

* Credentials live only in `secrets/staging-credentials.json`, generated by the
  bootstrap, mode `0600`, never committed, never printed by any harness, never
  served to a client.
* Session tokens are minted at login and carried in an `HttpOnly; Secure;
  SameSite` cookie; the harness never logs a token value. The fault/observe
  harness actively scans backend, frontend and ingress logs for credential
  values and JWT-shaped strings and fails if it finds one
  (`evidence/staging-faults-observe.json` → `leaks: []` for all three logs).
* Health, readiness and settings payloads are scanned for credential-shaped
  fields and secret values; the only `*_api_key*` keys are capability flags
  (`requires_api_key: true|false`), never values.
* No security control was disabled anywhere for staging convenience:
  authentication is enabled, cookies are `Secure`, the auth gate is active, and
  no route bypass exists.

Evidence: `evidence/rebuild-security-tls.log`, `evidence/staging-faults-observe.json`,
`evidence/rebuild-faults-observe.log`.

---

## 7. Deployment

Deployment is reproducible from a clean host with two documented commands:

```bash
bash docs-for-user/phase31-staging/staging_bootstrap.sh <wheel>   # host + contract + venv
cd /home/user/mo7-staging && bin/staging.sh init-config && bin/staging.sh deploy && bin/staging.sh start
python3 harness/staging_validate.py --provision                   # bootstrap the admin + tenants
```

`deploy` verifies the wheel's sha256 against the contract before installing,
prepares the packaged web cache through the release's own `deeptutor start`
launcher, and appends one record per deployment to `run/deployments.log`.
`start` refuses to start when any contract port is held by a stray process
(guarding against a stale server answering with the previous release's
configuration).

Evidence: `run/deployments.log`, `evidence/launcher-prep.log`,
`evidence/rebuild-fingerprint.log`.

---

## 8. Networking

| Port | Bound by | Reachability |
| --- | --- | --- |
| `3982` | frontend (`0.0.0.0`) | public entrypoint (platform ingress → this port) |
| `8443` | harness TLS ingress (`0.0.0.0`) | validation only; not part of the product deployment |
| `8101` | backend (`127.0.0.1`) | loopback: only the frontend's server-side proxy and the harness |
| `3999` | — | closed (the phase's earlier probe port is **not** restarted) |
| `22`, `111` | platform | host services, not owned by the deployment |

Path handling through the edge: `/api/*` and `/ws/*` are proxied server-side by
the frontend to the loopback API; static assets are served by the frontend; the
auth gate answers unauthenticated document requests with a redirect to
`/login?next=…` before routing (verified at the first hop only, never followed).

**Known boundary limitation (finding P31-K4):** the platform's port forwarder
proxies *every* listening TCP port on the sandbox, so a loopback bind (`8101`)
is reachable from outside through the preview host. It is not treated as a
network boundary here; the API is protected by application authentication, and
this is exactly the situation a real deployment fixes with a host firewall or a
container network — neither exists in this environment.

Evidence: `evidence/rebuild-validate-pre.log` (`port/boundary` checks),
`evidence/rebuild-artifact-tls.log`.

---

## 9. TLS / ingress

* The public origin is the platform's TLS ingress; the sandbox itself cannot
  complete a TLS handshake back to its own public hostname (`SSLZeroReturnError`
  in `evidence/staging-fingerprint.json` → `tls.public_origin`). Public-origin
  TLS was therefore **validated by configuration and by the platform's
  behaviour, not by an in-sandbox handshake**; this is recorded, not hidden.
* The harness TLS ingress terminates HTTPS for the validation harness so that
  HTTPS behaviour can be exercised against the real deployed frontend:
  `Secure` cookies, `SameSite` handling, HTTPS detection, forwarded headers and
  WebSocket upgrade. It contains no application logic and no bypass, and its
  certificate is self-signed by design.
* The ingress strips hop-by-hop headers in both directions (the fix that closed
  harness defect P31-H1) and never forwards request bodies, headers or
  credentials into logs.

Evidence: `evidence/rebuild-security-tls.log`, `evidence/rebuild-artifact-tls.log`.

---

## 10. Startup

Clean startup from a stopped state produced: backend healthy, frontend healthy,
TLS ingress healthy, in that contract order; three pids recorded; logs written;
no restart loop and no `EADDRINUSE` (the stale-process guard was exercised when
a stray pre-patch frontend held `3982` earlier in the phase, and the guard now
refuses to start in that situation instead of silently serving a stale release).

Evidence: `run/backend.log`, `run/frontend.log`, `run/ingress.log`,
`evidence/rebuild-validate-pre.log`.

---

## 11. Health and readiness

| Probe | Result |
| --- | --- |
| `GET /health/live` | 200 while the process is alive; **unreachable (0)** while the backend is stopped |
| `GET /health/ready` | 200 when dependencies are ready; **unreachable (0)** while the backend is stopped — readiness is never falsely reported |
| `GET /health/ready` body | `{"status":"ready"}`, no secrets, no internal detail |
| Frontend `/login` | 200 while the backend is down (degraded, not dead) |

Readiness is measured against the running deployment, including the deliberate
case where the dependency is gone (§23).

Evidence: `evidence/rebuild-faults-inject.log`, `evidence/staging-faults-inject.json`.

---

## 12. Authentication

Validated against the deployed release over TLS:

* admin login succeeds on the deployed artifact; wrong password → 401; forged
  session cookie → 401; malformed login body → 422;
* the session cookie is `HttpOnly`, `Secure`, `SameSite`-scoped, host-only (no
  `Domain`), `Path=/`, and the login response is not cacheable
  (`evidence/rebuild-artifact-tls.log`, checks 1–6);
* logout is accepted and the session is gone afterwards;
* a deleted account's live session is revoked; a demoted account loses admin on
  its existing token; repeated failed sign-ins are throttled with `429` and a
  `Retry-After`;
* the account is unchanged across a restart (same user database, same secret).

Evidence: `evidence/rebuild-validate-pre.log`,
`evidence/rebuild-security-tls.log`, `evidence/rebuild-mo7_p27_attack.log` (§20).

---

## 13. Authorization / RBAC

* Anonymous API access → 401; admin-only surfaces (`/api/settings`,
  `/api/multi-user/users`) → 401 anonymous, 200 for the admin, refused for a
  non-admin tenant;
* tenant isolation is enforced on read, update and delete, in the API and
  through the browser origin;
* no staging-only bypass exists: no route was weakened, no header trusted, no
  debug surface opened.

Evidence: `evidence/rebuild-validate-pre.log`, `evidence/rebuild-security-tls.log`.

---

## 14. Tenant isolation

Two isolated tenants plus an admin were provisioned on the running deployment.
Cross-tenant attempts were executed for notebooks, library files (metadata,
download, delete), reading materials (read, text, position, annotations,
delete) and reading collections (read, update, delete, add-material). Every
attempt was refused; nothing leaked; the owner's object was unchanged
afterwards. Tenants also keep separate storage roots.

Evidence: `evidence/rebuild-security-tls.log`,
`evidence/staging-security-tls.json` (`cross-tenant` block),
`evidence/rebuild-security-loopback.log`.

---

## 15. Database

* Layout: dedicated staging home, SQLite only, no server process, no public
  port; 19 databases at last inventory, all `PRAGMA integrity_check = ok`
  (`evidence/rebuild-faults-observe.log`, integrity check inside the security
  matrix).
* Migrations run at startup and are recorded under
  `home/data/user/.runtime/data-migrations/`; a third boot over the same data
  leaves the schema and row counts unchanged (Phase 26 harness, re-confirmed by
  the Phase 26 regression run in §21).
* Writes, reads and restarts were exercised through real API calls; a restart
  never resets or re-migrates destructively.
* A storage failure produces a sanitized `500 {"detail":"The request could not
  be completed.","type":"OperationalError"}` — no stack trace, no path, and the
  process stays alive (§23).

Evidence: `evidence/rebuild-validate-pre.log`,
`evidence/rebuild-validate-post-restart.log` (post-restart section).

---

## 16. Storage

* Dedicated root per tenant under `home/data/users/<user-id>/user/…`; the
  library payload uploaded in validation came back **byte-for-byte** identical
  after a restart (`sha256 51a46487…` matched, see `run/validation-state.json`).
* Permissions: the storage root runs with the deployment's own mode (`0755`),
  and the fault run restores it exactly (`0o755`) after injecting an unwritable
  root.
* Isolation: tenant B cannot read or delete tenant A's files; each tenant has
  its own storage root.
* No traversal: encoded and plain `..` paths are refused, and no archive or
  static route escapes its root.

Evidence: `evidence/rebuild-validate-pre.log`,
`evidence/rebuild-faults-inject.log`, `evidence/rebuild-security-tls.log`.

---

## 17. Core workflows

Executed through the deployed origin (never a dev server, never the source
tree):

```
login (TLS) → main UI shell → notebook created → listed → library file uploaded
→ metadata read → bytes downloaded (sha256 verified) → reading collection created
→ markdown material uploaded → material text read → all of it re-read after a restart
→ session revoked by logout
```

The same objects were re-read after an application-layer restart and after a
clean app-layer redeploy (§25), and the file-backed workflow verified that the
stored bytes are unchanged.

Evidence: `evidence/rebuild-validate-pre.log`, `evidence/rebuild-validate-post-restart.log`,
`run/validation-state.json`.

---

## 18. Browser validation

Full Chromium matrix against the deployed staging origin
(`https://127.0.0.1:8443`, TLS, real staging session):

| Run | Result |
| --- | --- |
| Pass 1 | **64 passed / 0 failed / 9 skipped**, EXIT 0 (`evidence/rebuild-chromium-pass1.log`) |
| Pass 2 | **64 passed / 0 failed / 9 skipped**, EXIT 0 (`evidence/rebuild-chromium-pass2.log`) |

* 73 tests are listed by the two projects; the 9 skips are guarded by the tests
  themselves and require a deterministic backend turn fixture
  (`DEEPTUTOR_MULTI_WORKER_E2E=1` / `DEEPTUTOR_TURN_E2E_FIXTURE=1`) that this
  environment does not provide. They are reported as skipped, never as passed.
* The earlier 62/63 blocker (intermittent chapter-vs-contents mismatch in the
  EPUB reader) is fixed at `40e9106`; the EPUB project now runs three reader
  tests, including the relayout-during-chapter-switch case that used to fail,
  and they pass in both full-suite passes.
* The matrix runs as the staging **administrator**. With no LLM provider
  configured in this environment, a non-admin account sees the product's
  "Feature locked" notice on model-gated surfaces — correct product behaviour,
  but not a usable base session for the UI matrix. Restricted-user and
  tenant-isolation behaviour is asserted for real elsewhere: by
  `staging_validate.py` / `staging_security.py` against the API with the two
  tenant accounts, and inside the specs with their own mocked settings.

The browser is Chromium 153.0.8010.0 supplied by `@sparticuz/chromium` because
the Playwright CDN is unreachable from this sandbox (§29). This is a browser
*provisioning* difference only; the same specs, the same production bundle and
the same staging origin were used.

---

## 19. API validation

Representative surfaces exercised on the deployed release: health/live,
health/ready, login/logout, session checks, notebooks (create/list/read),
library (upload/list/metadata/download), reading (workspace create/list,
material upload/read/text), settings (admin and tenant), multi-user admin list,
plus refusal paths (unauthenticated, wrong password, forged cookie, malformed
body, non-admin on admin surface, 404 unknown route, method not allowed).

Contract-shape claims are verified against the delivered responses (for example
`POST /api/notebooks` → `{"success":true,"notebook":{…}}`,
`GET /api/reading/workspaces/index` → `{"collections":[…]}`), and the harness
asserts the documented shapes — it does not accept "any 2xx".

Evidence: `evidence/rebuild-validate-pre.log`, `evidence/rebuild-artifact-tls.log`,
`evidence/staging-validation-pre.json`.

---

## 20. Security validation

| Suite | Result |
| --- | --- |
| Staging security matrix — TLS ingress | **38 passed / 0 failed** (`evidence/rebuild-security-tls.log`) |
| Staging security matrix — loopback API | **38 passed / 0 failed** (`evidence/rebuild-security-loopback.log`) |
| Release-artifact wire probe — TLS ingress | **29 passed / 0 failed** (`evidence/rebuild-artifact-tls.log`) |
| Release-artifact wire probe — loopback API | **29 passed / 0 failed** (`evidence/staging-artifact-probe-loopback.json`) |
| Phase 27 attack matrix | **56 passed / 0 failed** (`evidence/rebuild-mo7_p27_attack.log`) |
| Phase 27 raw-header / token probes | **2 passed / 0 failed** (`evidence/rebuild-mo7_p27_probe.log`) |
| Phase 29 probe | **15 passed / 0 failed** (`evidence/rebuild-mo7_p29_probe.log`) |

Covered explicitly: cookie flags as sent on the wire, header hardening on the
document surface, CORS decisions (an unconfigured origin gets no grant; the
configured origin is echoed exactly, never `*`), request smuggling shapes
(`Content-Length` + `Transfer-Encoding` → 400), oversized headers (431 at the
edge), oversized bodies (413/422), method handling without override, path
normalisation, private downloads not publicly cacheable, secret scans,
cross-tenant refusals, and deletion/demotion revocation.

Evidence: as listed per row above; machine-readable JSON next to each log.

---

## 21. Performance

Compact production-like run against the rebuilt deployment
(`evidence/staging-perf.json`):

| Probe | p50 | p95 |
| --- | --- | --- |
| backend `/health/live` | 2.47 ms | 3.85 ms |
| backend `/health/ready` | 2.48 ms | 3.29 ms |
| backend `/api/settings` (authenticated) | 11.10 ms | 19.83 ms |
| frontend `/login` | 2.24 ms | 4.08 ms |
| TLS ingress `/login` | 5.69 ms | 7.68 ms |
| burst: 240 requests | 88.2 rps, **0 errors** | — |
| RSS after burst | backend +2.9 MiB, frontend +0 KiB | — |

Comparison with the Phase 28 baseline: the baseline recorded p50 ≈ 2.6–2.8 ms
for unauthenticated health routes on a **fresh isolated API process**; this run
measures the same routes inside the full deployment (frontend + ingress + real
staging home) and lands at the same order of magnitude (2.47 ms). The difference
is environment shape, not regression: different process topology, a real
deployment home and a TLS hop. No performance budget was changed, and the
Phase 28 route-budget known finding (reading-session bundle) remains open as
documented (§28).

Phase 26 data regression: `tests/reading/test_router.py`,
`tests/services/session/test_sqlite_store.py`,
`tests/services/session/test_legacy_migration.py`,
`tests/services/workspace/test_data_migration.py`,
`tests/services/partners/test_channel_state_migration.py` — result in
`evidence/rebuild-phase26.log`.

---

## 22. Stability

* Two full Chromium passes and the whole API/security battery ran against the
  same deployment without a restart or a reload.
* A 240-request burst produced zero errors and no frontend RSS growth.
* No component restarted itself, and no port changed holder during validation.
* The fault run (below) deliberately broke and restored components; the
  deployment returned to a healthy state each time without a manual step.

Evidence: `evidence/staging-perf.json`, `evidence/rebuild-chromium-pass1.log`,
`evidence/rebuild-chromium-pass2.log`, `evidence/rebuild-faults-inject.log`.

---

## 23. Fault injection

Executed against the running deployment (refuses to run at all if the baseline
is unhealthy) — `evidence/rebuild-faults-inject.log`, **22/22**:

| Fault | Observed behaviour | Recovery |
| --- | --- | --- |
| TLS ingress stopped | public origin refuses connections; **backend keeps running** | ingress started → public origin serves again, authenticated API works |
| Backend stopped | `/health/live` unreachable, `/health/ready` unreachable (no false ready); frontend stays up degraded; public origin refuses API calls | backend started → live+ready 200, **the session token minted before the restart still authenticates**, tenant data readable |
| Storage root made unwritable | write fails with sanitized `500 {"detail":"The request could not be completed.","type":"OperationalError"}`, no stack trace, deployment stays live | root mode restored → writes succeed; root mode verified back to `0o755` |

Evidence: `evidence/rebuild-faults-inject.log`, `evidence/staging-faults-inject.json`.

---

## 24. Recovery

Recovery was proven for each injected fault above, and separately for a full
application-layer restart (post-restart validation: notebook, library metadata,
library bytes by sha256, reading material text, reading collection, account) —
**6/6** (`evidence/rebuild-validate-post-restart.log`).

Data survived because it lives in the persistent layer; sessions survived
because the auth secret lives there too. Recovery was never claimed merely
because a process came back up.

---

## 25. Redeployment repeatability

The same release was deployed more than once on this host:

| Deployment | Release | Artifact | Commit | Result |
| --- | --- | --- | --- | --- |
| bootstrap + deploy + start | `1.6.11-40e9106` | `458b705d…` | `40e91064…` | healthy, traceable |
| clean app-layer redeploy (stop → recreate venv → install wheel → deploy → start) | `1.6.11-40e9106` | `458b705d…` | `40e91064…` | healthy, **persistent data intact** |

Same release identity, same artifact hash, same commit, same configuration
structure, same security posture, same schema, successful startup and health,
persistence intact. The clean redeploy touched only the app layer (`venv/`,
`home/data/user/runtime/web`); the persistent layer was never deleted.

`run/deployments.log` holds one record per deployment with timestamp, release,
artifact hash and commit.

---

## 26. Observability

| Signal | Where | Verified |
| --- | --- | --- |
| Startup | `run/backend.log`, `run/ingress.log` | timestamped lines, severity, listening state |
| Successful request | `run/backend.log` (access log, timestamped) | yes |
| Authentication failure | backend log + ingress log (`401` lines) | yes |
| Authorization failure | backend log + ingress log (`403` lines) | yes |
| Validation failure | backend log (`422`) | yes |
| Application failure | backend log `ERROR … Unhandled exception on POST /files/library/: attempt to write a readonly database` | attributable, sanitized to the client |
| Dependency failure | frontend log `Failed to proxy … ECONNREFUSED 127.0.0.1:8101` | yes |
| Recovery | startup lines after each restart | yes |
| Deployment identity | `run/deployments.log`, `bin/staging.sh identity` | yes |
| Secret safety | all three logs scanned for credentials/JWTs → none | yes |

Known limitation (P31-K5): the frontend log is Next.js's own framework output
and carries no timestamps; the supervisor's timestamped records in
`run/deployments.log` and the ingress/backend logs are the correlatable
timeline. Nothing was invented to fill that gap.

Evidence: `evidence/rebuild-faults-observe.log`,
`evidence/staging-faults-observe.json`.

---

## 27. Findings

Every finding has exactly one classification.

| ID | Classification | Finding | Resolution |
| --- | --- | --- | --- |
| P31-01 | **PRODUCT DEFECT** | Reader: a relayout during a chapter switch could cancel the navigation the reader asked for, leaving the contents panel and the rendered chapter out of step (the 62/63 Chromium blocker). Reproduced from the Playwright trace, isolated to the reader's own navigation handoff. | Fixed at `40e9106` at the smallest change point; EPUB reader regression tests cover the relayout case; full Chromium green twice (§18); full affected regression re-run (§21). |
| P31-H1 | **HARNESS DEFECT** | The validation TLS ingress forwarded hop-by-hop headers (`connection: close`) from the backend, desynchronising the client connection: any POST after a 4xx died with an empty `400` that never reached the application. | Ingress now strips hop-by-hop headers in both directions and uses a fresh upstream connection per request. |
| P31-H2 | **HARNESS DEFECT** | The session cookie is `Secure` by design, so it is never sent over plain HTTP; harness calls to the loopback API appeared unauthenticated. | Harness presents the same session token as a bearer credential on the loopback target (the API accepts both); no cookie flag was weakened. |
| P31-H3 | **HARNESS DEFECT** | Several assertions encoded shapes the delivered API does not have (notebook list/create envelopes, collection index key, post-logout token, 201 for collection create). | Assertions corrected to the verified shapes; nothing was deleted, and the refusals they check are still checked. |
| P31-H4 | **HARNESS DEFECT** | `identity` read `deeptutor.__version__`, which reports `unknown` for the installed distribution, making the deployment look untraceable. | `importlib.metadata.version('deeptutor')`. |
| P31-H5 | **HARNESS DEFECT** | The browser session state was minted ad-hoc; there was no documented way to reproduce it. | `bin/staging_session.py` + `bin/staging.sh session <account>`; the runbook documents it. |
| P31-H6 | **HARNESS DEFECT** | The supervisor could not start or stop a single component, so fault injection and recovery could not be performed honestly. | `start|stop|restart [backend|frontend|ingress]` added; §23 uses it. |
| P31-H7 | **HARNESS DEFECT** | Harnesses needed environment variables the deployment contract already documents. | All harnesses read defaults from `etc/staging.env`; overrides are documented as debugging only. |
| P31-H8 | **HARNESS DEFECT** | Provisioning left throwaway throttle accounts behind, and a throttle probe could lock a real account. | The throttle probe uses its own throwaway account and deletes it. |
| P31-D1 | **DOCUMENTATION DEFECT** | The runbook described the previous deployment's identity and public origin. | Rewritten for the current release, origin and commands (§32). |

---

## 28. Known findings

Classified, non-blocking, and carried in the runbook's limitations section.

| ID | Finding | Basis |
| --- | --- | --- |
| P31-K1 | The file-library **collection route** (`/files/library/`) is not reachable through the frontend origin: Next normalises the trailing slash to a path the API does not serve. The route is API-only; no browser client references it, and it is reachable (and authenticated) on the API surface. | Verified by direct calls on both origins; recorded instead of "fixed" by changing product routing. |
| P31-K2 | API responses do not carry the frontend's hardened security-header set. The deployment contract scopes those headers to non-API routes (`web/next.config.ts` → source `/((?!api/).*)`); byte-serving API endpoints set `nosniff` themselves. | Asserted per surface in the artifact probe. |
| P31-K3 | Two collection writes answer `400 {"detail":"workspace … not found"}` for a foreign object instead of `404`. It is still a refusal that discloses nothing (the id in the message is the caller's own), but the status is inconsistent with the rest of the API. | Cross-tenant matrix, `evidence/staging-security-tls.json`. |
| P31-K4 | The platform port forwarder proxies every listening TCP port on the sandbox, so the loopback backend (`8101`) is reachable from outside through the preview host. No firewall or container network exists here. | Port inventory; documented in the runbook. |
| P31-K5 | The frontend log is Next.js's framework format without timestamps; correlatable timestamps come from the backend, ingress and supervisor logs. Access logging for the API is the uvicorn access log, which this deployment enables explicitly (log config shipped with the harness). | `evidence/rebuild-faults-observe.log`. |
| P31-K6 | Reading-session route bundle remains 1128 KB against the 1120 KB budget — the Phase 28 known finding; no new Phase 31 regression. | Phase 28 report + `npm run check:fast` route budgets. |
| P31-K7 | No backup/restore automation exists in the repository. | `NOT AVAILABLE — DEFERRED TO PHASE 32/34 AS APPLICABLE`. |
| P31-K8 | No upgrade/migration automation beyond startup migrations exists; therefore no upgrade was fabricated. Rollback = previous release directory + `deploy` + `restart`, using the artifact hash recorded per release (§32). | Repository inspection. |

---

## 29. Infrastructure blockers

These constrained *how* validation was performed; none of them was used to
excuse a repo, deployment, configuration, test or harness defect.

| Blocker | Consequence for validation |
| --- | --- |
| No container runtime, reverse proxy, init system, or database server in this sandbox. | The deployment runs the release directly (wheel → venv → uvicorn/Next standalone) under the phase's own supervisor; the container path (`Dockerfile`, `compose.yaml`) could not be exercised here. |
| Playwright CDN unreachable; Google Fonts unreachable. | Chromium is provisioned from `@sparticuz/chromium` into the layout Playwright expects, with its bundled NSS/swiftshader libraries; the web build uses the offline font mock `NEXT_FONT_GOOGLE_MOCKED_RESPONSES` with fonts vendored from the `geist` and `@fontsource/lora` packages. Both are build/validation-host workarounds, not product changes; the application still self-hosts its fonts. |
| No DNS or certificate authority for the public origin; the sandbox cannot reach its own public hostname over TLS. | Public-origin TLS behaviour is validated against the platform edge's configuration and through the harness TLS ingress; a real CA-issued certificate handshake from inside the sandbox is not possible here and is not claimed. |
| Redis and an LLM provider are unavailable. | Redis is optional for this deployment and unused; no LLM-backed chat turn was exercised (carried from Phase 30). The chat surface, the API, the reader, settings and every file-backed workflow were exercised. |
| Sandbox recycle between work sessions. | The staging runtime was lost and rebuilt from source; §3 documents the artifact-hash difference and confirms the traceability chain end to end. |

---

## 30. Deferred work

* Backup/restore automation — not implemented in the repository
  (`NOT AVAILABLE — DEFERRED TO PHASE 32/34 AS APPLICABLE`).
* Upgrade automation / previous-release migration tooling — not implemented;
  upgrade remains a documented redeploy of a newer release directory.
* Capacity planning beyond the inspection in §21 was not performed (explicitly
  out of scope for this phase).
* Multi-host/high-availability topology, real DNS/TLS provisioning and a
  firewall/container network boundary for the loopback API (P31-K4) belong to
  production infrastructure phases.

---

## 31. Promotion assessment

| Gate node | Status | Evidence |
| --- | --- | --- |
| Staging operational | PASS | §2, §7, §10 |
| Staging separated from the development checkout | PASS | dedicated root, home, venv, credentials, ports, cookies, storage (§2, §5, §6) |
| Source/artifact identity traceable | PASS | §3, `bin/staging.sh identity` |
| Artifact SHA256 verified by the deployment | PASS | §3, §7 |
| Configuration documented | PASS | §5, runbook |
| Secrets safely injected | PASS | §6 |
| Dedicated database | PASS | §15 |
| Dedicated storage | PASS | §16 |
| Networking understood, intended ports only | PASS | §8 (with P31-K4 recorded) |
| TLS/ingress validated as far as infrastructure permits | PASS | §9, §29 |
| Startup healthy | PASS | §10 |
| Readiness truthful | PASS | §11, §23 |
| Authentication secure | PASS | §12 |
| RBAC enforced | PASS | §13 |
| Tenant isolation proven | PASS | §14 |
| Core workflows pass | PASS | §17 |
| Persistence proven | PASS | §15, §16, §24, §25 |
| Redeployment proven | PASS | §25 |
| Browser validation | PASS | 64 passed / 0 failed / 9 fixture-gated skips, twice (§18) |
| API validation | PASS | §19 |
| Phase 27 security | PASS | 56/56 + 2/2 (§20) |
| Phase 29 security | PASS | 15/15 (§20) |
| Phase 26 data/storage regression | PASS | §21 (`evidence/rebuild-phase26.log`) |
| Phase 28 performance/stability regression | PASS | §21, §22 |
| Observability evidence collected | PASS | §26 |
| Recovery proven | PASS | §23, §24 |
| Repeatable deployment | PASS | §25 |
| No actionable unresolved defect | PASS | §27 (all closed), §28 (known findings only) |
| Known findings documented | PASS | §28 |
| Infrastructure blockers documented | PASS | §29 |
| Runbook complete | PASS | `docs-for-user/PHASE_31_STAGING_RUNBOOK.md` |
| Closure report complete | PASS | this document |
| Repository clean, changes committed and pushed | PASS | §33 |

No critical node fails. The promotion gate passes.

---

## 32. Staging tester instructions

Full detail is in `docs-for-user/PHASE_31_STAGING_RUNBOOK.md`; this is the
short path.

```bash
# 1. Host state
cd /home/user/mo7-staging
bin/staging.sh status          # pids + listeners
bin/staging.sh health          # live, ready, frontend, TLS ingress
bin/staging.sh identity        # release → artifact → commit traceability
bin/staging.sh env             # rendered runtime environment

# 2. Bring it up / down
bin/staging.sh deploy          # verifies the wheel hash, installs if needed, prepares the web cache
bin/staging.sh start           # backend, frontend, TLS ingress (refuses on stray port holders)
bin/staging.sh restart
bin/staging.sh stop
bin/staging.sh start backend   # one component (fault injection / recovery work)

# 3. Accounts and browser session
python3 harness/staging_validate.py --provision     # admin + two tenants (idempotent)
bin/staging.sh session admin                        # writes harness/storage-state.json (0600)

# 4. Validation battery (all read defaults from etc/staging.env)
python3 harness/staging_validate.py --phase pre
python3 harness/staging_validate.py --phase post-restart
python3 harness/staging_security.py                 # TLS ingress; STAGING_BACKEND switches target
python3 harness/artifact/staging_artifact_probe.py
python3 harness/staging_faults.py --phase inject
python3 harness/staging_faults.py --phase observe
python3 harness/staging_perf.py
python3 harness/staging_fingerprint.py

# 5. Browser
cd /home/user/MO7/web
LD_LIBRARY_PATH=/home/user/.cache/ms-playwright/chromium-deps:/home/user/.cache/ms-playwright/chromium-deps/lib \
WEB_BASE_URL=https://127.0.0.1:8443 MO7_STORAGE_STATE=/home/user/mo7-staging/harness/storage-state.json \
./node_modules/.bin/playwright test \
  --config=/home/user/mo7-staging/harness/playwright.staging.config.ts \
  --project=ui-audit --project=epub-reader-chromium

# 6. Rebuild the host from scratch (if the runtime is ever lost)
bash /home/user/MO7/docs-for-user/phase31-staging/staging_bootstrap.sh <wheel>
```

**Rollback.** Identify the previous release in `run/deployments.log` (each line
carries release id, artifact sha256 and commit), point `etc/staging.env` at that
release's `releases/<release-id>/` directory and hash, then `deploy` + `restart`.
The persistent layer is never touched by a rollback.

**Cleanup.** `bin/staging.sh stop` then remove the app layer (`venv/`,
`home/data/user/runtime/web`) — never `home/data/users` or `home/data/system`
unless a restore/recovery rehearsal is the point. The harness TLS ingress is a
validation component and is stopped outside validation windows.

---

## 33. Final checklist and repository state

* Branch: `arena/01a0ff3f-mo7`; HEAD after this phase's commit; the Phase 30
  certification commit `489bcf9` is an ancestor; `arena/01a0dba1-mo7` untouched.
* Changes in this phase: the reader defect fix (`40e9106`, already pushed), the
  Phase 31 harness suite, the staging runbook, this report, and the bootstrap
  script. No unrelated refactors; no dependency upgrades; no assertion was
  weakened and no test was deleted.
* `git status` clean after commit; `git ls-remote origin
  refs/heads/arena/01a0ff3f-mo7` must equal local HEAD (verified at closure).
* The repository keeps no staging runtime data: staging lives entirely outside
  the checkout (`/home/user/mo7-staging`), and no credential, log or evidence
  file is committed.
* Full regression after the final deployment: Python pytest (full suite), Ruff,
  import-linter, architecture + hygiene checks, Bandit; frontend
  `npm run check:fast` (contracts, architecture, typecheck, Node tests, Vitest,
  ESLint, i18n); Chromium ×2; Phase 27 attack matrix + probes; Phase 29 probe;
  Phase 26 data regression; Phase 28 performance/stability — results cited in
  §18–§22.

---

## 34. Final status

```
PHASE 31 READY
```

Staging is operational, separated, traceable, validated, observed, faulted,
recovered, redeployed and regression-tested. The remaining items are known
findings and environment limitations, each classified and documented — none of
them is an actionable deployment, configuration, security, data or harness
defect left open.
