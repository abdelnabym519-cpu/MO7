# Phase 28 — Performance & Stability, End-to-End Closure Report

**Final status: `PHASE 28 READY`**

- Repository: `MO7`, branch `arena/01a0ff3f-mo7`, HEAD `5b0d334` (Phase 27 closure) plus the Phase 28 changes listed in §4.
- Environment: 2 vCPU / 3.9 GB RAM container, Python 3.11.2, Node v22.22.3, Next.js 16.3.3, SQLite storage, no Redis/LLM/Docker (external services unavailable — see §8.3).
- Date: 2026-10-05.
- Historical baseline for comparison: the Phase 28 performance baseline recorded in `docs-for-user/PHASE_28_PERFORMANCE_BASELINE.md` and the Phase 26/27/29 reports.

The historical Phase 28 result was `VALIDATION BLOCKED — INFRASTRUCTURE`: the baseline measurements existed but had never been re-validated end-to-end on the current tree, and several Phase 27/29 gates were open. This report reopens that result, re-measures everything in this environment, separates genuine defects from harness artifacts, and closes the phase.

---

## 1. Baseline (historical vs current)

All current numbers were measured on `5b0d334` + the Phase 28 working-tree changes (no production code was changed in this phase, so the running code is `5b0d334`). Cold and warm metrics are never mixed.

| Metric | Historical / baseline doc | Current (2026-10-05, this environment) | Verdict |
|---|---|---|---|
| Cold start → `/health/live` 200 | ≈2 519.9 ms median | min 2 952.9 / **p50 3 170.3** / max 3 893.8 ms (5 samples) | Environment difference, not a regression (see A/B below) |
| `import deeptutor.api.main` | ≈1 840 ms | **p50 2 026.3 ms** | Same shape as base worktree |
| Orchestrator import | ≈187 ms | 185.1 ms | Unchanged |
| Container import | ≈425 ms | 512.8 ms | Same component, environment-scaled |
| Warm `/health/live` | ≈2.62 ms | p50 **2.151** / p95 2.435 ms (100 samples) | Improved/equal |
| Warm `/` | ≈2.60 ms | p50 **2.254** / p95 2.923 ms | Equal |
| Warm `/health/ready` | ≈2.81 ms | p50 **2.184** / p95 2.533 ms | Equal |
| 500-request memory smoke | flat RSS | RSS 134 808 → 135 068 KiB, 6 threads, 16 fds | Flat |
| Reading-session bundle | ≈1 128 KB vs 1 120 KB budget | **1 128 KB / 1 120 KB** | Reproduced exactly (§6) |

**A/B proof that the cold-start delta is the environment, not a code regression.** The historic commit `37f4f4f97db7d1e05fc25eaf403ea0b83c9975f8` was checked out into a separate git worktree (`/home/user/mo7-p28-base`) and measured with the same harness:

| Phase | Base `37f4f4f` | HEAD `5b0d334` |
|---|---|---|
| uvicorn import | 26.1 ms | 26.0 ms |
| `api.main` import p50 | 1 762.4 ms | 1 702.1 ms |
| bind → first 200 | 2 810.8 ms | 2 774.1 ms |
| ready | 1 025.7 ms | 1 021.5 ms |

The two commits produce the same startup shape within noise, so the ≈600 ms gap against the historic 2 519.9 ms figure is the sandbox, not Phase 26/27 work. Both figures are reported, neither is merged.

---

## 2. Methodology

- **Harnesses** (all re-runnable, kept outside the repository as phase tooling):
  - `mo7_p28_baseline.py` / `mo7_p28_baseline_results.json` — cold start, import timing, warm latency percentiles, 500-request memory smoke.
  - `mo7_p28_startup_phases.py` + `mo7_p28_startup_child.py` → `/tmp/phases_base.json`, `mo7_p28_startup_phases.json` — phase-level cold start on both commits.
  - `mo7_p28_startup_timeline.py`, `mo7_p28_lifespan_profile.py`, `/tmp/mo7_p28_importtime.txt` — ordered startup timeline, cProfile of the first request, `-X importtime`.
  - `mo7_p28_stability.py` → `mo7_p28_stability_results.json` — phases A–H: cold start + endpoint probe, warm latency matrix, concurrency, malformed requests, soak, unavailable dependency, SIGKILL recovery, repeated start/stop.
  - `mo7_p28_bundle_audit.mjs` → `/tmp/mo7_p28_bundle_audit.json` — production `next start`, per-route chunk fetching, chunk-size and marker labeller.
  - `web/scripts/route_budgets.mjs` + `web/tests/route-budgets-harness.test.ts` — the route-budget gate and its own tests.
- **Rules applied**: cold measurements are separated from warm ones; every number is a percentile or a range, never a single favourable sample; cProfile inflation is called out where relevant; the browser matrix and the heavy Python suites were run with no other load on the box; leftover `uvicorn`/`next start`/Playwright processes were killed between runs.
- **Classification** uses exactly one label per finding from: `REAL PERFORMANCE DEFECT` / `REAL STABILITY DEFECT` / `REAL MEMORY/RESOURCE DEFECT` / `HARNESS DEFECT` / `TEST DEFECT` / `INFRASTRUCTURE BLOCKER` / `KNOWN FINDING` / `OUT OF SCOPE`.

---

## 3. Findings

### 3.1 Cold start is dominated by framework-level lazy route materialisation (no defect)

- **Evidence.** Startup timeline (`/tmp/mo7_p28_startup_timeline.txt`, seconds): uvicorn import 0.045 → app import done 1.800 → lifespan startup 1.828 → socket 1.953 → **first 200 at 2.853** → shutdown 3.051–3.606. The container itself is cheap: `Container.start()` 0.000 s, migrations 0.039 s. At import time FastAPI sees only `{_IncludedRouter: 51, Route: 4, APIRoute: 3}`; the 51 included routers hold 597 `@router.` decorators and become 620 API routes lazily. The **first `/health/live` request costs 884.1 ms** (unprofiled; cProfile roughly doubles it to 2 054 ms and is used for call counts only); the second request costs 4.2 ms. Profile: `_build_effective_context` 2.006 s, `_populate_api_route_state` 1.961 s over 620 calls, `get_dependant` 1.610 s, `create_model_field` 1.428 s.
- **Import composition** (`/tmp/mo7_p28_importtime.txt`): cumulative 1 764.1 ms — `deeptutor` 1 200.1 ms / 494 modules, third-party + stdlib 580.8 ms / 860 modules; heaviest third-party `fastapi` 236.0 ms, `aiohttp` 109.1 ms, `httpx` 68.2 ms; heaviest product chains `model_selection.tasks` 231.4 ms and `llm.config` 229.5 ms; heaviest self-time `settings` 111.4 ms, `knowledge` 70.5 ms, `reading` 61.0 ms.
- **Classification:** `OUT OF SCOPE` (one-time cost inside the framework's lazy route/DI design; the warm path is unaffected).
- **Impact:** the first request after a cold start pays ≈0.9 s once; every later request is 2–4 ms.
- **Action:** deferred deliberately. Making the first request fast would require eager route/Pydantic construction (moving the cost back into import, which is exactly what makes cold start long) or a different routing layer — an architecture change beyond Phase 28's scope, with no user-visible latency benefit after the first request. The 1.7–1.8 s import is ordinary dependency loading with no accidental heavyweight import found on the reading/startup path.

### 3.2 Route-budget harness is trustworthy (harness inspected, no defect)

- **Evidence.** `node --test --experimental-strip-types tests/route-budgets-harness.test.ts` → **4 pass / 0 fail**. The harness starts its own server, fails loudly instead of following redirects (the `/` entry asserts a redirect rather than silently measuring `/chat`), and the measured artifact is the requested route.
- **Reproducibility check.** Two independent production builds produced a **byte-identical chunk set** — 355 JS chunks, identical paths and sizes (combined signature `d0958b0f…`); only `build-manifest.json` differs (build id). `perf:check` returned the identical 9 route numbers on both builds.
- **Classification:** no finding.

### 3.3 Reading-session bundle is 8 KB (0.7 %) over its 1 120 KB budget (known finding, broader work required)

- **Evidence.** `perf:check`: reading-session **1 128 KB / 1 120 KB FAIL**, all other routes OK (`/chat/[sessionId]` 969/1 020, settings 113/840, knowledge-bases 314/550, co-writer 255/320, co-writer/[docId] 328/515, mastery 898/980, root-app-shell 272/390, `/` redirect). Identical on both builds.
- **Cause analysis** (`/tmp/mo7_p28_bundle_audit.json`, `web/.next/server/.../page_client-reference-manifest.js` diff, minified chunk-content markers, k-64 shingle comparison, AST walk of the reading route's 294-module static import graph):
  - Reading loads 44 chunks / 1 400 KB vs mastery's 40 / 1 169 KB (excluding framework).
  - 7 chunks are unique to reading = 272 KB. Of those, **≈200 KB is genuinely reading-only**: `static/chunks/4064-*.js` 158 KB (epub.js, needed by the sanctioned EPUB reader) and `static/chunks/6267-*.js` 41 KB (KaTeX + question/tutor review UI).
  - The remaining 72 KB (`7639` turn/debate UI 34 KB, `907` `/api/courses` client 28 KB, `909` visibility/keyboard nav 10 KB, plus two 0 KB manifests) is **not dead code and not duplicated**: `UseReadingWorkspace` → `ReadingWorkspace` statically imports `ReadingCompanion`, which statically imports the shared chat stack (`ChatMessageList`, `SessionViewerPanel`, `ChatViewerBridges`, `ReadingActionCards`). That companion is a visible-by-default panel of the reading workspace; the same shared stack already serves `/chat/[sessionId]` (969 KB, in budget).
  - No duplication to reclaim: the only overlap among the reading-only chunks is `3775 ⊂ 4064` (webpack's normal shared-chunk split, already counted once). Markers: remark 296 KB, mermaid 294 KB, marked 236 KB, epub 158 KB, katex 42 KB — all reached through `import()`/`next/dynamic` except the reading-route graph's single heavy static import, `lib/pdfjs-loader.ts → pdfjs-dist` (itself a dynamic `import()` inside).
- **Classification:** `KNOWN FINDING` (already documented as Phase 29 report §4.1).
- **Impact:** the reading route ships 8 KB more raw JS than its 1 120 KB ceiling — well under 1 % over, on the heaviest legitimate workspace in the product.
- **Action:** **budget left unchanged** (raising it without evidence it is wrong is forbidden). Shedding the 8 KB would require deferring the reading companion panel — a UX-visible code-splitting/architecture change (the companion is part of the workspace by design, and the EPUB/KaTeX bytes serve a Phase 26/27-protected feature) — so it is documented as needing broader out-of-scope work rather than forced into this phase.

### 3.4 EPUB spine-switch assertion used the default 5 s window (real test defect — fixed)

- **Evidence.** The Chromium matrix's first Phase 28 run: **61 passed / 2 failed**, both `[epub-reader-chromium]` (`web/tests/epub-reader.audit.ts`), same post-click locator `locator('iframe').contentFrame().getByRole('heading', { name: 'Second chapter' })`, "element(s) not found" at the default 5 000 ms timeout. The preceding steps (library flow, contents panel, "Second chapter" button found and clicked) all passed. Re-running the same two specs alone: **2 passed in 7.4 s**; a full matrix rerun: **63 passed**. Phase 29 report §4.4 independently recorded ≈4.5–5 s spine switches under parallel load for a tall first chapter — exactly at the 5 s default.
- **Classification:** `TEST DEFECT`.
- **Impact:** the EPUB reader itself behaves correctly; a correct assertion sat exactly on a load-dependent timing boundary, producing a non-reproducible red.
- **Action:** the two post-click assertions now request an explicit **15 s** window (assertions and expectations unchanged), with a comment recording the measured cost and the reason. No production code, no assertion weakened — full matrix is green (§7.5).

### 3.5 Phase 27 harness scripts broke `ruff check` / `ruff format --check` (real lint regression — fixed)

- **Evidence.** `ruff check .` on `5b0d334` reported **5 errors** (`I001` ×3, `F401` ×2) and `ruff format --check` reported **5 files**, all introduced by the Phase 27 harness scripts committed in `d20bb70` (`mo7_p27_attack.py`, `mo7_p27_probe.py`, `mo7_p29_probe.py`) — the Phase 27 lint run had finished before those files landed.
- **Classification:** `HARNESS DEFECT` (Phase 27 tooling, not product code).
- **Action:** import-sorted/format-cleaned only those three files. Re-verified: `ruff check` → *All checks passed*; `ruff format --check` → back to the documented two-file delta (§8.1). All three harnesses still run correctly (§7.6).

---

## 4. Optimizations

**No production optimisation was made in Phase 28, and none was warranted.** Measurement came first everywhere; the cold-start cost is the framework's lazy route materialisation (§3.1), which is a deliberate design rather than an accidental regression, and the warm path is already at 2–4 ms. Changing it would trade cold-start behaviour for first-request behaviour without improving the steady state, and would require a routing/DI architecture change outside this phase's scope. The frontend lever (reading session) is documented in §3.3 with evidence that the remaining bytes are sanctioned features, not waste.

Changes actually made in this phase, with expected vs measured impact:

| Change | File(s) | Expected impact | Measured impact | Regression evidence |
|---|---|---|---|---|
| Explicit 15 s window on the two EPUB spine-switch assertions | `web/tests/epub-reader.audit.ts` | Removes a load-order-dependent red without weakening the assertion | Matrix 63/63 green; isolated run 2/2 green | §7.5 |
| Import sorting / unused-import removal / formatting of the three Phase 27 harness scripts | `docs-for-user/phase27-harnesses/*.py` | `ruff check` clean, format delta back to the known two files | Exactly that (§7.2); harness behaviour unchanged (56/56, 2/2, 15/15 in §7.6) | §7.2, §7.6 |

No product behaviour, API contract, dependency-injection path, auth/authz decision, i18n string, or storage behaviour was touched in this phase.

---

## 5. Stability

Harness: `mo7_p28_stability.py` (phases A–H), full run **EXIT=0**; results in `/home/user/mo7_p28_stability_results.json`, log `/tmp/mo7_p28_stability.log`, server log `/tmp/mo7_p28_stability_server.log`. An earlier run exited 1 in phase G because the harness passed a `Popen` object to `wait_health`; that was a harness defect, fixed, and is not crash-recovery evidence.

- **A — cold start + endpoint probe:** cold start 3 025.9 ms, all **10** probed endpoints 200.
- **B — warm latency** (p50 / p95 / p99 ms, 0 errors): `/api/settings` 12.79/23.57/25.67; `/api/multi-user/admin/resources` 7.64/8.48/10.16; `/api/sessions` 4.83/6.69/7.96; reading endpoints 2.65–4.02.
- **C — concurrency** (240 requests per level): 1 worker **123.8 rps** (p50 5.4 / p95 13.4 / p99 18.4 ms), 4 workers **144.5 rps** (25.6 / 35.6 / 41.7), 8 workers **133.8 rps** (45.2 / 108.1 / 215.2); **0 errors, 0 timeouts**; RSS delta 24 / 532 / 1 856 KiB — saturation shows as latency growth, not failure, on 2 vCPU.
- **D — malformed input:** 80 × 422 + 40 × 404, server alive, **zero 5xx**.
- **E — soak:** 240.3 s, **42 248 requests**, 175.8 rps sustained, 0 request errors, 0 health-check failures, RSS 161 448 → 162 040 KiB (flat; **no monotonic growth**).
- **F — unavailable dependency:** provider pointing at a dead port returns 200 `{"status":"unreachable","models":[]}` in 9.9 ms — graceful degradation, not an error storm.
- **G — SIGKILL recovery:** killed with −9, cold restart in **2 984.0 ms**; sessions 0 → 0; all **7 SQLite databases `ok`**; killed pid confirmed dead with no orphan; no corruption.
- **H — repeated start/stop:** three cycles 2 972.0 / 2 882.8 / 3 056.8 ms cold; RSS 137 416 / 137 468 / 137 376 KiB (no growth between cycles); clean shutdown 816.9 / 766.4 / 816.8 ms.
- **Memory conclusion:** no reproducible leak. Allocator fluctuation between runs (±1 %) is not growth; the 500-request smoke, the 42 k-request soak and the repeated lifecycle cycles are all flat. No leaked workers or file descriptors were observed (6 threads / 16 fds stable in the smoke test).

**Failure/recovery evidence reused from Phase 26/27:** bare restart and SIGKILL-under-load reproductions (`/tmp/mo7_p28_recovery.log`, `/tmp/mo7_p28_recovery2.log`) both clean, with throwaway homes to rule out stale `data/` state. The earlier belief that restarts were unclean was a harness bug (§5 preamble); it is not cited as evidence anywhere.

---

## 6. Frontend

- **Budgets** (`npm run perf:check`, production artifact, framework and root shell excluded): 8/9 OK, reading-session the single FAIL at 1 128 / 1 120 KB. `/chat/[sessionId]` 969/1 020; `/settings` 113/840; `/knowledge-bases` 314/550; `/co-writer` 255/320; `/co-writer/[docId]` 328/515; mastery 898/980; root-app-shell 272/390; `/` verifies its redirect.
- **Harness trustworthiness:** its own test file passes 4/4; it never follows redirects into a different route; it launches and stops its own server; the numbers are identical across two independently built artifacts (§3.2), so no stale-build artefact can produce the reported sizes.
- **Reading-session detail:** 44 chunks / 1 400 KB; 7 reading-only chunks = 272 KB, of which ≈200 KB is epub.js (158 KB) + KaTeX/review UI (41 KB) for the sanctioned EPUB reader and 72 KB is the reading companion's shared chat stack. Full evidence chain in §3.3.
- **Loading strategy already sane:** epub.js, KaTeX, remark, mermaid, marked and pdf.js all reach the reading route through `import()` / `next/dynamic`; only `lib/pdfjs-loader.ts` is statically imported, and even that resolves pdf.js via a dynamic `import()`.
- **Bundle reproducibility:** two builds, identical 355-chunk set (paths + sizes), `✓ Compiled successfully` at 28.5 s and 18.9 s, EXIT=0 both times, no build warnings other than the known standalone-output notice.
- **Conclusion:** budget measured correctly; the route is in budget on every route except the one documented in §3.3, which needs broader out-of-scope code-splitting work.

---

## 7. Full Regression

All runs on the final tree (no production files changed, so the Python results also certify `5b0d334`).

| Gate | Result | Evidence |
|---|---|---|
| Python full suite | **8 434 passed, 54 skipped, EXIT=0** (550.8 s) | `/tmp/mo7_p28_pytest.log` |
| Python lint | `ruff check` → *All checks passed* | `/tmp/mo7_p28_pylint.log` |
| Python format | 2 files would be reformatted — the pre-existing, documented delta (`codex_auth/oauth.py`, `tools/tex_downloader.py`); repo-wide `ruff format` forbidden | `/tmp/mo7_p28_pylint.log`, §8.1 |
| Architecture | import-linter 3 contracts kept / **0 broken**; `compileall` EXIT=0 | `/tmp/mo7_p28_pylint.log` |
| Data / storage | `tests/services/storage`, `test_runtime_storage_guard.py`, `test_file_library_router.py` → **49 passed** (the dedicated Phase 26 harness scripts no longer exist in the workspace; storage coverage is the in-repo suites) | run output |
| Frontend checks (`check:fast`) | **EXIT=0**: contracts, architecture (dependency-cruiser 880 modules / 2 617 deps), typecheck, ESLint, i18n parity + audit clean; Node **1 224 pass / 0 fail**; Vitest **108 files / 427 tests passed** | `/tmp/mo7_p28_checkfast.log`, `/tmp/mo7_p28_checkfast2.log` |
| Route budgets | 8/9 OK + 2 harness tests OK + 4/4 harness self-tests | `/tmp/mo7_p28_perfcheck2.log` |
| Runtime | health + representative APIs on :8001 (10 endpoints 200); warm percentiles in §5 | `mo7_p28_stability_results.json` |
| Browser (Chromium matrix) | **63 passed, EXIT=0** (2.6 min) — `--project=ui-audit --project=epub-reader-chromium`, backend :8001 and web :3000 up | `/tmp/mo7_p28_playwright2.log` |
| Security (Phase 27 matrix) | `mo7_p27_attack.py` **56 passed / 0 failed** | `/tmp/mo7_p28_p27_attack.log` |
| Security (Phase 29 probe) | `mo7_p29_probe.py` **15 passed / 0 failed**; `mo7_p27_probe.py` 2 passed / 0 failed (raw-header cookies, CORS, revoked and demoted tokens) | run output |
| Production build | **EXIT=0**, `✓ Compiled successfully`, chunk set byte-identical across two builds | `/tmp/mo7_p28_build.log`, `/tmp/mo7_p28_build2.log` |

---

## 8. Remaining Findings

### 8.1 `KNOWN FINDING`

1. **Reading-session bundle 1 128 KB vs 1 120 KB budget** — 8 KB / 0.7 % over; evidence and out-of-scope rationale in §3.3. Budget intentionally unchanged. First recorded in Phase 29 report §4.1.
2. **`ruff format --check` two-file delta** — `deeptutor/services/codex_auth/oauth.py` and `deeptutor/tools/tex_downloader.py` are not formatter-clean; pre-existing, unrelated to this phase, and a repo-wide `ruff format` is explicitly forbidden. Formatting-only, no behaviour impact. Identical to Phase 29 report §4.2.
3. **`next/og` advisory (Next 16.3.3)** — carried over from Phase 27; the codebase does not use `next/og` for privileged content, and a framework upgrade is out of scope.

### 8.2 `OUT OF SCOPE`

1. **First-request route materialisation (≈884 ms, one-time)** — FastAPI lazy route/DI construction: 620 routes built on the first request, costing ≈0.9 s once and 4.2 ms thereafter. Removing it means eager route construction (which lengthens cold start) or a different routing layer. Documented, not changed (§3.1).
2. **Reading companion code-splitting** — deferring the visible companion panel would shed up to ≈70 KB but changes first-paint UX on a sanctioned feature; a broader code-splitting architecture change (§3.3).
3. **Import composition** — 1.7–1.8 s of ordinary dependency loading across 494 product + 860 dependency modules; the heaviest product chains (`model_selection.tasks`, `llm.config`) are product architecture, not accidental imports. No dead or duplicated heavyweight import was found.

### 8.3 `VALIDATION BLOCKED — INFRASTRUCTURE`

Genuinely external, evidenced blockers; no repository defect hides behind them, and no production code was modified to bypass them. Inherited from the Phase 27/29 environment table:

| Blocked validation | Reason |
|---|---|
| Redis-backed paths (`DEEPTUTOR_TEST_REDIS_URL`, distributed coordination, some worker paths) | No Redis service and no route to install one in the sandbox |
| Live LLM provider calls (chat, tutor, model discovery) | No reachable LLM provider/credentials; provider-unreachable behaviour was validated instead (phase F) |
| Docker/container builds | Docker daemon unavailable |
| WebKit/Firefox browser projects | Playwright's browser CDN is TLS-blocked; only Chromium 143 (plus headless shell) is available, so browser coverage is Chromium-only — no WebKit claim is made |
| Google Fonts during build | `fonts.googleapis.com` TLS-blocked; builds ran with the offline font mock (`NEXT_FONT_GOOGLE_MOCKED_RESPONSES`) — build correctness verified, real font fetch not |
| Coverage instrumented runs | Not required by this phase's gates |

---

## 9. Definition of Done

| Criterion | Status |
|---|---|
| Baseline reproduced or differences explained | ✅ Historic vs current reproduced; ≈600 ms cold-start delta explained by A/B against `37f4f4f` (§1) |
| Cold start understood | ✅ 1.7–1.8 s import + one-time ≈0.9 s lazy route materialisation (§3.1) |
| Major bottlenecks addressed or proven safe to defer | ✅ Deferred with evidence and rationale (§3.1, §4) |
| Warm runtime stable | ✅ 2–4 ms p50 on health routes; multi-route matrix 0 errors (§3, §5) |
| Route budgets correctly measured | ✅ 4/4 harness tests; redirects not remapping measurements; identical across rebuilds (§3.2) |
| Reading-session bundle in budget or proven to need broader work | ✅ Proven broader (§3.3) |
| Concurrency stable | ✅ 123.8–144.5 rps, 0 errors up to 8 workers (§5) |
| No reproducible memory leak | ✅ 42 k-request soak and repeated lifecycles flat (§5) |
| Soak passes | ✅ 240 s / 42 248 requests / 0 errors (§5) |
| Failure/recovery validated | ✅ SIGKILL recovery in 2 984 ms, 7 DBs `ok`, no orphans (§5) |
| Chromium E2E green | ✅ 63/63, EXIT=0 (§7) |
| Production build passes | ✅ Two builds EXIT=0, reproducible chunk set (§6) |
| Full regression passes | ✅ §7 |
| Harness trustworthy | ✅ §3.2, §3.5, §7 |
| No actionable defect | ✅ Every finding is a known finding, out of scope, or infrastructure-blocked (§8) |
| Remaining blockers genuinely infrastructure-only | ✅ §8.3 |
| Repository clean, committed and pushed | ✅ §10 |

`PHASE 28 READY`.

---

## 10. Repository State

Phase 28 changed exactly three repository artifacts, none of them production code:

| File | Change |
|---|---|
| `web/tests/epub-reader.audit.ts` | Explicit 15 s window on the two EPUB spine-switch assertions (§3.4) |
| `docs-for-user/phase27-harnesses/{mo7_p27_attack,mo7_p27_probe,mo7_p29_probe}.py` | Import sorting, unused imports, formatting (§3.5) |
| `docs-for-user/PHASE_28_PERFORMANCE_STABILITY_CLOSURE_REPORT.md` | This report |

`web/scripts/route_budgets.mjs` was inspected, executed and found correct — it was not modified. No budget value was changed. No production file, dependency, migration, or configuration was touched.
