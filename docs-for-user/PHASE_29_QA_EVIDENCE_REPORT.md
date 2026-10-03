# MO7 Phase 29 — Testing & QA Evidence Report

Status: `PHASE 29 NOT READY`

Baseline verified, the full applicable QA matrix executed, every failure
classified with a documented disposition, and **no production or test source was
changed**. The phase is not READY because the project's own Gates are still red
(browser E2E specs, route-budget gate, `ruff format --check`) and none of those
were safely fixable inside a QA-only phase.

Phase 27 remains `VALIDATION BLOCKED — INFRASTRUCTURE`.
Phase 28 remains `VALIDATION BLOCKED — INFRASTRUCTURE`.

---

## 1. Repository baseline (safety gate)

| Item | Value |
|---|---|
| Repository root | `/home/user/MO7` |
| Remote | `https://github.com/abdelnabym519-cpu/MO7.git` |
| Branch | `arena/01a0ff3f-mo7` (session branch) |
| Starting commit | `37f4f4f97db7d1e05fc25eaf403ea0b83c9975f8` — "docs: record Phase 28 performance baseline" |
| Parent branch tip | `origin/arena/01a0dba1-mo7` = `37f4f4f…` (identical to HEAD) |
| Working tree at start | clean (`git status --porcelain` empty) |
| Working tree at end | no tracked file modified; only the new report file added |
| Generated artifacts | `.venv/`, `web/.next/`, `web/test-results/`, `web/playwright-report/`, `data/`, `web/dist/`, `.coverage`, caches — all already gitignored |

Baseline consistency: **PASS** — HEAD equals the recorded Phase 28 baseline and
the Phase 27/28 tree is unchanged.

---

## 2. Measurement environment

- Linux x86_64, Debian GNU/Linux 12, 2 vCPU, ~3 GiB RAM, 20 GiB free disk
- Python 3.11.2 in `.venv` (`requirements/dev.txt` + `requirements/partners.txt`
  + `pip install -e . --no-deps`), plus `ruff==0.16.0`, `detect-secrets==1.5.0`,
  `pytest-cov`, `patchelf`
- Node v22.22.3 / npm 10.9.8, `npm ci --legacy-peer-deps`
- No LLM configured, no Redis server, no Docker, no Debian apt access

### Network reachability observed

| Host | Reachable |
|---|---|
| `pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org` | yes |
| `github.com`, `api.github.com`, `codeload.github.com` | yes |
| `fonts.googleapis.com` | **no** (TLS reset) |
| `cdn.playwright.dev`, `playwright.azureedge.net` | **no** |
| `storage.googleapis.com`, `objects.githubusercontent.com` | **no** |
| `deb.debian.org`, `cdn.npmmirror.com`, `registry.npmmirror.com` | **no** |

### Environment preconditions created for this phase (no repo changes)

1. **npm install** — 5 entries in `web/package-lock.json` resolve to
   `registry.npmmirror.com` (TLS-blocked). Installed with
   `npm ci --legacy-peer-deps --replace-registry-host=always`. The lockfile was
   **not** modified.
2. **Chromium for Playwright (unblocks a Phase 27/28 blocker)** — `cdn.playwright.dev`
   is unreachable, so `npx playwright install` cannot run. Playwright 1.57 needs
   Chromium 143. `@sparticuz/chromium@143.0.4` (npm) ships a brotli-compressed
   Chromium 143 ELF; it was inflated, the bundled AL2023 NSS libraries were
   placed beside it and `patchelf --set-rpath '$ORIGIN'` applied, then it was
   installed into Playwright's expected layout:

   ```
   ~/.cache/ms-playwright/chromium-1200/chrome-linux64/chrome
   ~/.cache/ms-playwright/chromium_headless_shell-1200/chrome-headless-shell-linux64/chrome-headless-shell
   ```

   Resulting binary reports `Chromium 143.0.7499.0` (Playwright expects
   `143.0.7499.4`). Verified end-to-end: CDP endpoint live, `chromium.launch()`
   works, JS executes, DOM renders, `page.route` interception works.
3. **Production build (unblocks a Phase 27/28 blocker)** — `next/font` cannot
   reach Google Fonts, which is a hard failure in `next build`. Used Next.js's
   own documented offline mechanism `NEXT_FONT_GOOGLE_MOCKED_RESPONSES`
   (implemented in `node_modules/next/.../fetch-css-from-google-fonts.js` and
   `fetch-font-file.js`) pointing at `/tmp/mo7-font-mock.cjs`, which returns the
   two exact CSS URLs the app requests:

   - `…/css2?family=Geist:wght@100..900&display=swap`
   - `…/css2?family=Lora:wght@400..700&display=swap`

   Font file substitution: `Geist` ← Next.js's bundled genuine
   `geist-latin.woff2`; `Lora` ← `geist-latin-ext.woff2` (real font, different
   family — no offline Lora exists). The mock emits **one** `@font-face` per
   family where the live Google response emits several subsets, so measured
   bundle sizes are marginally **smaller** than a network-enabled build. The
   suite contains **zero** screenshot/snapshot assertions
   (`grep -c toHaveScreenshot` = 0), so glyph substitution cannot cause a false
   pass/fail.

---

## 3. QA evidence matrix

`PASS` = gate green. `FAIL` = gate red (classified in §5). `SKIP` = not
applicable/deselected. `BLOCKED` = environment/infrastructure prevented
execution (never used for a real result).

### 3.1 Python — unit, integration, architecture, static

| Area | Command | Result | Count | Blocker | Notes |
|---|---|---|---|---|---|
| Full regression | `.venv/bin/python -m pytest -q tests deeptutor/learning/tests` | **PASS** | 8,419 passed / 54 skipped / 37 warnings | none | 478.91 s. **Exactly matches the Phase 28 record** |
| Collection | `pytest --collect-only` | PASS | 8,472 collected, 0 errors | none | required `requirements/partners.txt` for collection |
| Ruff lint | `.venv/bin/ruff check .` | **PASS** | all checks passed | none | |
| Ruff format | `.venv/bin/ruff format --check .` | **FAIL (pre-existing)** | 2 files would be reformatted, 1,898 already formatted | none | `deeptutor/services/codex_auth/oauth.py`, `deeptutor/tools/tex_downloader.py` — identical to the Phase 28 record |
| Import architecture | `.venv/bin/lint-imports` | **PASS** | 3 kept / 0 broken (971 files, 3,593 deps) | none | |
| Application architecture | `.venv/bin/python scripts/check_architecture.py` | **PASS** | `Architecture boundaries: OK` | none | |
| Compile | `python -m compileall -q deeptutor deeptutor_cli scripts` | **PASS** | rc 0 | none | |
| CI import checks | 7 CI modules | **PASS** | 7/7 | none | |
| Lazy startup | tool registry stays cold | **PASS** | 1/1 | none | |
| Isolated worker | `run_in_isolated_process_sync` | **PASS** | 1/1 | none | |
| Bandit | `bandit -c pyproject.toml -q -r deeptutor deeptutor_cli scripts` | **PASS** | 0 issues | none | warnings only (`nosec`/comment parsing) |
| detect-secrets | `detect-secrets-hook --baseline .secrets.baseline` | **PASS** | rc 0 | none | |
| Repo hygiene | `check_workspace_hygiene.py`, `check_repo_hygiene.py` | **PASS** | 2/2 | none | |
| Coverage (P0 surfaces) | `pytest tests/multi_user tests/api tests/architecture tests/runtime --cov=…` | **PASS** | 1,223 passed / 3 skipped, total **66 %** | none | see §7 |

### 3.2 Frontend — unit, contract, architecture, static, i18n

| Area | Command | Result | Count | Blocker | Notes |
|---|---|---|---|---|---|
| Contract mirror | `npm run contracts:check` | **PASS** | rc 0 | none | OpenAPI → TS mirror is in sync |
| Dependency architecture | `npm run architecture:check` | **PASS** | 880 modules, 2,617 deps, 0 violations | none | |
| TypeScript | `npm run typecheck` | **PASS** | rc 0 | none | |
| Node tests | `npm run test:node` | **PASS** | 1,220 passed, 0 failed, 0 skipped | none | matches Phase 28 |
| Vitest | `npm run test:unit` | **PASS** | 108 files / 427 tests passed | none | matches Phase 28 |
| ESLint | `npm run lint` | **PASS** | 0 errors, 49 warnings | none | matches Phase 28 |
| i18n parity | `npm run i18n:parity` | **PASS** | rc 0 | none | 4 locales: en, fr, uk, zh |
| i18n audit | `npm run i18n:audit` | **PASS** | rc 0 | none | residual hits are brand/technical strings (`MO7`, `Redis`, `SOUL.md`, `sk-…`) |
| i18n (Python) | inside full pytest (`tests/i18n`) | **PASS** | included above | none | |
| Frontend coverage | `vitest --coverage` | **BLOCKED** | — | `@vitest/coverage-v8` is not installed and was not added | stated, not claimed |

### 3.3 Runtime

| Area | Command | Result | Count | Blocker | Notes |
|---|---|---|---|---|---|
| API startup (auth off) | `DEEPTUTOR_HOME=… uvicorn deeptutor.api.main:app` | **PASS** | 1/1 | none | clean startup, no LLM configured |
| Liveness | `GET /health/live` | **PASS** | 200 `{"status":"alive"}` | none | 18 bytes — matches Phase 28 |
| Readiness | `GET /health/ready` | **PASS** | 200 `{"status":"ready"}` | none | 18 bytes — matches Phase 28 |
| Product identity | `GET /` | **PASS** | 200 `{"message":"Welcome to MO7 API"}` | none | 32 bytes — matches Phase 28 |
| OpenAPI | `GET /openapi.json` | **PASS** | 200 | none | |
| Web production server | `next start` | **PASS** | ready in 135 ms, HTTP 200 on all probed routes | none | |

Note: with `AUTH_ENABLED=false` (the shipped default) protected routes return
200 without a token. This is **by design** — `require_auth` is a documented no-op
in single-user mode (`deeptutor/api/main.py:587`), and `/api/auth/status`
reports `{"enabled": false, …}`. It is **not** an access-control defect. The
real boundary was verified separately with auth enabled (§4).

### 3.4 Security (runtime, auth enabled)

A second isolated instance was started with `data/user/settings/auth.json →
enabled: true` on a clean `DEEPTUTOR_HOME`. Probe: `/tmp/mo7_security_probe2.py`.

| # | Check | Result | Observed |
|---|---|---|---|
| 1 | `/api/auth/status` reports enabled | **PASS** | `200`, `enabled=true` |
| 2 | status unauthenticated | **PASS** | `authenticated=false` |
| 3 | anonymous access rejected | **PASS** | 401 on `/api/multi-user/users`, `/api/sessions`, `/api/settings`, `/api/knowledge-bases`, `/api/notebooks` |
| 4 | forged / malformed tokens rejected | **PASS** | 401 for non-JWT garbage, empty, header-only, forged HS256, **`alg=none`**, expired-shaped |
| 5 | first-user bootstrap registration | **PASS** | 201 |
| 6 | admin login grants admin role | **PASS** | `200`, `role=admin`, `is_admin=true` |
| 7 | admin can read user list | **PASS** | 200 |
| 8 | admin can provision a user | **PASS** | 201 |
| 9 | self-registration closed after first admin | **PASS** | 403 |
| 10 | second user logs in as non-admin | **PASS** | `200`, `role=user` |
| 11 | non-admin cannot read user list | **PASS** | 403 |
| 12 | tenant isolation: new user's session list | **PASS** | `200`, `[]` |
| 13 | non-admin can read own settings | **PASS** | 200 |
| 14 | wrong password rejected | **PASS** | 401 |

**14/14 PASS.** Static SAST (Bandit) and secret scanning (detect-secrets) also
passed (§3.1). Phase 27 TeX archive containment is untouched and its tests pass
inside the green full regression.

### 3.5 Browser / E2E (first successful execution in the MO7 phases)

| Project | Command | Result | Count | Blocker | Notes |
|---|---|---|---|---|---|
| `ui-audit` (CI gate) | `WEB_BASE_URL=http://127.0.0.1:3000 npx playwright test --project=ui-audit` | **FAIL** | **57 passed / 4 failed** | none | never run before; 3.2 min |
| `ui-audit` isolated | `PW_SERIAL=1 --workers=1` on the 3 suspect specs | **FAIL** | 2 passed / 3 failed | none | `video-learning` passes serially → flaky |
| `critical-turns` | `--project=critical-turns` | **SKIP (by design)** | 3 skipped | deterministic backend turn fixture | gated on `DEEPTUTOR_TURN_E2E_FIXTURE=1` |
| `epub-reader-chromium` | `--project=epub-reader-chromium` | **FAIL** | 2 failed | none | stale spec, §5 |
| `epub-reader-webkit` | — | **BLOCKED** | — | no WebKit build obtainable offline | not claimed |
| `multi-worker-turns-*` | — | **BLOCKED** | — | needs Redis + 4-worker fixture + `DEEPTUTOR_MULTI_WORKER_E2E` | not claimed |

### 3.6 Performance regression vs Phase 28

Phase 28 recorded route budgets as **not measurable** (no production build).
This phase produced a build, so `npm run perf:check` ran for the first time.

| Route | Measured | Budget | Result |
|---|---:|---:|---|
| `/` | 969 KB | 300 KB | **FAIL — harness defect, §5** |
| `/chat/[sessionId]` | 969 KB | 1,020 KB | OK |
| `/settings` | 113 KB | 840 KB | OK |
| `/knowledge-bases` | 314 KB | 550 KB | OK |
| `/co-writer` | 255 KB | 320 KB | OK |
| `/co-writer/[docId]` | 328 KB | 515 KB | OK |
| `/learning/reading/[workspaceId]/sessions/[sessionId]` | 1,128 KB | 1,120 KB | **FAIL — real, §5** |
| `/learning/mastery/[pathId]/sessions/[sessionId]` | 898 KB | 980 KB | OK |
| root-app-shell | 272 KB | 390 KB | OK |

Phase 28 runtime numbers reproduced: `/health/live` and `/health/ready` return
200 with the same 18-byte payloads, and `/` the same 32-byte payload.

---

## 4. Coverage graph

```
USER → UI → BFF/API → SERVICE → DATABASE/FILES → EXTERNAL DEP
     → RESPONSE → PERSISTED STATE
```

| Node / edge | Status | Evidence |
|---|---|---|
| Auth (login, JWT, cookie, bootstrap, role) | **tested** | 14/14 runtime probe; `tests/api/test_auth_*`, `tests/multi_user/*` |
| Authorization / role boundaries | **tested** | non-admin 403 on admin route; `test_role_normalization`, `test_grants_and_settings` |
| Tenant isolation | **tested** | empty session list for a fresh user; `test_resource_isolation`, `test_learning_records_isolation` |
| API contracts (OpenAPI ↔ TS) | **tested** | `contracts:check`, `tests/api/test_frontend_contract_export.py` |
| Architecture / import direction | **tested** | `lint-imports`, `check_architecture.py`, dependency-cruiser |
| Persistence (SQLite, sessions, settings) | **tested** | `tests/api/test_sessions_*`, `tests/core`, `tests/services` |
| Archive containment (Phase 27) | **tested** | `tests/api/test_knowledge_zip_upload.py` + full green regression |
| UI interaction / accessibility | **partially tested** | 57 UI audits green; 3 specs stale |
| Browser ↔ BFF contract | **partially tested** | audits mock `**/api/**`; a live BFF round-trip is not covered |
| Multi-worker turn coordination | **untested (blocked)** | needs Redis + 4-worker fixture |
| Critical turn lifecycle | **untested (blocked)** | needs `DEEPTUTOR_TURN_E2E_FIXTURE=1` |
| LLM-backed answer quality | **untested (blocked)** | no LLM configured |
| WebKit/iOS rendering | **untested (blocked)** | no WebKit build obtainable |

### Coverage measurement (P0 surfaces only)

Scoped run over `tests/multi_user`, `tests/api`, `tests/architecture`,
`tests/runtime`: 1,223 passed / 3 skipped, **total 66 %**.

| Module group | Coverage |
|---|---|
| `deeptutor/multi_user/learner_profile.py` | 97 % |
| `deeptutor/multi_user/paths.py` | 96 % |
| `deeptutor/multi_user/models.py` | 94 % |
| `deeptutor/multi_user/book_access.py` | 94 % |
| `deeptutor/multi_user/book_permission.py` | 93 % |
| `deeptutor/multi_user/learning_access.py` | 93 % |
| `deeptutor/multi_user/audit.py` | 92 % |
| `deeptutor/multi_user/skill_access.py` | 92 % |
| `deeptutor/multi_user/device_credentials.py` | 91 % |
| `deeptutor/multi_user/grants.py` | 91 % |
| `deeptutor/multi_user/tool_access.py` | 91 % |
| `deeptutor/multi_user/context.py` | 91 % |
| `deeptutor/multi_user/guardians.py` | 89 % |
| `deeptutor/multi_user/model_access.py` | 89 % |
| `deeptutor/multi_user/session_handoff.py` | 87 % |
| `deeptutor/multi_user/personal_models.py` | 86 % |
| `deeptutor/multi_user/identity.py` | 83 % |
| `deeptutor/services/auth.py` | **72 %** |
| `deeptutor/multi_user/knowledge_access.py` | **67 %** |

Security-critical untested / weakly-tested paths found:

- `deeptutor/services/auth.py` lines 364–383, 392–407, 423–431, 450 (token /
  device-auth branches).
- `deeptutor/multi_user/knowledge_access.py` lines 237–246, 263–274, 341–361
  (knowledge-base access denial branches).
- Low-coverage API routers in this scoped run (memory 32 %, question 31 %,
  quiz_judge 9 %, video_learning 30 %, reading 44 %, unified_ws 46 %) — these
  routers are exercised by suites outside the scoped selection, so the numbers
  are a **selection artifact** and are deliberately not presented as gaps.
- Frontend line coverage: **not measured** (no coverage provider installed).

---

## 5. Failure classification and dispositions

Every observed failure has exactly one primary category.

### F1 — `perf:check` `/` row: 969 KB vs 300 KB

- **Category: TEST BUG (harness/config)** — not an application defect.
- **Reproduce:** `cd web && NEXT_FONT_GOOGLE_MOCKED_RESPONSES=… npm run build && npm run perf:check`
- **Root cause:** `scripts/route_budgets.mjs` measures each row with `fetch()`,
  which follows redirects. `GET /` returns **302 → `/chat`**
  (`curl -w '%{num_redirects}'` = 1, `final=/chat`), and
  `app/(workspace)/page.tsx` is intentionally a bare `redirect("/chat")` —
  its own comment states the workspace root has one destination. The harness
  therefore sizes the **`/chat`** payload against a 300 KB budget. Proof: the
  `/` and `/chat/[sessionId]` chunk sets are 43 chunks each and differ by
  exactly one file (`app/(workspace)/chat/page-*.js`), and both rows report
  969 KB. The `/chat` payload already has its own 1,020 KB budget, which it
  passes.
- **Affected graph path:** harness → production server → HTML → chunk sizes.
- **Disposition:** documented, **not changed**. Repairing it means either
  rewriting the harness to reject redirects (which would make the `/` row read
  ~0 KB and pass trivially — indistinguishable from weakening the gate) or
  restating the `/` budget (changing an acceptance criterion). Both are owner
  decisions outside QA change discipline.
- **Recommended fix (owner):** assert `response.redirected === false` per target,
  and either drop the `/` row or point it at `/chat` with the chat budget.

### F2 — `perf:check` reading-session row: 1,128 KB vs 1,120 KB (+0.7 %)

- **Category: APPLICATION BUG (performance budget breach), low severity.**
- **Reproduce:** as F1; the target returns **200 with 0 redirects**, so it is a
  genuine measurement of `/learning/reading/[workspaceId]/sessions/[sessionId]`.
- **Root cause:** page-level client chunks for the reading session route exceed
  the declared budget by 8 KB. No prior measurement exists (Phases 27/28 could
  not build), so this is a **pre-existing** condition, not a Phase 29 regression.
  The font mock emits fewer `@font-face` blocks than the live Google response,
  so the true figure is equal or slightly larger — the FAIL direction is robust,
  but the exact magnitude should be re-measured on a network-enabled runner.
- **Affected graph path:** UI → route chunk graph (no API/service involvement).
- **Disposition:** documented, **not fixed**. A fix is bundle-level optimisation
  across feature modules — not a small, safe, behaviour-preserving change, and
  explicitly out of scope for a QA phase.

### F3 — `reading-location-history.audit.ts:254` (History button not found)

- **Category: TEST BUG (stale spec).**
- **Root cause:** the reader's History action is no longer a top-level header
  button. `components/reading/ReaderPane.tsx` builds it as a workspace **menu**
  item (`useWorkspaceMenuSection("material", menuItems)`, `key: "history"`,
  `onSelect: () => setShowHistory(true)`) and the file's own comment states that
  history, auto-jump, export and the notes panel "live under ⋯". The spec still
  clicks a standalone `button "History"`.
- **Evidence:** the failure snapshot contains the material title, Back/Forward,
  section counter and Bookmark button — but no History button; `locationHistory`
  is restored (`Back` enabled, `Forward` disabled after the seeded 2-entry
  history), so the action *is* present in the `⋯` menu.
- **Disposition:** documented, **not changed** — see §6 rationale.

### F4 — `reading-location-history.audit.ts:217` (history entry not clickable after reload)

- **Category: TEST BUG (stale spec).**
- **Root cause:** two compounding staleness issues.
  (a) History entries render as buttons only inside the panel, which is gated on
  `showHistory && locationHistory.entries.length > 0`, and `showHistory` is only
  set from the `⋯` menu.
  (b) `getByRole("button", { name: "History material B" })` is a **substring**
  match, so earlier in the same spec it silently matched the *outline* entry
  "History material B first/second" rather than a history entry. After
  `page.goto()` to a new session the outline is collapsed, so nothing matches.
- **Disposition:** documented, **not changed** — see §6 rationale.

### F5 — `reading-w3c-annotations.audit.ts:171` (annotation sidebar entry not visible)

- **Category: TEST BUG (stale spec)** — same class as F3/F4.
- **Root cause:** the spec expects a companion `button` whose text contains
  "Wave behavior" to be visible at that point; the annotation list is gated on
  `annotationPanel ?? annotations.length > 0` and the companion's annotation
  affordance now lives under the same `⋯` menu. The snapshot shows the reader
  rendered with no annotation sidebar entry.
- **Disposition:** documented, **not changed** — see §6 rationale.

### F6 — `video-learning.audit.ts:494` (`toHaveClass(/ring-1/)`)

- **Category: FLAKY / INTERMITTENT.**
- **Evidence:** failed in the parallel `ui-audit` run; **passed** in the isolated
  `PW_SERIAL=1 --workers=1` re-run of the same spec. The assertion races the
  transcript cue highlight (the class is absent on the first samples and the
  active-cue state settles later), which the parallel run's CPU contention
  exposes.
- **Disposition:** documented, **not changed**. A fix would be an explicit
  wait-for-state before asserting, which is a test-authoring decision.

### F7 — `epub-reader.audit.ts:50` and `:105` (file input not found)

- **Category: TEST BUG (stale spec).**
- **Root cause:** the spec targets
  `getByRole("button", { name: /Open a document to read/i })` on
  `/chat?capability=immersive_reading`. The string `"Open a document to read"`
  exists **only** in the i18n bundles and in this spec — a repository-wide grep
  over `*.ts`/`*.tsx` finds no component that renders it. The affordance was
  renamed or relocated; the spec is stale. The page snapshot shows the chat
  workspace rendered normally with no such control.
- **Note:** the `ui-audit` project (and therefore CI's `npm run audit`) excludes
  `epub-reader.audit.ts` via `testIgnore`; it is a separate, non-default project.
- **Disposition:** documented, **not changed**.

### F8 — `ruff format --check`: 2 files

- **Category: EXPECTED / KNOWN (pre-existing).**
- **Root cause:** `deeptutor/services/codex_auth/oauth.py` and
  `deeptutor/tools/tex_downloader.py` predate this phase and are unchanged by
  it. Phase 28 recorded the identical pair.
- **Disposition:** documented, **not changed**. `tex_downloader.py` carries the
  Phase 27 archive-containment code, whose tree the phase must preserve exactly;
  reformatting two unrelated files is also an unrelated change.

### F9 — blocked suites (not failures)

| Suite | Category | Reason |
|---|---|---|
| `critical-turns` (3 tests) | INFRASTRUCTURE / FIXTURE | gated on `DEEPTUTOR_TURN_E2E_FIXTURE=1`; needs the deterministic backend turn fixture plus Redis |
| `multi-worker-turns-*` | INFRASTRUCTURE | needs Redis + a 4-worker deployment and `DEEPTUTOR_MULTI_WORKER_E2E_URL` |
| `epub-reader-webkit` | INFRASTRUCTURE | Playwright WebKit build unobtainable offline |
| Frontend coverage | DEPENDENCY | no coverage provider installed |
| LLM-backed behaviour | INFRASTRUCTURE | no LLM model configured |

### Classification tally

| Category | Count |
|---|---|
| APPLICATION BUG | 1 (F2, low severity) |
| TEST BUG | 5 (F1 harness/config + F3/F4/F5/F7 stale specs) |
| FIXTURE/TEST-DATA BUG | 0 |
| DEPENDENCY ISSUE | 0 |
| ENVIRONMENT ISSUE | 0 |
| INFRASTRUCTURE ISSUE | 5 blocked suites (F9) |
| EXPECTED/KNOWN FAILURE | 1 (F8) |
| FLAKY/INTERMITTENT | 1 (F6) |
| INSUFFICIENT EVIDENCE | 0 |

---

## 6. Change discipline — and why no source was modified

**Changed files: this report only.** No production code, no test code, no
dependency, no lockfile, no configuration was modified.

Justification for deliberately not fixing F1–F7:

- Every one of them is a **non-application** defect (F2 is the only application
  finding, and it is a bundle-size budget breach).
- The phase rules forbid modifying production or weakening assertions to obtain
  green, and forbid unrelated refactors.
- F3/F4/F5/F7 have **never passed in this repository's recorded history** — no
  MO7 phase has ever executed the browser suite (Phases 27 and 28 were both
  blocked). There is therefore no known-good baseline proving what these specs
  ever asserted, so "updating" them would mean *authoring new expectations* —
  test development, not QA validation — with a real risk of replacing a red gate
  with a vacuously green one.
- F1's repair either trivially passes the row (~0 KB) or edits an acceptance
  budget; both require an owner decision.

### Recommended owner actions

1. Decide `/` in `route_budgets.mjs`: assert non-redirect, then drop the row or
   retarget it to `/chat`.
2. Treat the reading-session route as a real, tracked 8 KB budget breach.
3. Modernise `reading-location-history.audit.ts`, `reading-w3c-annotations.audit.ts`
   and `epub-reader.audit.ts` to the current `⋯`-menu interaction model, or
   deliberately restore the top-level affordances.
4. Stabilise `video-learning.audit.ts:494` with an explicit wait.
5. Reformat the two files in `ruff format --check` (Phase 27 file included) in a
   dedicated formatting-only change.
6. Provision the deterministic turn fixture and Redis in CI so
   `critical-turns` / `multi-worker-turns-*` can execute.
7. Add `@vitest/coverage-v8` if frontend coverage is wanted.

---

## 7. Infrastructure blockers vs application defects

| Item | Classification | Evidence |
|---|---|---|
| Google Fonts TLS reset blocking `next build` | INFRASTRUCTURE — **worked around** with Next's documented offline font mock; no source change | F1/F2 reproduction; build succeeded |
| Playwright CDN unreachable | INFRASTRUCTURE — **worked around** with an npm-packaged Chromium 143 | CDP live; 61 UI audits executed |
| `registry.npmmirror.com` in the lockfile | INFRASTRUCTURE — worked around with `--replace-registry-host=always`; lockfile untouched | 951 packages installed |
| Redis absent | INFRASTRUCTURE — multi-worker and redis-marked tests not executed | no server binary, no Docker, apt blocked |
| Deterministic turn fixture absent | INFRASTRUCTURE — `critical-turns` self-skips | `DEEPTUTOR_TURN_E2E_FIXTURE` unset |
| WebKit absent | INFRASTRUCTURE — iOS/Safari rendering unverified; **not claimed** | no obtainable build |
| No LLM configured | INFRASTRUCTURE — LLM-backed quality unverified | startup warning |
| Route-budget `/` row | **not** an application defect (harness) | F1 |
| Reading-session route over budget | **application defect**, low severity | F2 |

No blocked test was reported as an application failure, and no application
failure was hidden behind a blocker label.

---

## 8. Definition of Done

| Item | State |
|---|---|
| Repository baseline verified | ✅ |
| QA matrix completed | ✅ (every applicable area executed; blockers named) |
| All practical P0 tests executed | ✅ |
| Relevant P1 tests executed | ✅ |
| Application failures classified | ✅ |
| Genuine application bugs fixed where safe | ⚠️ 1 found (F2), none safely fixable in-phase |
| Regression tests added/updated where justified | ⚠️ none — the only app finding is a bundle-size budget breach |
| Full relevant regression executed after fixes | ✅ (no source changed; regression re-verified) |
| Security QA completed where environment allows | ✅ 14/14 runtime + Bandit + detect-secrets |
| Contract validation completed | ✅ |
| Architecture validation completed | ✅ |
| Static quality validation completed | ⚠️ Ruff lint/TS/ESLint green; `ruff format --check` red (pre-existing, 2 files) |
| i18n validation completed | ✅ |
| Runtime smoke validation completed | ✅ |
| Browser/E2E status explicitly verified | ✅ executed; `ui-audit` 57/61, blockers named |
| Coverage gaps documented | ✅ |
| Infrastructure blockers separated from defects | ✅ |
| No tests weakened/deleted to achieve green | ✅ nothing changed |
| No unrelated changes introduced | ✅ |
| Working tree clean (no tracked modifications) | ✅ |
| Changes committed | ✅ |
| Changes pushed | ✅ |
| Final evidence report generated | ✅ this document |
| Phase 27 status preserved | ✅ `VALIDATION BLOCKED — INFRASTRUCTURE` |
| Phase 28 status preserved | ✅ `VALIDATION BLOCKED — INFRASTRUCTURE` |
| Final QA state reproducible | ✅ §9 |
| **Phase 29 READY** | ❌ gates still red |

---

## 9. Reproduction

```bash
# --- Python ---
python3 -m venv .venv && .venv/bin/pip install -r requirements/dev.txt
.venv/bin/pip install -r requirements/partners.txt -e . --no-deps
.venv/bin/pip install ruff==0.16.0 detect-secrets pytest-cov
.venv/bin/python -m pytest -q tests deeptutor/learning/tests      # 8419 passed, 54 skipped
.venv/bin/ruff check . && .venv/bin/ruff format --check .          # lint OK; format shows 2 files
.venv/bin/lint-imports && .venv/bin/python scripts/check_architecture.py
.venv/bin/python -m bandit -c pyproject.toml -q -r deeptutor deeptutor_cli scripts
.venv/bin/detect-secrets-hook --baseline .secrets.baseline

# --- Frontend ---
cd web && npm ci --legacy-peer-deps --replace-registry-host=always
npm run contracts:check && npm run architecture:check && npm run typecheck
npm run test:node && npm run test:unit && npm run lint && npm run i18n:check

# --- Production build + budgets (needs the offline font mock) ---
NEXT_FONT_GOOGLE_MOCKED_RESPONSES=/tmp/mo7-font-mock.cjs npm run build
npm run perf:check

# --- Browser audits (needs the Chromium 143 install described in §2) ---
npx next start --hostname 0.0.0.0 --port 3000 &
WEB_BASE_URL=http://127.0.0.1:3000 npx playwright test --project=ui-audit

# --- Runtime + security ---
DEEPTUTOR_HOME=/tmp/mo7-phase29-runtime python -m uvicorn deeptutor.api.main:app --port 8765
curl -s localhost:8765/health/live && curl -s localhost:8765/health/ready
# auth-enabled probe: put {"enabled": true} at $DEEPTUTOR_HOME/data/user/settings/auth.json,
# start on :8766, then run /tmp/mo7_security_probe2.py

# --- Coverage (P0 surfaces) ---
.venv/bin/python -m pytest -q tests/multi_user tests/api tests/architecture tests/runtime \
  --cov=deeptutor.multi_user --cov=deeptutor.api --cov=deeptutor.services.auth
```

---

## 10. Final status

`PHASE 29 NOT READY`

Reason: the QA matrix was executed to the limit of the environment and produced
complete, classified evidence, but validation gates remain red — the browser
`ui-audit` project (3 stale specs), `npm run perf:check` (1 harness defect plus 1
real budget breach), and `ruff format --check` (2 pre-existing files) — and none
of them were safely repairable without violating QA change discipline.

This is **not** a `VALIDATION BLOCKED — INFRASTRUCTURE` result: the previously
blocking infrastructure (Google Fonts, Playwright Chromium) was successfully
worked around without touching the repository, and the browser and performance
gates ran for the first time. The remaining blocked suites (WebKit, Redis
multi-worker, deterministic turn fixture, LLM) are listed explicitly in §7 and
are not counted as application failures.

No automatic progression to Phase 30.
