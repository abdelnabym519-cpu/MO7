# MO7 — Product Boundary & Identity

> Canonical product document (established in Phase 22 — Productization).
> Freeze record and its rules: [MO7_FOUNDATION_BASELINE.md](MO7_FOUNDATION_BASELINE.md).

## 1. Model

```text
Upstream foundation:  HKUDS/DeepTutor v1.6.11  (commit a053fecf)
        ↓  frozen at
Freeze:               mo7-foundation-v1.0      (commit f337295)
        ↓  productized by
MO7 product layer:    this repository (abdelnabym519-cpu/MO7)
```

**MO7 is a product built on DeepTutor — not a rewrite of it.** The foundation
code keeps its upstream module/CLI/container identifiers; the product layer is
identity, configuration boundary, documentation, release, and change rules.

## 2. Product Identity (single source per concern)

| Concern | Value | Source of truth |
|---|---|---|
| Product name | **MO7** | this document; `web/package.json` `name: mo7-web`; API OpenAPI title `MO7 API`; README preamble |
| Foundation name | DeepTutor (`deeptutor`) | frozen: python package `deeptutor`, CLI `deeptutor`, containers `deeptutor-*`, image names — **KEEP, do not rename** (renaming = architectural churn, zero product value) |
| Backend/API identity | `MO7 API` + version `1.6.11` | `deeptutor/api/main.py` → OpenAPI `info`; generated mirror `web/contracts/schema/openapi.json` (`info` only) |
| Web package identity | `mo7-web` (private, internal) | `web/package.json` (+ lockfile) |
| Version (runtime/backend) | **1.6.11** — the foundation version | `deeptutor/__version__.py` (single source; drives pyproject `version`, API `info.version`, web sidebar badge, CLI banner) |
| Frontend app version | 1.0.0 (web-internal counter, unrelated to product version) | `web/package.json` `version` |

Root `/` endpoint answers `Welcome to MO7 API` (runtime identity).

## 3. Versioning Model

```text
DeepTutor foundation version:  v1.6.11            (upstream, immutable)
Freeze marker:                 mo7-foundation-v1.0 (git tag, immutable — never move/rewrite)
MO7 product version:           NOT YET DECLARED — intentionally.
```

No product version is minted in this phase: there is no MO7 release yet, and
inventing a cosmetic version would create a second, unsourced version. The
release process (later CI/CD & Release phase) defines `v*` product tags on the
MO7 repository; until then, "MO7 @ freeze" is the only product coordinate.

## 4. Configuration Boundaries

```text
Foundation defaults (code: deeptutor/services/config, defaults seeded at first run)
        ↓ overridden by
Runtime settings: data/user/settings/* (gitignored, per-installation; frontend/build
                   reads system.json/auth.json — see web/next.config.js)
        ↓ overridden by
Environment variables / .env (gitignored; deployment overrides)
Template: .env.example (tracked, must stay secret-free)
```

Rules:

- No secrets in source control — verified: only `.env.example` is tracked;
  `.env*` is ignored (`!.env.example` exception).
- Test configuration for Python CI: `tests/fixtures/ci_model_catalog.json` +
  minimal `data/user/settings/main.yaml` (see `.github/workflows/tests.yml`).
- Do not add environment variables without an identified product need.
- Configuration loading behavior of the foundation is frozen — do not change it.

## 5. Environment Boundaries

| Environment | Backend | Frontend |
|---|---|---|
| Development | `deeptutor start --dev` | `npm run dev` (`web/`) |
| Test | pytest (`tests/`, `deeptutor/learning/tests`) | `npm run check:fast` gates + vitest |
| Production | uvicorn via `deeptutor start` | `npm run build && npm start`; Docker `compose.yaml` |

Redis: used by coordination tests/production; absence skips `redis_integration`
tests locally (CI provides the service).

## 6. Foundation vs Product — KEEP / ADAPT / ADD (as executed)

**KEEP (foundation, frozen — do not modify for identity reasons):**
`deeptutor` package & module names, `deeptutor_cli`, CLI name/banner, Docker
container names + `ghcr.io/hkuds/deeptutor` image references, compose service
names, `deeptutor/__version__.py` (= 1.6.11), backend protocol marker
`x-deeptutor-web-protocol-version`, and upstream issue references in comments.
Product-facing UI strings and metadata are governed by the Phase 24 branding
pass; their underlying keys and links remain contract-compatible.

**ADAPT (product technical identity, Phase 22 — executed):**

| File | Change | Validation |
|---|---|---|
| `deeptutor/api/main.py` | FastAPI `title` → `MO7 API`; `version` now sourced from `deeptutor/__version__` (was hardcoded `1.0.0` — duplicate/stale); `/` welcome message → `Welcome to MO7 API` | python suite, contracts echo, smoke |
| `web/package.json`, `web/package-lock.json` | package `name` `opentutor-web` → `mo7-web` (private package; metadata only; `version` untouched) | npm ci, all node gates |
| `web/contracts/schema/openapi.json` | `info.title`/`info.version` transplanted to match the backend (2-line change) | contracts:check |
| `README.md` | MO7 product preamble added; upstream README preserved unchanged below | review |

**ADD (Phase 22 — executed):**
this document `MO7_PRODUCT.md`.

## 7. Upstream Relationship

- Remotes: `origin` = MO7 product repo; `upstream` = HKUDS/DeepTutor
  (configured on the primary development machine; never deleted/renamed/replaced).
- Upstream history is never rewritten; the freeze tag is never moved.
- Any future upstream update enters only through a dedicated update process:
  freeze-current → merge/cherry-pick onto a branch → full harness (Section: validation
  in `MO7_FOUNDATION_BASELINE.md`) → re-audit deltas → new freeze tag. Never
  silently re-base the product onto upstream HEAD.

## 8. Release Rules

- Foundation freeze tags: `mo7-foundation-*` — immutable.
- Product releases: to be defined by the CI/CD & Release phase (tags on the MO7
  repository + container images under MO7-owned registry paths).
- Until then: no release, no product version number, no image republishing.

## 9. Change Rules (delta from the freeze rules)

1. Foundation behavior changes require evidence and a regression harness run.
2. Identity changes must be metadata-level (docs, config, OpenAPI info,
   package metadata) unless a future phase (Branding/UX) owns them.
3. Generated artifacts are updated through their generators
   (`deeptutor.api.contracts.export`, `web/scripts/generate-contracts.mjs`).
4. No feature development under productization scope (see freeze rules 1–10).

## 10. Explicitly Deferred (with reason)

| Item | Deferred to | Reason |
|---|---|---|
| App-update check URL `services/app_update.py` → HKUDS releases | Release phase | MO7 has no release feed yet; repointing now would check a nonexistent feed |
| UI branding (product labels, i18n strings, CLI banner) | Phase 24 — Branding & Product Identity | completed as a minimal product-facing pass; logos/assets, foundation package names, release URLs, and protocol links remain unchanged |
| Docker image naming/registry for MO7 | CI/CD & Release phase | needs release pipeline |
| Stale `web/contracts/schema/openapi.json` vs exporter output (562 diff lines; missing `file_preview` upload body, `WebContinuityBody`, `capability_once` schemas) | Foundation-tooling maintenance (upstream-lag) | pre-existing on the freeze commit; normalizing is not Phase-22 scope; verified `turn-protocol.json` clean and 2-line `info` transplant is intentional |
