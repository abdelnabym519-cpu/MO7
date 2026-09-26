# MO7 Foundation Baseline

> **Status: FROZEN** — established at Phase 21 (Foundation Freeze).
> This document is the authoritative record of the MO7 Foundation. All future
> product work MUST preserve it. See the Freeze Rules at the end.

## Identification

| Item | Value |
|---|---|
| Product | MO7 |
| Foundation source | [HKUDS/DeepTutor](https://github.com/HKUDS/DeepTutor) `v1.6.11` |
| Foundation commit | `a053fecf` (`release: v1.6.11`) |
| Freeze tag | `mo7-foundation-v1.0` |
| Freeze date | 2026-09-26 |
| Product repository | [abdelnabym519-cpu/MO7](https://github.com/abdelnabym519-cpu/MO7) |

## Foundation Contents

DeepTutor v1.6.11 **plus only**:

1. **Phase 19 — Test/dependency compatibility fixes (test-only):**
   - `tests/api/test_file_preview.py` — FastAPI ≥ 0.141 lazy `_IncludedRouter`: use
     `tests/api/route_introspection.py:iter_effective_route_paths(app)` instead of
     scanning flattened `app.routes`.
   - `tests/api/test_knowledge_router.py` — same root cause: local
     `_iter_matchable_routes()` helper for route-template matching.
   - `tests/cli/test_doctor_cli.py` — environment-proof secret sentinels
     (`user-secret`/`password-secret`) in the redaction test.
   - `web/tests/internal-route-contract.test.ts` — Windows path-separator
     portability (`[/\\]page\...`).
2. **Phase 20 — Voice upload hardening (one line, semantics-preserving):**
   - `deeptutor/api/routers/voice.py` — `/stt` bounded read:
     `await file.read(_MAX_AUDIO_BYTES + 1)` (was unbounded; closes an
     in-memory buffering DoS window before the 25 MB cap). Contract unchanged
     (400 empty / 413 oversize / provider dispatch), matches `practice.py` pattern.
   - `tests/api/test_voice_routes.py` — regression test: 413 with bounded read.
3. **Phase 20 — Dependency security fix (critical release blocker):**
   - `next` `^16.2.3` → `^16.3.3` (lockfile 16.2.3 → **16.3.3**; family-only
     updates: `@next/env`, `@next/swc-*`, `@swc/helpers`,
     `@next/eslint-plugin-next`, nested `postcss`, `sharp` 0.34.5 → 0.35.4;
     `react`/`react-dom` unchanged). Exits the critical advisory range
     `9.3.4-canary.0 – 16.3.2` (middleware/proxy bypass, SSRF, CSP-nonce XSS,
     RSC cache poisoning, DoS). Also clears sharp/libvips highs and nested
     postcss advisories.

Everything else — authentication, RBAC, persistence (SQLite/WAL/PocketBase
scoping), workspace isolation, sandbox architecture, API architecture, i18n,
PDF generation, frontend, CI/CD — is DeepTutor v1.6.11 verbatim.

## Runtime Expectations

| Tool | Expected |
|---|---|
| Python | ≥ 3.11 (CI matrix 3.11–3.14) |
| Node | 22 (CI), compatible with 24 |
| FastAPI | `>=0.100.0` (validated on 0.141.x — lazy router includes) |
| npm | 10/11 with `--legacy-peer-deps` |

## Test Baseline (freeze validation, 2026-09-26, Linux sandbox, node 22 / python 3.11)

| Gate | Command | Result |
|---|---|---|
| Python | `pytest -q tests deeptutor/learning/tests` | 8417 passed, 54 skipped, 0 failed |
| Node | `npm run test:node` (in `web/`) | 1220/1220 |
| Vitest | `npm run test:unit` | 426/426 (107 files) |
| TypeScript | `npm run typecheck` | PASS |
| Ruff (CI 0.16.0) | `ruff check .` / `ruff format --check .` | PASS |
| import-linter + `scripts/check_architecture.py` | — | 3/3 contracts kept; OK |
| ESLint | `npm run lint` | 0 errors (49 pre-existing warnings) |
| i18n | `npm run i18n:check` | PASS |
| Contracts/arch | `npm run contracts:check`, `architecture:check` | PASS |
| Runtime smoke | production-like probes (see below) | 15/15 PASS |

Smoke coverage: app start, `/api/auth/status`, `/health/live`, login 401 on bad
credentials (fully-enabled auth), 401 on unauthenticated data/file-preview routes,
400 external-URL preview rejection, 404 missing preview source, voice 400/413
(bounded read)/provider dispatch, SQLite write→restart→read integrity + `0600`
perms, settings bootstrap read, KB list read.

## Build Status

- `npm run build`: **PASS known-good** on Windows/CI (production build incl.
  `/chat` route verified at Phase 19). Sandbox builds are environment-blocked:
  `next/font` fetches Google Fonts (`fonts.googleapis.com` unreachable → HTTP 000).
  Not an application defect. Re-confirm on Windows/CI after every dependency change.
- `npm run perf:check`: part of the same authoritative run.

## E2E Status

- Command (repo-defined, do not invent): `npm run audit`
  (`playwright test --project=ui-audit`, requires a running production server,
  Chromium). Configuration verified against `.github/workflows/tests.yml`.
- Sandbox execution blocked: Playwright CDN downloads unreachable (TLS resets on
  `cdn.playwright.dev`, `prss.microsoft.com`, and mirror). Not an application
  defect. **Authoritative E2E = CI or Windows.**

## Security Baseline

- Dependency audit (`npm audit --omit=dev`): **6 vulnerabilities (5 moderate, 1
  high), 0 critical** at freeze. Next.js critical: **resolved (16.3.3)**.
  Remaining: `brace-expansion` (high; build/tooling-chain regex-DoS), `dompurify`,
  `mermaid`, `uuid`, `exceljs` (moderate). Documented; no `npm audit fix`
  without an explicit upgrade plan.
- Verified PASS: auth/login (bcrypt/JWT HS256, HttpOnly+Secure+SameSite cookies),
  RBAC, WS auth, CORS dual-mode, route-level auth (401 verified), upload surfaces
  (bounded reads, size caps, magic-byte sniffing, suffix allowlists, archive
  entry/ratio/traversal guards), path-traversal guards, workspace/user/session
  isolation, secrets redaction (doctor), sandbox runner architecture, Docker
  config, CI architecture.
- Known production-hardening warnings (deliberately deferred to the Security /
  Production phases; expected to be handled by reverse proxy and policy work):
  no global API rate limiter, no global security-header middleware, no
  TrustedHost/HTTPSRedirect middleware, mutable `latest` Docker tag, CI actions
  not SHA-pinned, full backup/DR strategy not yet defined, Redis-dependent tests
  exercised only in CI (service absent locally).

## Known Warnings (non-blocking)

- Windows pytest temp `PermissionError` → use project-local `.pytest-tmp`
  (`TMPDIR/TEMP/TMP`) workaround.
- Frontend unit tests may show worker startup timeouts under constrained Windows
  environments; green on CI (Linux).
- Redis service required locally only for `redis_integration`-marked tests;
  they skip cleanly otherwise and run in CI.
- Sandbox/CI matrix: Playwright and Google Fonts egress required for build/E2E
  respectively.

## Known Deferred Work

1. Remaining 6 non-critical npm vulnerabilities (tracked for the dependency phase).
2. Reverse-proxy-level hardening (rate limits, security headers, TrustedHost,
   HTTPS) — deployment/phase decision, not Foundation defect.
3. Backup/restore/DR strategy — Production Readiness phase.
4. Docker production policy (pinned tags, egress, resource limits) — Production
   Readiness phase.

## Freeze Rules (binding after Phase 21)

1. No feature development may modify Foundation behavior without explicit scope.
2. Productization changes must preserve existing behavior.
3. Security fixes are allowed when required.
4. Critical bug fixes are allowed.
5. Dependency security fixes are allowed when required.
6. Regression tests are allowed/required.
7. Any architectural change requires explicit justification.
8. Any breaking change requires explicit approval.
9. Existing tests must remain green.
10. New product features belong to later product-development phases.
