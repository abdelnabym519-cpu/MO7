# Phase 26 — Production Data & Storage — End-to-End Closure Report

**Verdict: `PHASE 26 READY`** — the production data and storage foundation is
verified end to end against real servers and real data roots; the two repository
defects this phase found are fixed, covered by regression tests, and re-verified.
See §8 for what the verdict does and does not cover.

## 0. Branch

| | |
|---|---|
| Branch | `arena/01a0ff3f-mo7` |
| Start commit | `82eb018` — `docs: record phase 29 qa closure report` |
| Fix commit | `6393a74` — `fix(reading): keep deleted materials deleted when content is shared` (`deeptutor/reading/store.py`, `tests/reading/test_router.py`, +94/−4) |
| Report commit | the commit that adds this document (branch tip after `6393a74`) |
| Safety ref | `mo7-phase26-closure-safety` @ `82eb018`, created before the first edit; all work stayed on the session branch |
| Baseline branch | `arena/01a0dba1-mo7` = `37f4f4f` — untouched |
| Push target | `origin/arena/01a0ff3f-mo7` only |

## 1. Initial state

Phase 26 left **no artifact in the repository**: no Phase 26 document, branch,
commit message, or string match anywhere in the tree, and `git log --all` held
five commits. The phase therefore had to be judged from the code, the tests and
the runtime, not from historical notes — a note that a phase was never marked
`READY` is not evidence that it was incomplete.

Inventory reconstructed **before** any change (Implemented / Partially
implemented / Missing / Broken / Infrastructure-blocked / Out-of-scope):

| Area | State | Evidence |
|---|---|---|
| Session store (`data/user/chat_history.db`) | Implemented | WAL, `busy_timeout=30000`, `BEGIN IMMEDIATE`, PRAGMA `table_info` column migrations, notebooks/assessments; `tests/services/session/test_sqlite_store.py` (36 tests) |
| Legacy chat migration (`chat/sessions.json` → sqlite) | Implemented | archive-first, ledger, idempotent; `tests/services/session/test_legacy_migration.py` (4) |
| Reading store + catalog | Implemented | content-addressed dirs, staging/backup + atomic replace, `_catalog.sqlite3` with FK and indexes, workspace/session/annotation state |
| Workspace + per-user layout | Implemented | `services/path_service.py`, `multi_user/paths.py` (`data/user` vs `data/users/<uid>/user/…`), `tests/multi_user/` |
| Workspace / learning / partners migrations | Implemented | `services/workspace/{migration,data_migration}.py`, `learning/migration.py`, `partners/channel_state_migration.py` with dedicated tests (15 / 8 etc.) |
| File library object storage | Implemented | per-user root, sha256 dedupe, soft delete + restore, `tests/services/storage/test_file_library.py` (26) |
| Tenant isolation | Implemented | separate roots per account, auth dependency on every data router |
| **Deletion lifecycle for shared content** | **Broken** | deleted material id stayed readable and was re-registered by listings (F1, F2 below) |
| Performance / route budgets | Out of scope | Phase 28 workstream (reading-session bundle) |
| Auth / RBAC redesign | Out of scope | Phase 27 workstream |

No requirement was invented; nothing in the "Implemented" rows was rewritten.

## 2. Findings

Every finding below was reproduced against a real uvicorn server over a real
`DEEPTUTOR_HOME` before any code was touched.

### F1 — a deleted material whose content a sibling still reads came back (real defect, fixed)

*Reproduction* (`/tmp/mo7_p26_probe.py`, pre-fix):

1. upload `p26-a.md` → material `c310716e6edd5f2c` (content-addressed id);
2. upload the same bytes with `reuse=false` → sibling `rm_c593ca83c773` over the
   same content directory;
3. `DELETE /api/reading/materials/c310716e6edd5f2c` → `200 {"deleted": true}`;
4. `GET /api/reading/materials/c310716e6edd5f2c` → **`200`** (row gone, id still served);
5. `GET /api/reading/library/materials` → rows
   `['c310716e6edd5f2c', 'rm_c593ca83c773']` — the listing re-registered the
   deleted material from the surviving directory.

The same id also stayed readable through `/units/1`. Deleting the last
reference behaved correctly, so only shared content was affected.

*Root cause.* `ReadingStore._content_id()` falls back to the id itself when the
catalog has no row (the legacy content-addressed layout), and
`list_library_materials()` adopts every on-disk manifest it does not know
because a directory is normally a material that was never registered. Both
assumptions break when the directory belongs to a *living* sibling copy: the
id is a content id, not a material.

*Impact.* A deleted document reappeared in the reader library without
collections or annotations and could be re-opened by any client holding the old
id. Data stayed inside the account's own root, so this was a data-lifecycle
defect rather than a cross-tenant exposure.

*Action.* Fixed in `6393a74` (see §3) with regression coverage; re-verified by
harness A (checks 22–27) and the tenant probe.

### F2 — an `rm_` id whose row was gone answered 400 instead of 404 (real defect, fixed)

*Reproduction:* after deleting `rm_c593ca83c773`,
`GET /api/reading/materials/rm_c593ca83c773` → **`400` "invalid content id for
material …"**. `rm_` ids only resolve through their catalog row; without one,
`_content_id()` fell through to the hex content-id validation and surfaced a
malformed-id error for a well-formed, deleted id.

*Impact.* Wrong status semantics for the documented "404 for no such material"
contract (`deeptutor/api/routers/reading.py:_http_error`); clients could not
distinguish "deleted" from "bad request".

*Action.* Fixed in `6393a74`; a deleted `rm_` id now answers 404.

### F3 — an unreadable catalog degrades route by route (observation, no change)

With `_catalog.sqlite3` overwritten by garbage:

* `GET /api/reading/library/materials` → `500 {"detail": "DatabaseError: file is not a database"}` — loud, no fabricated rows;
* `GET /api/reading/materials` → `200` from the on-disk manifest walk (this route never consulted the catalog);
* material reads → `404` (missing manifests) rather than 500;
* restoring the catalog file restores every route (`200`).

This is honest degraded behaviour: no fake data and no silent empty success,
with a clear failure on the route that actually needs the catalog. Recorded as
a known finding (§7) rather than changed, because the store-backed listing
serves real bytes from disk and rewriting it was outside the minimal-fix rule.

### F4 — text materials keep extracted units, not a raw copy (by design, no change)

An uploaded Markdown/PDF-text material stores `manifest.json`, `outline.json`,
`unit_refs.json` and `units/0001.txt`; `/raw` exists only for
`render_mode != "text"` (PDF/EPUB/media). Verified as intended behaviour, not a
storage defect.

### Environment findings (not product defects)

* `tests/e2e/release-ui-matrix.audit.ts` uses `WEB_BASE_URL` or defaults to
  `http://127.0.0.1:3300`; without `WEB_BASE_URL` its 32 rows fail in ~60 ms
  with `net::ERR_CONNECTION_REFUSED` even though the app is healthy.
* Running a production build while a dev server serves the same `.next`
  directory breaks the running server (`ResponseAborted` on every API route);
  the live server must be restarted after a build.
* The Next server needs `DEEPTUTOR_API_BASE_URL` when the API is not on the
  port recorded in `data/user/settings/system.json` (`backend_port: 8001` here,
  smoke API on `8765`).

## 3. Changes

Two files, both committed in `6393a74`. Nothing else in the repository was
modified.

### `deeptutor/reading/store.py` (+54/−4)

**Reason.** F1/F2.

**Root cause.** A content directory shared by several materials was treated as
a material of its own whenever the catalog had no row for the id.

**Change.**

* `_content_owned_by_other(material_id)` — one indexed query answers both
  halves: a row for the id itself means it *is* a material; rows under other
  material ids with the same `content_id` mean the directory belongs to those
  siblings. A store without a catalog (the pure engine layout used by tests and
  legacy installs) keeps the previous behaviour.
* `manifest()` raises `MaterialNotFound` for such an id, so every read route
  (material, units, raw, render, annotations, position, export, transcript)
  answers 404 for a deleted material.
* `list_materials()` skips directories owned by another material, so neither
  `/api/reading/materials` nor the library listing can resurrect a deleted one.
* `_content_id()` raises `MaterialNotFound` for an `rm_` id with no catalog row
  instead of falling through to the hex validation (F2). `rm_` ids are minted
  by the catalog; ingestion paths only ever pass content-hash ids, so no write
  path changes behaviour.

**Validation.** `tests/reading/test_router.py` + `test_engine.py` +
`test_catalog_store.py` → 126 passed; full `tests/reading` + `tests/multi_user`
→ 681 passed; live harnesses A/B/C → 50/50, 40/40, 11/11; reverting only
`store.py` makes the two new tests fail (proving they pin the defect).

### `tests/reading/test_router.py` (+44)

**Reason.** No test expressed the deletion contract for shared content.

**Change.** Two regressions after `test_deleting_a_material_reports_where_it_was_used`:

* `test_deleting_one_copy_does_not_leave_its_id_readable` — delete one of two
  copies, then assert 404 on the detail and unit routes, absence from
  `/materials` and `/library/materials`, and that the sibling still reads;
* `test_deleting_the_last_copy_removes_the_shared_directory` — delete both
  copies, then assert the content directory is gone and both ids 404.

**Validation.** Both fail against the pre-fix store (`status 200 == 404`,
`status 400 == 404`) and pass with it; the whole reading suite stays green.

## 4. End-to-end validation

All timings from this session on this checkout. Harness sources are kept at
`/home/user/mo7_p26_data_e2e.py`, `/home/user/mo7_p26_tenant.py`,
`/home/user/mo7_p26_failures.py` (run from `/tmp` copies); they are ad-hoc
instruments, not part of the repository suite, because the durable regression
coverage for the fixed behaviour lives in `tests/reading/test_router.py`.

### 4.1 Database and schema (harness A, 50/50)

Real uvicorn server, real `DEEPTUTOR_HOME`:

* fresh boot creates `data/user/chat_history.db` — `PRAGMA integrity_check` =
  `ok`, `journal_mode` = `wal`, 13 tables incl. `sessions`/`messages`;
* the reading catalog (`workspace/reading/_catalog.sqlite3`) is created on first
  upload with `integrity_check` = `ok`;
* 10 concurrent uploads + workspace writes all succeed, session and catalog
  integrity stay `ok`;
* a third boot over the same database leaves row counts and tables unchanged
  (no destructive reset, no re-migration drift).

### 4.2 Migrations (targeted matrix + live)

* `tests/services/session/test_sqlite_store.py` 36 passed — legacy chat DB,
  notebook column migration, workspace-ownership migration without reordering,
  idempotent explicit migration;
* `tests/services/session/test_legacy_migration.py` 4; live: a real
  `workspace/chat/chat/sessions.json` is migrated at startup (session readable
  over HTTP, messages persisted) and the source is **archived** under
  `data/user/archive/legacy-chat/` rather than deleted;
* `tests/services/workspace/test_data_migration.py` 15;
  `tests/services/partners/test_channel_state_migration.py` 8.

### 4.3 Persistence across restart

Upload → restart → the material, its extracted text, its workspace tabs and its
renamed reading session all read back identically; sessions remain reachable
through `/api/sessions/{id}`.

### 4.4 Creation, read, update, deletion lifecycle

Workspace create/attach/rename/delete, reading-session create/rename/delete,
material create → read → update → delete, all verified over HTTP. Deleting the
workspace also removes its session rows.

### 4.5 Storage, retrieval, missing objects

Uploaded content lands under a stable 16-hex content directory
(`sha256(bytes)[:16]`) with manifest + extracted units; metadata reads back the
original filename; the file library round-trips bytes exactly, deduplicates
identical content to one object id, soft-deletes (download → 404), restores, and
downloads the same bytes again.

Removing the stored files behind a live catalog row: `/units/1` → 404, `/raw` →
404, delete → 200, id → 404, directory gone. Interrupted-write leftovers
(`.<id>.<uuid>.staging`, `.<id>.<uuid>.deleting`) are ignored by both listings
and never reach the library. Stale metadata (row without a directory) reads 404
and deletes cleanly.

### 4.6 Tenant isolation and direct identifiers (harness B, 40/40)

Two real accounts, real cookie login, real per-user roots
(`data/user` for admin, `data/users/<uid>/user/workspace/reading` for user2):

* user2 → admin material detail/units/raw/delete and collection: **404**;
* admin → user2 material: **404**; admin's own material intact afterwards;
* user2 listings (`/materials`, `/library/materials`) empty; user2 cannot list
  accounts (403); admin listing unaffected;
* on-disk: the two accounts share no content directory, and no admin bytes
  exist anywhere under the user2 root;
* file library: user2 cannot read, download or delete the admin file (404),
  admin's file survives, listings are per account;
* anonymous → `/files/library/` 401, `/api/reading/materials/{id}` 401;
* every tenant database passes `integrity_check`.

### 4.7 Failure and recovery (harness C, 11/11)

* duplicate uploads keep one identity;
* interrupted-write leftovers ignored, catalog integrity intact;
* stale metadata fails safely and can be deleted;
* a **read-only data root** makes the server refuse to boot rather than serve
  empty data;
* an **unusable session database** (directory where the file must be): the
  server refuses to boot; a corrupt catalog answers 500 on the route that needs
  it and 404 on material reads, and recovers when the file is restored.

### 4.8 API, runtime and security

* API smoke through the app server (port 3000 → backend 8765):
  `/api/settings` 200, `/api/reading/materials` 200, `/api/sessions` 200,
  `/files/library/` 308;
* backend `8765`: `/health/live` 200, `/health/ready` 200, `/api/settings` 200;
  web `3000`: `/` 307 → `/chat`, `/chat` 200, `/learning/reading` 200;
* security: an auth-enabled instance on a fresh home, Phase 29 probe reused —
  **14/14**: anonymous 401 on five protected routes, six forged/malformed token
  shapes rejected, bootstrap registration 201 → admin, self-registration closed
  (403), admin provisioning 201, non-admin 403 on the account list, empty own
  session list, wrong password 401.

### 4.9 Frontend

`npm run check:fast` → **RC 0**: Node tests 1224 pass / 0 fail; Vitest 427
passed / 108 files; ESLint 0 errors (49 pre-existing warnings); contracts,
dependency-cruiser architecture, typecheck and i18n parity/audit all clean
(en/zh/uk 100 %, fr 69.1 % used ≥ 65 % floor).

### 4.10 Browser

`WEB_BASE_URL=http://127.0.0.1:3000 npx playwright test --project=ui-audit
--project=epub-reader-chromium` → **63 passed (2.3 m)**, including the affected
reading flows (`reading-citation-material`, `reading-location-history`,
`reading-w3c-annotations`), the library/release matrix and the EPUB reader
(`epub-reader.audit.ts`) against the production build.

### 4.11 Production build and performance boundary

`NEXT_FONT_GOOGLE_MOCKED_RESPONSES=/tmp/mo7-font-mock.cjs npm run build` →
**RC 0** (Google Fonts is TLS-blocked in this sandbox; the mock only supplies the
font payloads). `npm run perf:check` → 8 routes within budget and the one known
Phase 28 failure (`/learning/reading/[workspaceId]/sessions/[sessionId]`
1128 KB / 1120 KB), unchanged from Phase 29 — a Phase 28 workstream item, not a
Phase 26 data/storage blocker. No performance work was done in this phase.

## 5. Regression matrix

| Gate | Command | Result |
|---|---|---|
| Python suite | `PYTHONPATH=. DEEPTUTOR_TEST_REDIS_URL=redis://127.0.0.1:6379/0 pytest -q tests deeptutor/learning/tests` | **8421 passed, 54 skipped**, 475 s, exit 0 (Phase 29 baseline: 8419 + the 2 new regressions) |
| Store/migration focus | 8 files (§4.2 plus identity/paths 4, owner-path 4, resource isolation 2, file library 26) | all passed |
| Ruff lint | `.venv/bin/ruff check .` | All checks passed |
| Ruff format | `.venv/bin/ruff format --check .` | 2 pre-existing files only (`services/codex_auth/oauth.py`, `tools/tex_downloader.py`); the two Phase 26 files are formatted |
| Import boundaries | `.venv/bin/lint-imports` | 3 kept, 0 broken |
| Architecture | `scripts/check_architecture.py` | OK |
| Security (static) | `bandit` on the changed store, `detect-secrets-hook --baseline .secrets.baseline` on both changed files | clean |
| Frontend | `npm run check:fast` | RC 0 (§4.9) |
| Browser | `ui-audit` + `epub-reader-chromium` | 63 passed (§4.10) |
| Build | `npm run build` (font mock) | RC 0 (§4.11) |
| Budgets | `npm run perf:check` | 1 known Phase 28 FAIL (§4.11) |
| Runtime | `/health/live`, `/health/ready`, `/`, API smoke | all 200/307 as expected (§4.8) |
| Live data/storage | harnesses A/B/C | 50/50, 40/40, 11/11 |

## 6. Definition of Done

| Requirement | Evidence |
|---|---|
| Production data flows verified end to end | §4.1–4.6, §4.8 |
| Persistence survives restart | §4.3 |
| Migrations safe and reproducible | §4.2, §4.1 (idempotent re-boot) |
| Database integrity verified | `integrity_check` on every store after each harness |
| Retrieval and lifecycle/deletion verified | §4.5, §4.4, F1/F2 fixed |
| Tenant isolation verified; direct ids cannot bypass authorization | §4.6, §4.8 |
| Failure paths safe, no fake success | §4.7 |
| Regression suites and production build pass | §5 |
| Runtime checks pass | §4.8 |
| No unresolved Phase 26 repository defect | §7 (findings are Phase 27/28 scope or external) |
| Repository clean, changes committed and pushed | §0, push verified with `git ls-remote` |
| Closure report exists | this document |

## 7. Remaining findings

### KNOWN FINDING (not Phase 26 blockers, carried forward)

1. **Reading-session route budget** — `/learning/reading/[workspaceId]/sessions/[sessionId]`
   1128 KB / 1120 KB. Pre-existing, unchanged by this phase, owned by the Phase 28
   performance workstream.
2. **Two pre-existing unformatted files** — `deeptutor/services/codex_auth/oauth.py`
   and `deeptutor/tools/tex_downloader.py` fail `ruff format --check`; untouched
   because they are unrelated to this phase.
3. **Corrupt-catalog route asymmetry** — F3: the catalog-backed library route
   fails loudly (500) while the store-backed `/materials` listing still answers
   from disk. Honest degraded behaviour, no fabricated rows; left as-is.
4. **`WEB_BASE_URL` default in `release-ui-matrix.audit.ts`** — the spec falls
   back to port 3300 and reports 32 false failures when the variable is unset.
   Test-harness ergonomics, not an application defect.

### OUT OF SCOPE

1. Authentication/RBAC redesign, archive containment — Phase 27.
2. Performance work, including the reading-session bundle — Phase 28.
3. WebKit and multi-worker browser projects; reading-payload optimisation.
4. Any product behaviour change beyond the minimal lifecycle fix above.

### VALIDATION BLOCKED — INFRASTRUCTURE

1. **Redis-backed integration tests** (`DEEPTUTOR_TEST_REDIS_URL`) — no Redis in
   this sandbox; the full suite reports them as skipped, not failed.
2. **Real-LLM resolver tests** — no LLM endpoint configured.
3. **WebKit browser project** — the only usable browser here is the pre-provisioned
   Chromium 143; the Playwright CDN route is TLS-blocked, so WebKit could not be
   installed. Chromium covered every affected flow.
4. **Multi-worker E2E project** — requires Redis-backed workers.

None of these touch the data/storage paths this phase verified; all are external
to the repository.

## 8. Verdict

`PHASE 26 READY`

The production data and storage foundation was verified end to end on real
servers, real SQLite stores and real per-account roots: creation, retrieval,
update, restart persistence, deletion and lifecycle, object storage, migration
safety, tenant isolation, direct-id authorization and failure/recovery all
behave as specified. One real lifecycle defect family (F1/F2) was found,
reproduced, minimally fixed, regression-tested and re-verified; the remaining
findings belong to Phase 27/28 or to the sandbox's missing external services.
