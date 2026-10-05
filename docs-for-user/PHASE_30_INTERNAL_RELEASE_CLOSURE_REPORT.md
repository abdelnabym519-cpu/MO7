# Phase 30 — Internal Release, End-to-End Release Validation

**Final status: `PHASE 30 READY`**

An internal release of MO7 `1.6.11` at commit `0a630a1` was built through the repository's own release
workflow, packaged as a wheel, installed into a clean runtime, started from the packaged artifact on a
pristine runtime home, validated end-to-end (release contract, authentication, sessions, persistence,
restart, browser, security, performance), and its remaining findings classified. This report is the
release documentation: identity, procedure, evidence, findings and instructions for internal testers.

---

## 1. Release Identity

| Field | Value |
|---|---|
| Product | MO7 (DeepTutor) |
| Release ID | `internal-1.6.11-0a630a1` |
| Version | `1.6.11` — from `deeptutor/__version__.py` (the repository's single source of truth; tag policy `v1.6.11` is enforced by `.github/workflows/pypi-release.yml`) |
| Repository | `https://github.com/abdelnabym519-cpu/MO7.git` |
| Branch | `arena/01a0ff3f-mo7` |
| Commit | `0a630a1517e5512e4ddb2987a8aa9369883908f1` (`0a630a1`) |
| Release date | 2026-10-05 |
| Channel | internal |
| Artifact | `deeptutor-1.6.11-py3-none-any.whl` — 38 762 949 bytes, sha256 `a0beca6b5188a2bef11396ba37850e5119bbc8681734fdf1b32c36bc8a315dea` |
| Artifact manifest | `/home/user/mo7-phase30-release/release/RELEASE_MANIFEST.json` |
| Build host path | `/home/user/mo7-phase30-release/src` (a `git archive` export of the commit, 3 494 files — no working-tree, `.next`, `node_modules`, `data/` or developer state) |

Environment: Linux 6.1.158+ x86_64, Python 3.11.2, Node v22.22.3, npm 10.9.8, SQLite 3.40.1, 2 vCPU /
3.9 GB RAM, no Docker CLI or daemon.

Historical branch `arena/01a0dba1-mo7` was not touched.

---

## 2. Build

The repository's intended release mechanism is its own workflow: `.github/workflows/pypi-release.yml`
builds the packaged Web assets and the wheel (`.github/workflows/docker-release.yml` builds the container
image — see §16). That procedure was executed locally, verbatim:

```bash
git archive 0a630a1 | tar -x -C /home/user/mo7-phase30-release/src     # clean source export
cd src/web && npm ci --legacy-peer-deps                                # EXIT 0 (25 s)
NEXT_PUBLIC_API_BASE=__NEXT_PUBLIC_API_BASE_PLACEHOLDER__ \
NEXT_PUBLIC_AUTH_ENABLED=__NEXT_PUBLIC_AUTH_ENABLED_PLACEHOLDER__ \
  npm run build                                                        # EXIT 0, ✓ Compiled successfully in 27.3s
cd src && python scripts/prepare_web_package.py --skip-build           # EXIT 0 -> deeptutor_web/ (107 MB, 369 chunks)
cd /tmp && python -m build --wheel --outdir <release> <src>            # EXIT 0
python -m twine check <release>/deeptutor-1.6.11-py3-none-any.whl      # PASSED
```

- **Result:** the wheel contains `deeptutor` (10.7 MB / 1 183 entries), `deeptutor_cli` (0.2 MB / 23) and
  `deeptutor_web` (98.7 MB / 3 203 entries: `server.js`, `.next/static`, `.next/server`, `public`, and the
  minimal `node_modules` subset the standalone server needs), plus `dist-info`; 4 415 entries total.
  Console scripts `deeptutor` and `deeptutor-export-frontend-contracts` are present.
- **Warnings:** only the pre-existing setuptools `license` deprecation notice from `pyproject.toml`
  (`license = {text = ...}`; removal deadline 2027-02-18). Classified `KNOWN FINDING` — packaging-only, no
  behaviour impact, no action in this phase.
- **Placeholder mode:** the packaged build carries `__NEXT_PUBLIC_API_BASE_PLACEHOLDER__` and
  `__NEXT_PUBLIC_AUTH_ENABLED_PLACEHOLDER__`; the launcher copies the packaged web into
  `<home>/data/user/runtime/web` and patches them at first start. Verified on the installed artifact:
  the marker file `.deeptutor-web-runtime.json` was written with `api_base=http://127.0.0.1:8001`, and the
  served HTML contained **no** unpatched placeholder.
- **Reproducibility (measured, not assumed):**
  - *Within one tree:* three consecutive builds produced **byte-identical** client assets (426 static files,
    identical content hashes), including after a cold-cache rebuild (`rm -rf .next`); only Next's random
    `BUILD_ID` differs (it appears in the `static/<BUILD_ID>/` path and in server manifests).
  - *Across independent trees:* two clean installs of the same commit produced 346/426 identical client
    assets; 80 files differed by 1–2 bytes with different content hashes (webpack chunk-id/content-hash
    assignment varies with build-cache state and tree). Sources, lockfile, environment and chunk count were
    identical, and **both builds produced identical route-budget numbers**, so the divergence is
    functionally neutral.
  - **Verdict:** the artifact is *functionally* reproducible, not bit-for-bit reproducible. Classified
    `KNOWN FINDING` (build tool), recorded as a release risk in §17.
- **Font payload:** `fonts.googleapis.com` is TLS-blocked in this environment, so the web build ran with a
  locally supplied font response (`NEXT_FONT_GOOGLE_MOCKED_RESPONSES`). The build, packaging and runtime
  font pipeline are unchanged and the artifact is self-contained (22 woff2 files under
  `.next/static/media`), but the font *bytes* in this sandbox-built artifact are stand-ins. Classified
  `INFRASTRUCTURE BLOCKER` (see §16); a network-enabled rebuild replaces only those bytes.

---

## 3. Installation (clean, documented)

Executed exactly as an internal tester would, on a pristine tree and a new runtime home:

```bash
python3 -m venv runtime/venv
runtime/venv/bin/python -m pip install dist/deeptutor-1.6.11-py3-none-any.whl    # EXIT 0, 983 MB with deps
runtime/venv/bin/deeptutor start --home <runtime-home> --no-browser --detach     # EXIT 0
```

- **No manual intervention was required.** On first start the app created `data/user/settings/` (14 config
  files), the workspace/storage tree, the runtime web cache and all databases (see §5, §6).
- `deeptutor init` is interactive (ports, LLM provider, optional embedding/search). The README documents it
  as optional for a trial install, and the release was validated both ways: with the documented
  skip-`init` quick-trial path (defaults: backend `8001`, frontend `3782`) and with `auth.json` enabled.
  Because it is documented and interactive by design, it is not a release defect; note for internal
  testers: `deeptutor init` is the only interactive step and may be skipped.
- **Dependency resolution:** a fresh install resolves the wheel metadata's lower bounds, so the release ran
  with newer libraries than the developer venv (fastapi 0.142.2, uvicorn 0.54.0, starlette 1.7.0,
  openai 2.54.0, anthropic 1.11.0, llama-index 0.14.25, faiss-cpu 1.15.1, redis 6.4.0, pyjwt 2.15.1,
  bcrypt 5.0.0, python-jose 3.5.0). The whole validation below was performed against that resolved set —
  i.e. what an internal tester gets today, not what the dev venv happens to hold.

---

## 4. Configuration

- Default configuration is created on first start under `<home>/data/user/settings/`:
  `system.json` (ports 8001/3782, worker count, attachment limits, CORS), `auth.json` (auth **disabled** by
  default, 24 h tokens), `model_catalog.json`, `integrations.json`, `interface.json`, `main.yaml`,
  `agents.yaml`, `document_parsing.json`, `graphrag.json`, `ima.json`, `lightrag*.json`, `llamaindex.json`,
  `pageindex.json`, and agent personas (`peer`, `research-assistant`, `teacher`).
- **No secrets are written by default**: every credential-bearing field inspected is empty
  (`auth.json.password_hash`, `integrations.json.pocketbase_admin_password`, `ima.json.api_key`,
  `lightrag_server.json.api_key`, `pageindex.json.api_key`) — verified by a scan of the fresh home.
- Permissions: `data/user/settings` is `700`; files that can carry secrets (`auth.json`, `system.json`) are
  `600`. Recorded observation: workspace content directories are `755` (default umask), i.e. readable by
  other local OS accounts — see §17 `KNOWN FINDING`.
- Configuration mechanism is the intended one (settings files + `DEEPTUTOR_HOME`/`--home`); no environment
  variable, hidden shell state or manually edited generated file was needed. `system.json.version_check_enabled`
  is `true` and the offline update check degrades silently (no startup errors in `launcher.log`).

---

## 5. Database

- On first start the app created 12 SQLite databases in the runtime home, including
  `data/user/chat_history.db`, `data/user/.runtime/workspaces.sqlite3` (2 tables),
  `data/user/workspace/reading/_catalog.sqlite3` (6 tables), `data/user/workspace/library/library.db`,
  `data/user/workspace/learning/mastery/mastery.sqlite3` (12 tables), `data/cron/jobs.sqlite3`,
  `data/partners/_runtime/status.sqlite3`, `data/system/auth/login_attempts.sqlite3`, and the per-user
  equivalents under `data/users/<user-id>/…`.
- `PRAGMA integrity_check` on every database: **`ok`** (12/12).
- The startup data-migration runner executed on the fresh home and left its state under
  `data/user/.runtime/data-migrations/` (`default-adoption-v1.json`, `default-adoption-backup.json`), with no
  migration errors. No migration file was modified.

---

## 6. Storage

- Storage root layout is created on first start: `data/user/workspace/{notebook,co-writer,book,chat,personas,reading,library,learning,suggestions}`, `data/user/archive/legacy-chat`, `data/users/<id>/…`, `data/system`, `data/cron`, `data/partners`, `data/user/logs`, `data/user/runtime/web`.
- Write → read → restart → read → delete covered by the release checks: a reading collection, a reading
  material (text), a library file and a notebook were created before the restart and all read back
  correctly afterwards; a cross-tenant delete attempt left the owner's data untouched (§9); material text
  was readable through the reader path (`/units/1`) after two restarts, and the library file was still
  listed. Material bytes were verified as persisted content, not process memory.
- Per-user separation on disk is real: `data/users/u_<id>/user/…` holds each account's own runtime state and
  reading catalog.

---

## 7. Authentication

Auth was enabled the documented way (`auth.json` → `"enabled": true`, restart). Release checks (all PASS):

| Check | Result |
|---|---|
| Anonymous access to 5 protected routes | 401 on all five |
| First registration bootstraps the admin | 201 (and self-registration closes afterwards: 403) |
| Login | 200 with `dt_token` session cookie |
| Cookie attributes (raw `Set-Cookie`) | `HttpOnly`, `SameSite` present |
| Login response cacheability | `Cache-Control: no-store` |
| Wrong password / malformed body | 401 / 422 |
| Authenticated protected route + settings | 200 / 200 |
| Logout | 200, cookie cleared (`Max-Age=0`), post-logout access 401 |
| Admin provisions a second user | 201 |
| Second user login | 200 |
| Second user on the admin route | 403 |
| Forged bearer token | 401 |
| Token minted **before a restart** | still valid after the restart (persisted signing secret) |

---

## 8. Core Workflows

Real product workflows, driven through the release HTTP surface and the release browser build:

1. **Open the app → reach the UI:** `GET /` → 307 → `/chat` → 200 production HTML (23 KB), all referenced
   JS/CSS assets 200, security headers present, no hydration or console failures in the browser matrix.
2. **Authenticate → protected API → log out:** §7.
3. **Reading workspace:** create a collection (201) → upload a markdown material (reader-ready detail) →
   list → read the material detail → read its unit text → survives restarts (§6, §12).
4. **Library:** upload a file → list → survives restart.
5. **Notebook:** create → list → survives restart.
6. **Settings:** read `GET /api/settings` as an authenticated user (200).
7. **Multi-user:** admin provisions a user; that user sees an empty own session list (tenant isolation) and
   is refused on admin routes (§9).
8. **Browser journeys:** the full Chromium matrix (63 specs) ran against the release build and passed —
   chat workspace, settings, providers, library, reading/EPUB reader, video-learning and mobile variants
   (§10).

Not exercised: **LLM-backed chat turns** — no provider is reachable from this environment
(`INFRASTRUCTURE BLOCKER`, §16). The chat surface itself loads and renders; the model hop is the blocked
node.

---

## 9. Tenant Isolation, RBAC and Failure Handling

- Tenant isolation: the second user cannot read (403/404) or delete (403/404) the admin's reading
  collection, and the owner's collection was still present and unmodified afterwards; the second user's own
  session list is empty.
- RBAC: non-admin → 403 on `/api/multi-user/users`; admin provisioning works; anonymous → 401 everywhere
  protected.
- Failure handling: malformed JSON → 400/422; unknown route → 404; unsupported method → 405; forged token →
  401; provider-unreachable behaviour is graceful (Phase 27/28 evidence and the dead-port probe).

---

## 10. Frontend Release Validation

- Production frontend served **from the packaged artifact** (`Frontend runtime: packaged` in the launcher
  log; `next-server` from `<home>/data/user/runtime/web`).
- Assets: `/_next/static/...` JS and CSS returned 200 for every sampled asset of the served HTML (8/8
  sampled of 13 referenced); CSS loads; no placeholder leakage; static pages 42/42 generated at build.
- **Chromium matrix against the release instance: 63 passed / 0 failed (EXIT=0), 2.6 min** —
  `--project=ui-audit --project=epub-reader-chromium`, `WEB_BASE_URL=http://127.0.0.1:3782`
  (evidence: `canonical/evidence/playwright.log`). This is the same matrix that Phase 29 closed at 63/63;
  it now runs against the packaged artifact rather than a developer server.
- No release-only browser errors were observed (no failed requests, hydration errors or console errors
  surfaced by the specs).

---

## 11. Performance (Phase 28 regression on the release artifact)

| Metric | Phase 28 (developer tree, `5b0d334`) | Release artifact (`0a630a1`, packaged) |
|---|---|---|
| Cold start → `/health/live` | 2 952.9 / 3 170.3 / 3 893.8 ms (min/p50/max, direct uvicorn) | **4.51 s** to health, 5.16 s to frontend `/chat` via `deeptutor start` (4.47–5.02 s across four cold starts) |
| Restart → health / frontend | 2 872.8–3 056.8 ms | **4.68 s / 5.31 s** |
| Warm `/health/live` | p50 2.151 / p95 2.435 ms | p50 **3.34** / p95 5.98 ms |
| Warm `/health/ready` | p50 2.184 / p95 2.533 ms | p50 **1.90** / p95 2.19 ms |
| Warm `/api/settings` | p50 12.79 / p95 23.57 ms | p50 **13.42** / p95 20.71 ms |
| Warm `/api/multi-user/users` | p50 7.64 / p95 8.48 ms | p50 **2.03** / p95 3.08 ms |
| Warm `/api/reading/workspaces` | p50 2.65–4.02 ms (reading endpoints) | p50 **6.36** / p95 8.99 ms |
| Concurrency | 133.8 rps at 8 workers (mixed workload), 0 errors | **394.4 rps** (240 requests, 8 workers, `/health/live`), 0 errors, p95 27.7 ms |
| Soak | 240 s / 42 248 requests / 0 errors, flat RSS | 60 s / 477 requests / 0 errors, backend RSS 174 544 → 174 468 KiB (flat) |
| Route budgets (release build) | 8/9 OK (reading-session 1128/1120 KB) | **8/9 OK, identical numbers** on two independently built artifacts |

Interpretation: the *request path* is not regressed — warm latencies are in the same range, the budget
numbers are identical, memory is flat and the soak is clean. The release **startup** measured through the
launcher is ~1.4–1.9 s above Phase 28's direct-uvicorn cold start; the difference is the launcher's own
startup/readiness handling plus the packaged-artifact environment (fresh 983 MB venv, packaged-web cache
handling), not a code regression — the same code path measured the same way in Phase 28 is unchanged and
the backend's own import/bind cost is unchanged. This is recorded as an accepted release characteristic,
not a defect (`KNOWN FINDING`, §17).

---

## 12. Recovery

| Scenario | Observed behaviour |
|---|---|
| Graceful stop (`deeptutor stop`) | Clean shutdown, both children reaped, ports released |
| `SIGKILL` backend under the running release | The launcher supervisor tears down the frontend as well and exits — no orphan processes, no half-served UI |
| Restart after kill (`deeptutor start`) | Backend healthy in **3.51 s**, frontend serving in **0.71 s**; all data intact (collection, material detail, unit text, notebook, library file) |
| `SIGKILL` frontend | Backend unaffected (`/health/live` 200); restart restores the frontend in **0.77 s** |
| Restart with live data (×2 in the canonical cycle) | Health 4.65 s; workspace/material/library/notebook all still readable; pre-restart session token still valid |
| Database integrity after all restart/kill cycles | 12/12 SQLite databases `ok` |

The release recovers without manual database repair; no corrupt data, no leaked workers, no orphaned
processes were observed after any scenario.

---

## 13. Upgrade

- The repository ships **startup data migrations** (`data/user/.runtime/data-migrations/`, executed on
  every start; observed clean on the fresh home and on restarts) — the migration mechanism is real and was
  not modified.
- The CLI has **no `update`/`upgrade` command**; the in-app updater targets published releases (PyPI /
  GitHub) and cannot install anything in this offline environment.
- **Validated:** installing the release artifact over an existing installation
  (`pip install --force-reinstall --no-deps <wheel>`) and starting it over the existing runtime home →
  healthy, no migration errors, data intact (collection + material still readable).
- **Not executable here:** a true `release N → release N+1` upgrade, because only this release exists and
  no newer artifact is published. Status: `Upgrade path not yet productized — deferred to later release
  phase`; the artifact-swap mechanic itself is validated.

## 14. Rollback

- Rollback is **not productized** (no rollback command, no artifact retention policy in the repo).
- What is possible today, and was verified: the previous known-good release is identifiable (commit
  `0a630a1`, version `1.6.11`, wheel sha256 `a0beca6b…`), and the install mechanic (reinstall an artifact
  over an existing installation + restart) works on the existing runtime home.
- **Not executed:** an actual downgrade, because no earlier internal release artifact exists for this line.
  Status: `Rollback: artifact swap validated; true downgrade requires a second release artifact — deferred`.

---

## 15. Full Regression (exact results)

| Suite | Result | Evidence |
|---|---|---|
| Python full suite | **8 434 passed, 54 skipped, 0 failed** (532.07 s, EXIT=0) | `/tmp/mo7_p30_pytest.log` |
| Ruff check | **All checks passed** (EXIT=0) | `/tmp/mo7_p30_python_checks.log` |
| Ruff format check | 2 files would be reformatted — pre-existing `KNOWN FINDING` (`codex_auth/oauth.py`, `tools/tex_downloader.py`); repo-wide format is out of scope | idem |
| Import-linter | **3 contracts kept, 0 broken** | idem |
| Compile / import | `compileall` EXIT=0; `deeptutor.api.main` imports, 58 routes at import time | idem |
| Bandit | rc 0 (`-c pyproject.toml`); `nosec` notices only | idem |
| Frontend `check:fast` | EXIT=0 — contracts, architecture (dependency-cruiser), typecheck, ESLint, i18n parity+audit; Node **1 224 pass / 0 fail**; Vitest **108 files / 427 tests passed** | `/tmp/mo7_p30_checkfast.log` |
| Route budgets | 8/9 OK (reading-session 1128 KB / 1120 KB known finding), harness self-tests 4/4 | `evidence/perfcheck-src.log`, `perfcheck-src2.log` |
| Browser (release instance) | **63 passed / 0 failed, EXIT=0** | `canonical/evidence/playwright.log` |
| Security — Phase 27 attack matrix | **56 passed / 0 failed** | `/tmp/mo7_p30_security_regression.log` |
| Security — Phase 27 raw probe | **2 passed / 0 failed** | idem |
| Security — Phase 29 probe | **15 passed / 0 failed** | idem |
| Release contract harness (pristine install) | **42/42** | `canonical/evidence/release-harness-pre.json` |
| Release persistence harness (after restart) | **10/10** | `canonical/evidence/release-harness-post_restart.json` |
| Data/storage | 12/12 SQLite `integrity_check = ok`; storage lifecycle exercised (§6); Phase 26 regression suites inside the full pytest run | §5, §6 |
| Performance | §11 | `evidence/release-perf.json`, `canonical/evidence/cycle-timings.txt` |
| Artifact integrity | `twine check` PASSED; no test credentials, no secrets, no development files in the wheel | `/home/user/mo7-phase30-release/evidence/canonical-inspect` |

**Secret/configuration audit of the artifact:** no API keys, private keys, tokens, `.env` files or
credential stores are present. The only pattern matches were innocuous (`api_key` identifier names in
provider code, a vendored `crypto-browserify` bundle, and a Next.js chunk) and the release harness test
credentials (`p30admin`, `p30user`, their passwords) appear **nowhere** in the artifact. Two recorded
findings: (a) the Next.js standalone output embeds the build directory path in 148 JS + 2 JSON files and
`server.js` — inert at runtime (the launcher relocates the bundle into the runtime home, and the app then
serves correctly in every check), and it is the same pattern the project's own CI builds produce; (b)
`next 16.3.3` carries the critical `next/og` advisory whose vulnerable surface the application does not use
(§17).

---

## 16. Remaining Findings

### `KNOWN FINDING`

1. **`next 16.3.3` — critical advisory `GHSA-vcvr-r3jv-pc5j` (next/og ImageResponse RCE).** Unreachable in
   this release: no `next/og` / `ImageResponse` import exists in the application, no `opengraph-image` /
   `twitter-image` route exists, and the packaged artifact contains no app route using it (verified against
   the installed wheel). Fix path when maintainers next touch dependencies: `next ≥ 16.3.6`.
2. **`exceljs → uuid` moderate advisory** (`GHSA-w5hq-g745-h8pq`); `npm audit` itself says the fix requires
   a breaking `exceljs@3.4.0` downgrade. No production path in this release uses the vulnerable API.
3. **Reading-session bundle 1 128 KB vs 1 120 KB budget** (0.7 % over) — carried from Phases 28/29 with
   chunk-level evidence that ≈200 KB is epub.js + KaTeX for the sanctioned reader and the rest is the
   reading companion's shared chat stack; budget intentionally unchanged; needs broader code-splitting work.
4. **`ruff format --check` two-file delta** (`deeptutor/services/codex_auth/oauth.py`,
   `deeptutor/tools/tex_downloader.py`) — pre-existing, formatting-only, repo-wide format out of scope.
5. **Build reproducibility is functional, not bit-for-bit** — Next's random `BUILD_ID` and webpack's
   cross-tree chunk-hash assignment (§2). Mitigation in practice: identify releases by artifact sha256 and
   re-run the validation suites against the exact artifact (as done here).
6. **Absolute build paths embedded in the Next standalone output** (§15) — inert at runtime, verified by
   relocation; leaks only the build directory path.
7. **Workspace content directories are created `755`** (settings are `700`/secrets `600`) — on a
   multi-account host other local OS users could read workspace files. Changing lifecycle permissions
   touches Phase 26 storage guarantees; recorded for a dedicated hardening phase rather than changed here.
8. **Launcher-measured startup ≈4.3–5.0 s** vs Phase 28's 2.95–3.06 s direct-uvicorn cold start (§11) —
   accepted release characteristic; the request path is not regressed.
9. **The developer test suite must run with ports 8001/3782 free** — with the release instance running, the
   full pytest suite ran ~10× slower (tests bind the default ports and retry); stopping the release
   restored normal speed (532 s). Operational note for testers/CI, not a product defect.

### `OUT OF SCOPE`

1. **Container release path** — building/running the image is out of scope for this phase's environment
   (below), and switching the internal release to containers is a deployment-architecture decision.
2. **Productized upgrade/rollback tooling** — the repository has migrations and an in-app updater only;
   packaging a release-to-release upgrade/rollback mechanism is a later release phase (§13, §14).
3. **Reading companion code-splitting** — the lever that would clear finding 3 changes default-visible UX;
   a broader architecture change.
4. **Broad dependency upgrades** (`npm audit fix`, framework bumps) — explicitly outside the standing
   scope; the two advisories above are documented instead.

### `VALIDATION BLOCKED — INFRASTRUCTURE`

| Blocked validation | Reason | Consequence |
|---|---|---|
| Docker/Compose image build & run (`Dockerfile`, `compose.yaml`, `docker-release.yml`) | No Docker CLI and no daemon in this environment | The containerized release path was **not** exercised; the wheel path validates the same application, storage, auth and frontend |
| Live LLM provider calls (chat turns, tutor, model discovery) | No provider or credentials reachable | LLM-backed chat turns were not exercised; provider-unreachable behaviour is validated as graceful |
| Redis-backed paths (distributed coordination, some worker paths) | No Redis service installable here | Redis-dependent features degrade gracefully (Phases 27–29 evidence); this release does not require Redis |
| Real Google Fonts payload | `fonts.googleapis.com` TLS-blocked | Font *bytes* in this artifact are stand-ins (§2); pipeline unchanged |
| Published PyPI/GitHub release download (in-app updater, true N→N+1) | No network route to the release channel for this version | Upgrade mechanic validated as artifact reinstall only (§13) |
| WebKit/Firefox browser projects | Playwright browser CDN TLS-blocked; only Chromium 143 available | Browser coverage is Chromium-only; no WebKit/Firefox claim is made |

No application defect is hidden behind this table: every blocked item is an external service or platform
capability, and the release was validated end-to-end on the paths that are available.

---

## 17. Release Risks

1. **Critical advisory in an unused framework surface** (`next/og`): unreachable today, but it becomes
   exploitable the moment a route uses `ImageResponse`; upgrade `next` at the next dependency window.
2. **Non-bit-reproducible builds**: two build hosts can produce different chunk hashes; verification must
   always be run against the shipped artifact (as in this phase) rather than assuming equivalence.
3. **Fresh dependency resolution**: a new install can pull newer libraries than the validated set (the
   bounds are lower bounds). Internal testers install with the wheel's metadata as-is; a lockfile for the
   runtime would make installs reproducible.
4. **Sandbox-built font payload**: replace the stand-in fonts by rebuilding in a network-enabled
   environment before any user-facing distribution outside the lab.
5. **No productized upgrade/rollback**: upgrading means "install the newer wheel and restart" (validated);
   rollback means "reinstall the previous wheel" — both need an artifact-retention discipline that does not
   exist yet.
6. **Storage permissions `755` on workspace content**: acceptable for a single-account internal host,
   worth hardening before multi-account deployment.
7. **Port assumptions in tooling**: the dev-tree test suite and the release instance compete for 8001/3782;
   testers must stop the release before running the suite (documented in §19).

---

## 18. Internal Release Checklist

| Item | Status | Evidence |
|---|---|---|
| Clean installation | **PASS** | §3 — fresh venv, `pip install` EXIT=0 |
| Configuration | **PASS** | §4 — defaults created, no secrets, documented mechanism |
| Database initialization | **PASS** | §5 — 12 DBs, integrity ok, migrations ran |
| Storage initialization | **PASS** | §6 — tree created, 700/600 where it matters, lifecycle verified |
| Production build | **PASS** | §2 — EXIT 0, 369 chunks, packaging EXIT 0 |
| Release artifact | **PASS** | §1/§2 — wheel, sha256, `twine check` PASSED, inventory clean |
| First start | **PASS** | §3/§11 — 4.51 s to health, 5.16 s to frontend |
| Health | **PASS** | `/health/live` 200 `{"status":"alive"}` (p50 3.34 ms) |
| Readiness | **PASS** | `/health/ready` 200 `{"status":"ready"}` (p50 1.90 ms) |
| Authentication | **PASS** | §7 — 13 checks |
| Session | **PASS** | §7 — cookie flags, logout, pre-restart token still valid |
| Core workflows | **PASS** | §8 — app UI, reading, library, notebook, settings, multi-user (LLM chat hop BLOCKED) |
| Persistence | **PASS** | §6/§12 — 10/10 after restart; 12/12 DBs ok |
| Restart | **PASS** | §12 — graceful + SIGKILL recovery, no orphans |
| Dependency recovery | **PASS** | §12 — supervisor semantics verified; Redis/LLM/container paths BLOCKED (external) |
| Tenant isolation | **PASS** | §9 — cross-tenant read/delete refused, data untouched |
| RBAC | **PASS** | §9 — 403 for non-admins, 401 anonymous |
| Security regression | **PASS** | §15 — 56/56, 2/2, 15/15, artifact secret scan clean |
| Browser regression | **PASS** | §10 — 63/63 Chromium against the release |
| Performance regression | **PASS** | §11 — warm/budget/soak/memory equivalent; startup delta explained |
| Full test suite | **PASS** | §15 — 8 434 passed, frontend checks EXIT 0 |
| Release documentation | **PASS** | this report + `RELEASE_MANIFEST.json` |
| Git verification | **PASS** | §20 — clean tree, commit pushed, remote verified |

---

## 19. Internal Release Instructions

### Install

```bash
python3 -m venv ~/mo7-release && ~/mo7-release/bin/python -m pip install -U pip
~/mo7-release/bin/python -m pip install deeptutor-1.6.11-py3-none-any.whl   # sha256 a0beca6b5188a2bef1…315dea
```

Requirements: Python 3.11–3.14 and **Node.js 20+ on `PATH`** (the packaged Next.js standalone server is
spawned by the launcher).

### Start / stop

```bash
export MO7_HOME=~/mo7-runtime           # runtime home: settings, databases, storage, logs
~/mo7-release/bin/deeptutor start --home "$MO7_HOME" --no-browser --detach
~/mo7-release/bin/deeptutor stop  --home "$MO7_HOME"
```

Optional first-run configuration (interactive; may be skipped — defaults are backend `8001`, frontend
`3782`): `~/mo7-release/bin/deeptutor init --home "$MO7_HOME"`.

URLs: frontend `http://127.0.0.1:3782` (`/` redirects to `/chat`), backend `http://127.0.0.1:8001`,
health `/health/live`, `/health/ready`. Launcher log: `$MO7_HOME/data/user/runtime/launcher.log`.

### Enable multi-user authentication

Set `"enabled": true` in `$MO7_HOME/data/user/settings/auth.json`, restart, then register the first account
at `/register` (it becomes the admin) and provision further users from `/admin/users`. Auth is **off by
default**; tokens last 24 h; the signing secret lives in `$MO7_HOME/data/system/auth/auth_secret` and must
be preserved across restarts (sessions survive restarts only if it is).

### Data locations

`$MO7_HOME/data/user/…` (workspace, settings, chat history), `$MO7_HOME/data/users/<user-id>/…` (per-user
state), `$MO7_HOME/data/system/…`, `$MO7_HOME/data/cron`, `$MO7_HOME/data/partners`. Back up the whole
runtime home; SQLite databases can be copied while the app is stopped.

### Notes for testers

- Keep the runtime home out of the repository; it is self-contained.
- Stop the release before running the developer test suite (default ports 8001/3782, §16 finding 9).
- Node is mandatory: without it the launcher cannot spawn the packaged frontend.
- No LLM provider is configured in this lab; chat turns need a provider configured under
  **Settings → Models**. Knowledge-base embedding/search likewise needs a provider.
- The build in this lab used stand-in font files (blocked Google Fonts); cosmetic differences from an
  online build are expected.
- Container deployment (`Dockerfile`, `compose.yaml`) is the repository's other supported path and was not
  exercisable in this environment.

---

## 20. Git and Artifact Verification

- Working tree: clean before and after validation; only the Phase 30 report is committed
  (`git status --short` → empty; `git log --oneline -1` shows the docs commit).
- Branch `arena/01a0ff3f-mo7`; `git push origin arena/01a0ff3f-mo7`; `git ls-remote` shows the pushed HEAD.
- `arena/01a0dba1-mo7` untouched.
- The artifact was built from a `git archive` of `0a630a1`; the Phase 30 commit adds this report only
  (documentation — not part of the wheel), so the artifact corresponds to the shipped source.
- Artifact hash recorded in the manifest and in this report: `deeptutor-1.6.11-py3-none-any.whl`,
  38 762 949 bytes, sha256 `a0beca6b5188a2bef11396ba37850e5119bbc8681734fdf1b32c36bc8a315dea`.
- No development server, browser process or temporary credential is left running; no temporary database is
  presented as release state.

---

## 21. Definition of Done

A clean internal release can be built ✔ · release identity immutable and traceable ✔ · artifact
identifiable (sha256) with clean inventory ✔ · clean installation succeeds ✔ · configuration documented ✔ ·
database initializes ✔ · storage initializes ✔ · production app starts ✔ · health/readiness pass ✔ ·
authentication and sessions work ✔ · core workflows work ✔ (LLM hop infrastructure-blocked) · persistence
survives restart ✔ · authorization/tenant isolation intact ✔ · security regression passes ✔ · frontend
production validation passes ✔ · Chromium regression passes ✔ · Phase 28 performance regression passes ✔ ·
full regression passes ✔ · no actionable release blocker remains ✔ · release documentation exists ✔ ·
artifact corresponds to the final source ✔ · repository clean, branch pushed and verified ✔.

`PHASE 30 READY`
