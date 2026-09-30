# MO7 Phase 28 Performance and Stability Baseline

Status: baseline recorded; no production optimization retained.

## Scope and invariants

This baseline covers the authoritative Phase 27 tree at commit
`c45f76574783189bd9a7f884b57b343740c509af`. It preserves the DeepTutor
foundation, MO7 product boundaries, API contracts, security behavior, and the
Phase 27 TeX archive-containment implementation. Phase 27 remains
`VALIDATION BLOCKED — INFRASTRUCTURE`; this document does not change that
status.

No production-code, test, dependency, package-manifest, or lockfile change was
made for the performance work. The only Phase 28 repository change is this
measurement record.

## Measurement environment

- Linux x86_64, Debian GNU/Linux 12 (bookworm), kernel 6.1.158+
- Python 3.11.2; declared development and partner requirements installed in a
  local `.venv`
- Node v22.22.3; npm 10.9.8; frontend dependencies installed from the existing
  lockfile with `npm ci --legacy-peer-deps`
- No active LLM model configured; no Redis service configured for this local
  run
- Measurements were taken in the Arena sandbox, not on production hardware

## Baseline measurements

### API startup and import cost

Each startup sample launched a fresh Uvicorn process with a unique temporary
`DEEPTUTOR_HOME` and measured time from process creation until
`GET /health/live` returned HTTP 200. Five samples were collected; all five
started successfully.

| Metric | Samples | Result |
|---|---:|---:|
| Fresh process to `/health/live` | 5 | min 2,280.14 ms; median 2,519.94 ms; max 2,553.77 ms |
| `import deeptutor.api.main` wall time | 20 | p50 1,835.71 ms; p95 2,216.85 ms; max 2,910.52 ms |
| `import deeptutor.runtime.orchestrator` wall time | 20 | p50 187.32 ms; p95 214.28 ms; max 222.20 ms |
| Importing application container factory | 20 | p50 424.68 ms; p95 508.86 ms; max 528.09 ms |

Import samples include interpreter/process startup and were used for relative
comparison only. They are not a production cold-start SLA.

A one-sample `cProfile` run of `import deeptutor.api.main` recorded 3.97M
function calls in 3.116 seconds. The largest cumulative groups were FastAPI
route registration (`add_api_route` and route state construction, about 1.12 s)
and Pydantic model construction/type-adapter work (about 0.84 s). This is a
confirmed startup-cost hotspot, but reducing eager route/model construction
would require a broad application-lifecycle change and was not justified as a
small safe Phase 28 optimization.

### Local API latency

A running local API was warmed with ten requests and then measured with 100
sequential keep-alive requests per route using `httpx`. All 300 requests
returned HTTP 200. The values below are local handler/request-path timings,
not network or production measurements.

| Route | Payload | Samples | p50 | p95 | p99 | Max |
|---|---:|---:|---:|---:|---:|---:|
| `/health/live` | 18 bytes | 100 | 2.621 ms | 3.172 ms | 3.422 ms | 3.511 ms |
| `/` | 32 bytes | 100 | 2.602 ms | 3.035 ms | 3.435 ms | 3.804 ms |
| `/health/ready` | 18 bytes | 100 | 2.808 ms | 3.284 ms | 3.859 ms | 4.025 ms |

These endpoints do not exercise LLM calls, authentication-backed data access,
Redis coordination, large-file processing, or production network conditions.
No API latency bottleneck was established from this harness.

### Stability smoke

A fresh isolated API process handled 500 sequential `GET /health/live`
requests. All 500 returned HTTP 200 with no client errors. The process RSS was
135,276 KiB after warm-up and remained 135,276 KiB at each 100-request
checkpoint, for a measured delta of 0 KiB. This is a bounded smoke signal for a
lightweight route, not proof of leak-free behavior under authenticated,
LLM-backed, Redis-backed, or large-file workloads.

### Frontend and route budgets

The repository provides `npm run perf:check`, which measures raw production
route JavaScript against the configured budgets. A production build could not
be produced in this sandbox because `next/font` could not reach
`fonts.googleapis.com` and failed with TLS `ECONNRESET`. Therefore route-size
measurements and browser-based frontend measurements are not available in this
environment. This is classified as an infrastructure limitation, not an
application result.

### Database, files, and asynchronous work

Static inspection and the existing runtime/session/document test surfaces were
used to identify candidate paths. No reproducible N+1 query, connection leak,
unbounded queue, retry storm, or large-file regression was demonstrated by the
available local harnesses. Configured LLM, Redis, production database, and
browser-backed workloads were not available, so those areas remain
insufficiently evidenced rather than declared clean under production load.

## Bottleneck classification

| Finding | Classification | Decision |
|---|---|---|
| Eager API route and Pydantic construction contributes materially to cold startup | Confirmed bottleneck | Documented; no change because a safe fix would require broad lifecycle/import restructuring |
| `/`, `/health/live`, and `/health/ready` local request path | Non-bottleneck in this harness | No change |
| Frontend production build and route-budget measurement | Infrastructure limitation | No source workaround; rerun on CI/Windows or a network-enabled runner |
| Browser UI/E2E performance | Infrastructure limitation | Chromium download is unavailable in the sandbox |
| Production LLM/Redis/database/file-processing behavior | Insufficient evidence | Requires service-backed, representative benchmarks |

No speculative production optimization was retained. Consequently there are no
before/after optimization measurements to claim.

## Budgets and acceptance status

The following metrics are now repeatable baseline signals, but responsible
absolute targets are **TARGET NOT YET RELIABLE** because the measurements are
from a constrained sandbox and omit configured external services:

- fresh process to `/health/live`
- `deeptutor.api.main` import wall time
- local health/root route p50, p95, and p99
- frontend route JavaScript budgets from `npm run perf:check`

The next performance run should repeat the same commands on CI-like or
production-like hardware before setting an acceptance threshold. No arbitrary
performance target was introduced in this phase.

## Reproduction

From the repository root:

```bash
# API import timing/profile
python -c "import cProfile; cProfile.run('import deeptutor.api.main', sort='cumulative')"

# Start an isolated local API
DEEPTUTOR_HOME=/tmp/mo7-phase28-runtime \
  python -m uvicorn deeptutor.api.main:app --host 127.0.0.1 --port 8765

# Repeat route measurements with a local httpx harness, using 10 warmups and
# 100 sequential samples per route.

# Frontend route budgets (requires a successful production build)
cd web
npm run build
npm run perf:check
```

## Validation recorded in this phase

- Phase 27 focused tests: 2 passed.
- Full Python regression: 8,419 passed, 54 skipped, 37 warnings.
- Ruff lint, import architecture, application architecture, and compile checks:
  passed. Ruff format check still reports the two pre-existing target-tree
  differences and was not allowed to rewrite them.
- Node tests: 1,220 passed; Vitest: 108 files and 427 tests passed;
  TypeScript passed; ESLint passed with 0 errors and 49 warnings.
- Contracts, dependency architecture, i18n, Bandit, and detect-secrets: passed.
- Production build and route-budget check: blocked by the external Google Fonts
  TLS failure described below.

## Limitations and follow-up

- The sandbox cannot currently reach Google Fonts, Playwright CDN endpoints, or
  Debian package repositories. These block the production build and browser
  validation gates.
- The critical-turn and multi-worker workloads require their deterministic
  backend fixture plus Redis/four-worker infrastructure; no such service was
  available locally.
- The startup hotspot is a candidate for a separately scoped design/profiling
  effort. It was not changed here because the evidence does not identify a
  small, behavior-preserving optimization with adequate regression coverage.
