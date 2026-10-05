# Phase 27 — Security Hardening: End-to-End Closure

**Branch:** `arena/01a0ff3f-mo7` · **Base commit:** `31376d0` (Phase 26 closure)
**Safety branch:** `mo7-phase27-security-closure-safety` @ `31376d0`
**Environment:** local sandbox, no network to Google Fonts / Playwright CDN / Redis / LLM
providers / Docker (see §9) — every workaround below is an environment substitute, not a
product change.

## 1. Verdict

Phase 27 took the security surface from "audited in earlier phases" to "re-validated live
against a real server", found and fixed **two real defects** (token revocation, 500-body
disclosure) plus **four hardening gaps** (unexpiring tokens, failed-login throttle, Mermaid
DOM-injection advisory, missing document headers), repaired a set of **harness defects** that
had been producing false results, and re-established the Phase 26/29 evidence that did not
survive the sandbox re-clone.

Everything that ran is green: live attack harness **56/56**, raw-header probe **2/2**,
re-created Phase 29 probe **15/15**, Playwright **63/63**, frontend `check:fast` **exit 0**,
production build **exit 0**, full Python suite **8434 passed / 0 failed** (§8). The historical
`VALIDATION BLOCKED — INFRASTRUCTURE` label (Playwright CDN, Google Fonts) **no longer
applies** — both were worked around with bundled/offline substitutes and the browser suite
ran to completion. Remaining blockers are enumerated in §9; none is security-relevant.

**Final status: `PHASE 27 READY`.**

## 2. Method and guardrails

* Safety checkpoint first: root/remote/branch/HEAD/clean tree verified; the stale local
  checkout was proved byte-identical to `origin/arena/01a0ff3f-mo7` before `reset --hard`,
  and `mo7-phase27-security-closure-safety` was created at `31376d0`. No destructive cleanup,
  no user work deleted.
* Nothing was skipped to reach green. Every failing check was reproduced, inspected
  request-by-request, classified (application defect vs harness defect), fixed at the right
  layer, and re-run.
* No authentication was disabled, no assertion weakened, no 200 accepted where 401/403 is
  required, and no runtime validation replaced with mocks. Where a fixture legitimately
  needed an identity, the fixture was given a real account record.
* Production code changed only for confirmed defects and minimal hardening; no auth/RBAC
  redesign, no provider/framework switch, no unrelated refactor, no Phase 28/30 work.

## 3. Security inventory (surface → what protects it → evidence)

| Surface | Protection in the code | Evidence from this phase |
|---|---|---|
| Authentication | JWT HS256, `algorithms=[_ALGORITHM]` allowlist (no `alg=none`, no confusion), `require_exp`, bcrypt hashes | harness: garbage/empty/header-only/wrong-key/`alg=none`/malformed/expired/**unexpiring** tokens all 401; wrong password 401; unknown user 401; malformed body 422 |
| Account lifecycle | stored record is authoritative for identity, role and `disabled` (`services/auth.py::decode_token`) | harness: delete → token 401; demote → admin route 403 while own settings stay 200; token for a never-created account 401 |
| Password guessing | per-account+caller failure counter, 10 / 5 min, 429 + `Retry-After`, cleared on success | harness: `[401×10, 429]`, `retry_after='300'`; probe: throttled request never reaches bcrypt |
| Device pairing | code expiry, `MAX_PIN_ATTEMPTS`, `PIN_LOCKOUT_SECONDS`, daily-minute cap (pre-existing, `multi_user/device_credentials.py`) | code inspection; covered by `tests/multi_user/test_device_credentials.py` (part of the green suite) |
| Sessions / cookies | `dt_token` HttpOnly, `SameSite=lax`, `Path=/`; logout `Max-Age=0`; `no-store` on login | raw `http.client` headers from the probe and the harness |
| RBAC | `require_auth` / `require_admin` FastAPI dependencies on every data router; server-side only | harness: anonymous refused 17/17 protected routes; non-admin 403 on admin routes; admin-only endpoints admin-only |
| Tenant isolation | per-account roots + auth-scoped stores | harness: cross-tenant matrix **21/21 refused**, zero marker leakage, listings isolated, storage roots disjoint, non-owner delete leaves owner data intact, **every tenant SQLite passes `integrity_check`** |
| Object-level authz (IDOR/BOLA) | every resource resolved through the caller's scoped store | harness: material/units/raw/position/bookmark/delete, collection get/patch/delete, session get/rename/delete, library meta/download/delete/restore, attachment and generated-output paths — all refused for the wrong account, with owner-visible state verified unchanged |
| Path & file access | filename handling + scoped stores | harness: `../`, `%2f`, `%5c`, `....//`, absolute and mixed-separator attempts on outputs/workspace-items/assets/media/attachments/library all denied |
| Archive extraction | Phase 27 containment hardening (safe extractor) | harness zip-slip upload writes nothing outside the extraction root and does not 5xx; `tests/utils/test_archive_extractor.py` + `tests/api/test_knowledge_zip_upload.py` green |
| Injection | parameterised SQL throughout; the only dynamic SQL uses constant fragments with `# nosec B608`; no `pickle`/`marshal`/`eval`/`yaml.load` (17 `yaml.safe_load`) | repo-wide grep + bandit (0 issues on changed files) |
| Command execution | only the sandbox runner executes commands; `shell=True` is its documented contract for the shell-string form, argv requests are `exec`'d directly, workdir is validated, isolation is the container | code inspection of `services/sandbox/runner/server.py`; runner is opt-in via `DEEPTUTOR_SANDBOX_RUNNER_URL` |
| XSS | uploads sniffed by magic bytes (SVG refused for avatars); user bytes served with `nosniff` + `sandbox` CSP; visualizer HTML rendered in a sandboxed iframe | harness + `web/lib/iframe-html.ts`; Mermaid advisory fixed (§5 F5) |
| SSRF | `web_fetch` disallowed-host resolution (all DNS answers checked, mixed answers blocked) | `tests/tools/test_web_fetch.py` green |
| Secrets / config | auth secret generated on disk (64-hex); no secret material in responses; `.env` never served | harness: public settings expose no credential-shaped fields; 500 bodies leak no paths/traces/secrets; detect-secrets on changed files (§9) |
| Error responses | sanitized 500 body (type only), generic 4xx details | harness: `{'detail': 'The request could not be completed.', 'type': 'DatabaseError'}`; no `/home/user`, `Traceback`, `sqlite3` or secret strings |
| CORS | explicit origin list when auth is on; credentials allowed only for listed origins | harness + probe: `http://localhost:3000` → 200 with ACAO+ACAC; `https://evil.example` → 400, no ACAO; simple GET with evil Origin → no ACAO |
| Headers / framing | `nosniff`, `Referrer-Policy`, `X-Frame-Options: SAMEORIGIN`, `frame-ancestors 'self'` on app documents; `/api/**` left to the backend | live header capture on the production build (new in this phase, §5 F6) |
| Cache controls | `no-store` on auth responses; `private` on user file downloads | harness: login `no-store`; download `private, max-age=31536000, immutable` |
| Logging / audit | admin actions logged (`log_admin_action`), access log, diagnostics stay in logs | harness checks the client bodies only; server logs retain full detail |
| Resource exhaustion | upload ceilings enforced per chunk (avatar 1 MB, reading 200 MB, docx 20 MB, partners 20/25 MB, import 2 MB); LLM traffic controller concurrency/RPM; handoff rate-limit table | harness: 1 MB + 1 avatar upload → **413** in <0.1 s; unit test pins the 200 MB reading ceiling; failed-login throttle |
| Dependencies | pinned lockfile; one targeted security patch applied | §5 F5, §9 |

## 4. Threat model (classes that apply, and what refuses them)

* **Forged / modified / expired / wrong-key / `alg=none` / algorithm-confusion / unexpiring
  tokens** — all refused (401); the decode path fixes the algorithm and requires `exp`.
* **Horizontal escalation / IDOR / BOLA** — refused on 21 resource routes, with the owner's
  state re-verified afterwards; alternate verbs (PUT/PATCH/DELETE/POST) exercised, not just GET.
* **Vertical escalation / role bypass / bootstrap abuse** — non-admin 403 on admin routes;
  first registration becomes admin, second registration 403; claim tampering 401; a token
  minted while admin loses the route on demotion.
* **Cross-tenant read/write/delete, direct storage access, stale metadata, deleted-object
  resurrection** — per-account roots and scoped stores; library restore/delete on another
  account's file refused; no shared content directories on disk.
* **Traversal / archive escape** — URL-encoded, backslash, doubled and absolute variants all
  denied; zip-slip writes nothing outside the root.
* **Injection / XSS / SSRF / command execution / filesystem escape** — targeted tests at the
  real boundaries (parameterised SQL, sandboxed iframes, `web_fetch` host validation, sandbox
  runner contract), not blind fuzzing.
* **Secret and sensitive-error exposure** — sanitized bodies; diagnostics in logs only.
* **Resource exhaustion** — ceilings verified live; the only new limiter (login throttle) was
  added against measured evidence (below), smallest form that closes the risk.
* **Not applicable, not tested:** none claimed. Classes that do not exist in this codebase
  (unsafe deserialization, template injection, open redirect) were checked by inspection:
  no `pickle`/`eval`, no template engine, and the `next` return path is normalised by
  `normalizeInternalReturnPath` (origin-pinned, `//`, backslash and control-char rejected,
  decode-then-recheck) with existing unit coverage `web/tests/auth-return-url.test.ts`.

### Evidence for the throttle (why a limiter, why this size)

Eight consecutive wrong passwords for a real account returned `401` each time at
**273–292 ms** per attempt (bcrypt is the only friction) with no counter and no lockout; the
device-pairing path next to it already locks after `MAX_PIN_ATTEMPTS`. A per-(account, caller)
counter of 10 failures / 5 min, cleared by a successful sign-in, closes the guessing window
without penalising a user who mistypes a password, and does not touch any other endpoint.

## 5. Findings

| # | Finding | Severity | Exploitability | Class | Action |
|---|---|---|---|---|---|
| F1 | A deleted account's token still authenticated (`/api/settings` 200) and a demoted admin's old token still passed `/api/multi-user/users` (200) — the token claim outlived the account for up to the 12 h expiry; a token for a never-created account was similarly accepted if signed | **High** | Requires a token issued before the delete/demote (any ex-user, revoked admin, or a leaked token) | **Application defect** | **Fixed** — `decode_token` resolves the stored record and treats it as authoritative for existence, `disabled`, id and role; signature still decides issuance |
| F2 | Unhandled exceptions returned `{"detail": "DatabaseError: file is not a database", "type": "DatabaseError"}` — class + message (DB errors, absolute paths, occasionally credential values) in a body any caller can read | **Medium** | Any request that triggers an unhandled error, including unauthenticated public routes | **Application defect** | **Fixed** — body is now a stable `{"detail": "The request could not be completed.", "type": <class>}`; full detail still logged server-side |
| F3 | No failed-login throttling on the password path (evidence above) | Medium | Online guessing + CPU burn at ~270 ms/attempt, unbounded | Hardening gap | **Fixed** — SQLite-backed counter in the auth state dir, 10 / 5 min per account+caller, `429` + `Retry-After`, cleared on success; fails open with a log line if the counter file is unavailable (secondary control behind bcrypt) |
| F4 | A correctly-signed token with **no `exp`** was accepted — it would never expire | Medium | Anyone who can mint with the deployment secret (or a mis-issued token) gets an immortal credential | Hardening gap | **Fixed** — `options={"require_exp": True}`; every minter in the repo sets `exp` |
| F5 | `mermaid@11.14.0` is affected by CVE-2026-41149 / GHSA-ghcm-xqfw-q4vr (DOM injection escaping the SVG through `classDef` in state diagrams); MO7 renders model-authored Mermaid into the app origin via `dangerouslySetInnerHTML` | Medium | Content-driven (a document/prompt that makes the model emit a crafted diagram, viewed by another user) | Dependency defect, applicable | **Fixed** — `mermaid` bumped to `^11.15.0` (single dependency, inside the declared range; lockfile churn confined to Mermaid's own parser subtree) |
| F6 | Next document responses carried no `X-Content-Type-Options`, `Referrer-Policy` or framing protection; only the backend's file routes set `nosniff` | Low–Medium | Clickjacking / UI redressing of a logged-in reader; MIME-sniffing of proxied content on the app origin | Hardening gap | **Fixed** — `next.config.js` `headers()` scoped to non-`/api` paths; **not** a blanket CSP (inline theme script/blob media would need nonces — a product change) |
| F7 | `npm audit` (prod): Next.js **critical** `GHSA-vcvr-r3jv-pc5j` (RCE in `next/og` `ImageResponse`) on installed `16.3.3` (`<16.3.6`); plus `brace-expansion` (high, via minimatch→archiver→exceljs), `fflate`, `dompurify`, `uuid` (moderate) | Framework CVEs | **Not reachable in this app**: no `ImageResponse`/`next/og` usage anywhere (no `opengraph-image*`/`icon*` routes); `fflate` is jspdf's deflate helper, `uuid` is called as v4 without a buffer, the DOMPurify cross-realm variant needs foreign-realm objects while Mermaid passes strings, `brace-expansion` runs on app-supplied globs | **KNOWN FINDING** | Not fixed — a framework/transitive upgrade spree is explicitly out of Phase 27 scope. Recorded here with the reachability analysis so it is not mistaken for a hidden pass |
| F8 | `tests/services/test_codebuddy_credentials.py` used a hardcoded absolute expiry (`1791055241000` ≈ 2026-10-01) and had just gone red on the wall clock — unrelated to security, but it made the full suite non-green | Test-only | n/a | **Test defect** | **Fixed** — fixture derives expiry from `time.time()` and the assertion reads back the written value |

Also confirmed and deliberately unchanged: the sandbox runner's `shell=True` is its
documented contract (argv requests are `exec`'d directly, workdir validated, container
isolation); cross-tenant `PATCH /api/reading/workspaces/{id}` answers `400 "workspace …
not found"` — a denial whose message echoes an id the caller supplied, with the owner's
state verified unchanged.

## 6. Changes per file

| File | Change |
|---|---|
| `deeptutor/services/auth.py` | `decode_token`: record-authoritative identity/role/`disabled`/id, `require_exp`; new failed-login throttle (`LOGIN_ATTEMPT_LIMIT`, `check_login_throttle`, `record_failed_login`, `clear_login_throttle`, SQLite counter beside the auth state) |
| `deeptutor/api/routers/auth.py` | login: throttle check **before** bcrypt (429 + `Retry-After`), record failure, clear on success |
| `deeptutor/api/main.py` | `json_error_boundary`: sanitized 500 body (log keeps the diagnostics) |
| `web/next.config.js` | `headers()` for non-`/api` paths: `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`, `frame-ancestors` |
| `web/package.json`, `web/package-lock.json` | `mermaid` `^11.14.0` → `^11.15.0` (lockfile churn confined to the Mermaid subtree) |
| `tests/services/test_account_revocation.py` *(new)* | 8 cases: role resolution, demotion, deletion, `disabled`, never-created account, missing `exp`, foreign signature — all verified to fail without the fix |
| `tests/api/test_login_throttle.py` *(new)* | 4 cases: 10×401 then 429 + `Retry-After`, success clears the counter, per-bucket counting, a throttled request never calls `verify_password` |
| `tests/api/test_reading_upload_ceiling.py` *(new)* | pins the streaming 200 MB rejection and temp cleanup without shipping 200 MB |
| `tests/api/test_session_handoff.py`, `tests/api/test_partners_router.py` | fixtures that invented tokens for non-existent accounts now provision a real record (the store is authoritative) — assertions unchanged |
| `tests/api/test_main_exception_handler.py` | asserts the new sanitized contract and that class/message/paths no longer appear |
| `tests/services/test_codebuddy_credentials.py` | time-relative expiry (F8) |

## 7. Attack validation — what was run and what it proved

Two independent live paths were used (a urllib/CookieJar matrix and a raw `http.client`
probe), each against a real uvicorn process, real per-account roots and real SQLite stores:

| Harness | Result |
|---|---|
| `/home/user/mo7_p27_attack.py` (56 checks) | **56 / 56 passed, exit 0** — bootstrap; anonymous 17/17 refused; 8 token-shape attacks; RBAC; cross-tenant matrix 21/21 refused with zero leakage; owner-state-unchanged proofs; traversal (5 families); zip-slip; 500 sanitisation; public-settings leak check; CORS both directions; download cache-control; 413; revocation; demotion; throttle; **Phase 26 continuity** (listings isolated, 3 tenant DBs `integrity_check ok`) |
| `/home/user/mo7_p27_probe.py` (raw headers) | **2 / 2 assertions, exit 0** — plus wire-level evidence: `Cache-Control: no-store`, `dt_token` HttpOnly/`SameSite=lax`/`Path=/`, logout `Max-Age=0`, preflight `localhost:3000` → 200 ACAO+ACAC / unknown origin → 400, cross-tenant workspace PATCH denied with the owner's title intact, failed-login cost 273–292 ms |
| `/home/user/mo7_p29_probe.py` (Phase 29's 14 documented checks, re-created) | **15 / 15 passed, exit 0** — the original script was lost with the prior sandbox; this reproduces the list recorded in the Phase 29 report (§6) and adds the split token cases |

All three scripts are committed under `docs-for-user/phase27-harnesses/` (with a README) so
the evidence is reproducible from this report; Phase 26's ad-hoc scripts were not in the
repository and did not survive the sandbox re-clone, which is why the Phase 26 continuity
probes were folded into `mo7_p27_attack.py` (listings isolation + `integrity_check` on every
tenant database) and its committed regression tests.

**Harness defects found and fixed (not security results):** urllib folded `Set-Cookie`
(real headers now read over `http.client`); `code(body)` instead of `code(meta)` printed 0
for the revocation evidence; notebook/course payloads used `title` instead of the routers'
`name`; KB creation sent JSON to a `Form` endpoint; a material `PATCH` targeted a route that
does not exist (405) — replaced with the real position/bookmark mutations; cross-tenant
mutations now carry an owner-side shape check plus a before/after equality proof; the 60 MB
"oversize" upload was under a 200 MB ceiling (replaced by a 1 MB avatar overshoot → 413);
the harness needed the venv's `jose` and now writes `harness-results.json` and exits non-zero.

## 8. Regression matrix (real counts)

| Gate | Command | Result |
|---|---|---|
| Python suite | `PYTHONPATH=. DEEPTUTOR_TEST_REDIS_URL=redis://127.0.0.1:6379/0 pytest -q tests deeptutor/learning/tests` | **8434 passed, 0 failed, 54 skipped**, 515 s, **exit 0** (first pass: 8432 passed / 1 failed — the expired fixture F8, fixed and then green) |
| Phase 26 focus | `pytest -q tests/reading/test_router.py tests/reading/test_catalog_store.py tests/reading/test_ingestion.py tests/api/test_file_library_router.py tests/multi_user/test_identity_and_paths.py tests/multi_user/test_owner_path_service.py tests/multi_user/test_resource_isolation.py` | **129 passed** (includes the two Phase 26 regressions `test_deleting_one_copy_does_not_leave_its_id_readable`, `test_deleting_the_last_copy_removes_the_shared_directory`) |
| Archive containment | `pytest -q tests/utils/test_archive_extractor.py tests/api/test_knowledge_zip_upload.py tests/tools/test_tex_downloader.py tests/api/test_document_extractor.py` | **66 passed** (recorded at the start of the phase, unchanged) |
| Lint | `.venv/bin/ruff check deeptutor tests` | **All checks passed** |
| Format | `.venv/bin/ruff format --check .` | 2 pre-existing files only (`services/codex_auth/oauth.py`, `tools/tex_downloader.py`); every Phase 27 file formatted |
| Import boundaries | `.venv/bin/lint-imports` | **3 kept, 0 broken** (971 files) |
| Architecture | `.venv/bin/python scripts/check_architecture.py` | **OK** |
| Compile | `.venv/bin/python -m compileall -q deeptutor` | **exit 0** |
| Static scan | `bandit -c pyproject.toml` on the changed modules | **0 issues** |
| Secrets | `detect-secrets-hook --baseline .secrets.baseline` on changed files | 3 detections, **all pre-existing lines** (verified identical on the `HEAD` copies: `test_partners_router.py:202`, `test_session_handoff.py:177/235`); no new secret material |
| Frontend | `npm run check:fast` | **exit 0** — contracts, dependency-cruiser architecture, typecheck, node tests, Vitest, ESLint, i18n all green after the Mermaid bump |
| Browser | `WEB_BASE_URL=http://127.0.0.1:3000 npx playwright test --project=ui-audit --project=epub-reader-chromium` | **63 passed, 0 failed, 2.6 min** (44 release-matrix renders, reading history/W3C annotations/citation, settings, video learning, compliance/accessibility, EPUB ×2) |
| Production build | `NEXT_FONT_GOOGLE_MOCKED_RESPONSES=/tmp/mo7-font-mock.cjs npm run build` | **exit 0** (final build with the header rule; ~82 s) |
| Performance | `npm run perf:check` | 8/9 within budget; the one FAIL (`/learning/reading/…/sessions/…` **1128 KB / 1120 KB**) is byte-identical to the Phase 28 line recorded in the Phase 26 report — a Phase 28 workstream item, untouched |
| Runtime health | `curl` on the running production build | `/` 307 → `/chat`, `/chat` 200, headers present on documents, `/api/**` passthrough untouched |
| Security matrix | harness + probes above | **56/56, 2/2, 15/15** |

## 9. Infrastructure

**PASS (worked around, substitutes documented):**
* Chromium 143.0.7499.0 runs from the npm-bundled `@sparticuz/chromium` payload (Playwright CDN
  is TLS-blocked), assembled into the `ms-playwright/chromium-1200` layout with its own NSS and
  SwiftShader libraries.
* `next build` succeeds offline through `NEXT_FONT_GOOGLE_MOCKED_RESPONSES` (Google Fonts
  blocked); the mock only supplies font files, and no spec asserts on screenshots.

**KNOWN FINDING:** F7 — the dependency advisories in §5, with reachability analysis. The
Next.js critical is not reachable (`next/og` unused); the rest are transitive helpers called
without the vulnerable arguments. Remediation is a coordinated dependency upgrade, explicitly
outside this phase's "no broad dependency upgrades" rule.

**OUT OF SCOPE:** a nonce-based Content-Security-Policy for the app shell (product change);
the Phase 28 route-budget line (1128 KB vs 1120 KB); upgrades of every transitive advisory;
`critical-turns` Playwright project (self-skips without `DEEPTUTOR_TURN_E2E_FIXTURE=1` — an
in-repo design decision, not a browser blocker).

**VALIDATION BLOCKED — INFRASTRUCTURE:** the `epub-reader-webkit` Playwright project (no
WebKit build obtainable offline; the Apple/Playwright CDN route is TLS-blocked). Nothing else
is blocked: Redis, LLM providers and Docker remain unavailable in this sandbox, but no Phase 27
acceptance criterion depends on them — the security suite runs against real local stores.

**Historical label:** the earlier `VALIDATION BLOCKED — INFRASTRUCTURE` covered the Chromium
binary and the Google-Fonts-dependent build. Both are now substituted and the affected gates
ran to completion (63/63 browser tests, exit-0 build), so that label is **removed** for
everything except the WebKit project named above.

## 10. Definition-of-done check

* Security inventory and threat model reflect this codebase, with evidence per surface — §3/§4.
* Every applicable attack class was exercised live; no class skipped without classification — §7.
* Confirmed defects fixed (F1, F2); hardening gaps closed (F3–F6); no prod code touched for
  test convenience — §5/§6.
* Harnesses trustworthy: every prior failure was reproduced, classified and either fixed in the
  harness or fixed in production; both harnesses now exit non-zero on failure and record their
  results — §7.
* Regression, build, runtime and attack matrices pass with real counts — §8.
* No actionable Phase 27 security defect remains open; the only open items are the dependency
  advisories recorded as KNOWN FINDING and the out-of-scope items above — §9.
* Files reviewed, only Phase 27 changes committed, pushed to `origin/arena/01a0ff3f-mo7`,
  remote verified — §11.

## 11. Repository state

* Work branch: `arena/01a0ff3f-mo7`, Phase 27 commit **`d20bb70`** (pushed; `git ls-remote`
  returned the same hash as the local HEAD).
* Safety branch: `mo7-phase27-security-closure-safety` @ `31376d0` (untouched).
* `arena/01a0dba1-mo7` not touched; no Phase 28 branch created.
* Working tree clean after the Phase 27 commit; the Phase 26 report and artifacts are unchanged.

**Final status: `PHASE 27 READY` — hard stop, awaiting approval before any Phase 28/30 work.**
