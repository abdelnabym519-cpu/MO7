# MO7 — Phase 31 Staging Runbook

Single-host staging deployment of the certified internal release **1.6.11**
(commit `40e91064fa41920167c88fa7ceb170491f3f6e2f`, branch
`arena/01a0ff3f-mo7`), running the packaged release artifact
`deeptutor-1.6.11-py3-none-any.whl`
(sha256 `458b705da1101759c16286973824adaeb901bd315c7f4318b55815144767a6f7`).

The release id is `<version>-<commit-prefix>` (`1.6.11-40e9106`). The artifact
hash recorded in `etc/staging.env` is the one this deployment verifies before
every install; if the host is ever rebuilt, the new artifact's hash is recorded
there and in `run/deployments.log` — the identity chain is always
source commit → release → artifact hash → deployment, verified by
`bin/staging.sh identity`.

This runbook contains no magic commands: every action maps to a repository
mechanism (`python -m uvicorn deeptutor.api.main:app` as in
`Dockerfile:/app/start-backend.sh`, `node server.js` as in
`Dockerfile:/app/start-frontend.sh`, `export_runtime_settings_to_env` as in
`/app/entrypoint.sh`) or to a documented staging wrapper that only orchestrates
those mechanisms.

## 1. Topology

```
browser ──TLS──▶ platform ingress ──▶ frontend (0.0.0.0:3982)   ← public entrypoint
                                        │  server-side proxy /api/*, /ws/*
                                        ▼
                                    backend (127.0.0.1:8101)    ← loopback bind
                                        │
                                        ▼
                    staging home: SQLite databases + file storage
```

* Public staging URL: `https://3982-io9i00p5541wlmnz5qbjl.e2b.app`
  (platform-provided TLS ingress; **the sandbox cannot complete a TLS handshake
  back to its own public hostname**, so public-origin TLS is validated through
  the platform edge plus the harness ingress below).
* `127.0.0.1:8443` is a **harness component only** (TLS-terminating ingress
  emulator used by the in-sandbox Chromium/API validation). It is not part of
  the staging deployment and is stopped outside validation windows.
* The platform port forwarder proxies **every** listening TCP port on the
  sandbox, so the loopback backend is not a network boundary here; the API is
  protected by application authentication. See the closure report, finding
  P31-K4.

## 1. Topology

```
browser ──TLS──▶ platform ingress ──▶ frontend (0.0.0.0:3982)   ← public entrypoint
                                        │  server-side proxy /api/*, /ws/*
                                        ▼
                                    backend (127.0.0.1:8101)    ← loopback only
                                        │
                                        ▼
                    staging home: SQLite databases + file storage
```

* Public staging URL: `https://3982-i5e85tiqj7jr5i1ufzm8g.e2b.app`
  (platform-provided TLS ingress; the sandbox itself cannot complete a TLS
  handshake back to its own public hostname).
* `127.0.0.1:8443` is a **harness component only** (TLS-terminating ingress
  emulator used by the in-sandbox Chromium/API validation). It is not part of
  the staging deployment and is stopped outside validation windows.

## 2. Layout & contract

| Path | Purpose |
| --- | --- |
| `etc/staging.env` | Non-secret deployment contract: identity, paths, bindings. Single source of truth. |
| `releases/<release-id>/` | Release artifact (`*.whl`), `wheel.sha256`, packaging evidence. `1.6.11-40e9106` is current. |
| `venv/` | Staging virtualenv with the release installed (app layer; recreated on clean redeploy). |
| `home/` | **Persistent layer**: settings JSON, SQLite databases, file storage, structured logs. |
| `secrets/staging-credentials.json` | Non-production staging credentials (mode `0600`). Never committed. |
| `bin/staging.sh` | Supervisor: `deploy | start | stop | restart | status | health | identity | session | logs | env`. |
| `bin/staging_identity.py` | Traceability check: artifact hash → installed version → git blob comparison. |
| `bin/staging_session.py` | Mints the browser harness session (`harness/storage-state.json`) from a staging account. |
| `bin/staging_init_config.py` | Writes the settings JSON contract (first-time setup). |
| `bin/staging_http.py` | Shared HTTP helpers for the harnesses (cookie sessions, bearer tokens, uploads). |
| `harness/staging_artifact_probe.py` | Wire-level release-artifact probe (cookie flags, headers, CORS, framing, path handling). |
| `harness/staging_faults.py` | Fault injection / degradation / recovery / log observability (`--phase inject|observe`). |
| `harness/uvicorn-log.json` | Access + error log configuration passed to the backend (timestamped, severity levels). |
| `harness/tls/` | Self-signed certificate for the harness ingress (mode `0700`). |
| `harness/node_modules` | Symlink to `web/node_modules`, so the browser config resolves `@playwright/test`. |
| `run/` | `backend.pid`, `frontend.pid`, `ingress.pid`, `backend.log`, `frontend.log`, `ingress.log`, `deployments.log`, `validation-state.json`. |
| `evidence/` | Validation evidence JSON/logs (`evidence/archive/` keeps superseded runs). |

First-time setup on a fresh host (also the recovery path after a host loss):

```bash
bash /home/user/MO7/docs-for-user/phase31-staging/staging_bootstrap.sh <wheel>
```

Settings contract (repository-documented location):
`home/data/user/settings/{system,auth,integrations}.json`. The launcher/entrypoint
mechanism re-exports these to `DEEPTUTOR_*` environment variables; `bin/staging.sh`
performs the same re-export through the app's own `export_runtime_settings_to_env`.

Bindings are the only staging-specific difference from the container default:
`STAGING_BACKEND_HOST=127.0.0.1` (the value `start-backend.sh` documents for
`network_mode: host`), `STAGING_FRONTEND_HOST=0.0.0.0` (the ingress-facing tier).

## 3. Daily operations

```bash
cd /home/user/mo7-staging

bin/staging.sh status      # pids + listeners
bin/staging.sh health      # /health/live, /health/ready, frontend / and /api/auth/status
bin/staging.sh identity    # release/artifact/commit traceability (non-zero when untraceable)
bin/staging.sh logs        # tail of backend.log + frontend.log
bin/staging.sh env         # rendered runtime environment (mirrors the container entrypoint)
```

## 4. Deploy / redeploy / rollback

```bash
cd /home/user/mo7-staging

# Deploy the release referenced by etc/staging.env (idempotent)
bin/staging.sh deploy        # verifies wheel sha256 → installs if the artifact is not current
                             # → prepares the packaged web bundle through `deeptutor start`
                             # → appends a record to run/deployments.log

bin/staging.sh restart       # both tiers + harness ingress
bin/staging.sh start backend # single component (fault injection / recovery work)

# Clean redeploy (recreate the app layer, keep persistent data)
bin/staging.sh stop
rm -rf venv home/data/user/runtime/web          # app layer only — never home/data/users or home/data/system
python3 -m venv venv
venv/bin/pip install releases/<release-id>/deeptutor-<version>-py3-none-any.whl   # with dependencies
bin/staging.sh deploy && bin/staging.sh start
```

**Fresh host.** `staging_bootstrap.sh <wheel>` creates the directory contract,
`etc/staging.env`, the harness TLS material, non-production credentials (mode
`0600`), `releases/<release-id>/` with `wheel.sha256`, and the release
virtualenv with the artifact installed. Then: `init-config` → `deploy` →
`start` → `harness/staging_validate.py --provision`.

**Previous-release identification.** Every deployment appends one line to
`run/deployments.log`:

```
<UTC timestamp> deploy release=<release-id> artifact=<sha256> commit=<commit>
```

`releases/<release-id>/wheel.sha256` is the artifact currently referenced by
`etc/staging.env`; `bin/staging.sh identity` prints the last deployment record
plus the installed version and the runtime-bundle comparison.

**Rollback** = point `etc/staging.env` at the previous `releases/<release-id>/`
(artifact + `STAGING_ARTIFACT_SHA256`), then `deploy` + `restart`. There is no
upgrade/rollback CLI in the repository; this procedure is the documented
supported path (`pip install --force-reinstall --no-deps <wheel>` preserving
`home/`). Database migrations run at startup and are recorded under
`home/data/user/.runtime/data-migrations/`. The persistent layer is never
touched by a rollback.

## 5. Database

SQLite only. No server process and no public port. There is **no `sqlite3` CLI**
on this host — use Python:

```bash
venv/bin/python - <<'PY'
import sqlite3
con = sqlite3.connect("file:home/data/user/workspace/reading/_catalog.sqlite3?mode=ro", uri=True)
print(con.execute("pragma integrity_check").fetchone()[0])
print(con.execute("select count(*) from materials").fetchone())
PY
```

Main stores: `home/data/user/chat_history.db`, `home/data/users/<uid>/user/…`
(per-tenant chat/library/reading), `home/data/system/auth/{users.json,login_attempts.sqlite3}`,
`home/data/cron/jobs.sqlite3`. Integrity of every store is checked by
`harness/staging_fingerprint.py`.

Login throttling lives in `home/data/system/auth/login_attempts.sqlite3`
(limit `(10, 300 s)`); it is not a lockout — throttling one username does not
affect other accounts.

## 6. Storage

`home/` is the storage root (`data/users/<uid>/user/workspace/…`). The
`Phase 26` storage lifecycle applies: uploads are sha256-addressed, archives are
extracted with containment checks, and traversal is refused. Only the app
process should own these files; keep `secrets/` at `0600` and `home/` at `0700`.

## 7. Recovery

| Symptom | Action |
| --- | --- |
| Frontend process died | `bin/staging.sh restart frontend` (a full `restart` also works). |
| Backend process died | `bin/staging.sh restart backend`; sessions survive (the auth secret lives in `home/`). |
| Ingress process died | `bin/staging.sh restart ingress`. |
| A port is held by a stray process | `bin/staging.sh start` refuses and prints the holder; stop that process, then start. |
| Backend returns sanitized 500 on a store | Check the store's permissions/integrity (Python `sqlite3`), fix, retry — no restart needed. |
| Stale/incorrect runtime bundle | `rm -rf home/data/user/runtime/web && bin/staging.sh deploy` (re-materialises it from the artifact). |
| Suspected secret/liveness issue | `bin/staging.sh health` (no secrets are exposed), `bin/staging.sh identity`. |

## 8. Cleanup / teardown

```bash
bin/staging.sh stop          # stops frontend, backend and the harness ingress
rm -rf venv home/data/user/runtime/web   # optional app-layer cleanup; keeps persistent data
```

Do not delete `home/` unless intentionally testing restore/recovery.

## 9. Validation harnesses

Every harness is standard-library-only Python 3.11, so `venv/bin/python` and the
repository virtualenv are both fine. Each takes its defaults from
`etc/staging.env` (the deployment contract): release identity, ports, origins and
paths. Overriding them with
`STAGING_FRONTEND` / `STAGING_BACKEND` / `STAGING_PUBLIC_HOST` points a harness
at a different shape and is only for debugging.

```bash
# API contract / auth / RBAC / tenancy / workflows (through the TLS ingress)
venv/bin/python harness/staging_validate.py --phase pre
venv/bin/python harness/staging_validate.py --phase post-restart

# Security matrix (38 checks). TLS ingress by default; STAGING_BACKEND switches
# the target to the loopback API, which is the same matrix without the edge.
venv/bin/python harness/staging_security.py
STAGING_BACKEND=127.0.0.1:8101 venv/bin/python harness/staging_security.py

# Release-artifact wire probe (29 checks): cookie flags as sent, header
# hardening, CORS decisions, request ambiguity, framing, path normalisation.
# The default target is the TLS ingress; STAGING_ARTIFACT_TARGET switches it to
# the loopback API (document-surface checks are then scoped to the API).
venv/bin/python harness/staging_artifact_probe.py
STAGING_ARTIFACT_TARGET=127.0.0.1:8101 venv/bin/python harness/staging_artifact_probe.py

# Fault injection, degradation, recovery and log observability
venv/bin/python harness/staging_faults.py --phase inject
venv/bin/python harness/staging_faults.py --phase observe

# Persistence fingerprint (before/after redeploy comparison)
venv/bin/python harness/staging_fingerprint.py evidence/persistence-after-redeploy.json

# Compact performance check
venv/bin/python harness/staging_perf.py

# Browser session state (re-mint after a deploy; tokens are per-login)
bin/staging.sh session admin

# Chromium suites against staging (needs the 8443 TLS harness ingress running)
# Always call the locally installed binary: `npx playwright` fetches a different
# playwright version and then cannot resolve @playwright/test for the config.
cd /home/user/MO7/web
PLAYWRIGHT_BROWSERS_PATH=/home/user/.cache/ms-playwright \
LD_LIBRARY_PATH=/home/user/.cache/ms-playwright/chromium-deps/lib \
WEB_BASE_URL=https://127.0.0.1:8443 MO7_STORAGE_STATE=/home/user/mo7-staging/harness/storage-state.json \
./node_modules/.bin/playwright test \
  --config=/home/user/mo7-staging/harness/playwright.staging.config.ts \
  --project=ui-audit --project=epub-reader-chromium

# Phase 27 attack matrix, Phase 27 header probes and the Phase 29 probe run the
# same way, from the repository's own harness directory:
cd /home/user/MO7/docs-for-user/phase27-harnesses
/home/user/MO7/.venv/bin/python mo7_p27_attack.py
/home/user/MO7/.venv/bin/python mo7_p27_probe.py
/home/user/MO7/.venv/bin/python mo7_p29_probe.py
```

The matrix acts as the staging administrator. With no LLM provider reachable from
this environment (see §10), a non-admin staging account is shown the product's
"Feature locked" notice on the model-gated surfaces, which is correct product
behaviour but not a usable base session for the UI matrix. Restricted-user and
tenant-isolation behaviour is asserted separately and for real: by
`staging_validate.py` / `staging_security.py` against the API with the
`staging-tenant-a` / `staging-tenant-b` accounts, and inside the specs
themselves with their own mocked settings responses.

## 10. Known limitations (see the closure report for classifications)

* The platform port forwarder proxies **every** listening TCP port on the
  sandbox (including loopback binds): a loopback bind is not a network boundary
  here. The staging backend therefore stays protected by application auth, not
  by network isolation.
* Real TLS certificate/hostname validation cannot be performed from inside the
  sandbox (the public ingress is unreachable from within); the in-sandbox TLS
  checks use the self-signed harness ingress.
* No backup/restore automation exists in the repository
  (`NOT AVAILABLE — DEFERRED TO PHASE 32/34 AS APPLICABLE`).
* No LLM provider is configured or reachable from this environment, so
  LLM-backed chat turns are not exercised (carried from Phase 30) and
  non-admin accounts see the model-gated surfaces locked.
* The file-library collection route (`/files/library/`) is not reachable through
  the frontend origin (Next normalises the trailing slash to a path the API does
  not serve). It is an API-only route; no browser client references it.
* API responses do not carry the frontend's hardened header set — the deployment
  contract scopes those headers to non-API routes (`web/next.config.ts`).
* Two collection writes answer `400` for a foreign object instead of `404`
  (still a refusal that discloses nothing).
* The platform port forwarder exposes every listening port, so `127.0.0.1:8101`
  is not a network boundary on this host.
* The frontend log is Next.js framework output without timestamps; the backend,
  ingress and supervisor logs carry timestamps and severity.
* Chromium for the browser suite comes from `@sparticuz/chromium` (the Playwright
  CDN is unreachable here) and the web build uses the offline font mock
  `NEXT_FONT_GOOGLE_MOCKED_RESPONSES` with fonts vendored from the `geist` and
  `@fontsource/lora` packages. Both are build-host workarounds only.
* Reading-session route bundle: 1128 KB against the 1120 KB budget (Phase 28
  known finding, unchanged).
* No upgrade/rollback CLI exists; redeploy of the same artifact is the supported
  operation today.
* Staging has no LLM provider configured; LLM-dependent chat flows are not
  exercised end-to-end.
* Request/auth-event logging is not emitted by the application (server-side
  errors are).
