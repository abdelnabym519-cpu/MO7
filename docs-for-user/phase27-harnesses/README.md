# Phase 27 harnesses

The three live security harnesses the Phase 27 report cites, kept in the repo so the
evidence can be reproduced from the report. They are test tooling, not product code: they
start their own uvicorn process against a fresh `DEEPTUTOR_HOME` under `/tmp`, exercise the
real HTTP surface, and exit non-zero on any failed expectation.

| Script | Checks | What it covers |
|---|---|---|
| `mo7_p27_attack.py` | 56 | bootstrap, authentication attacks, RBAC, object-level authorization, the cross-tenant matrix, traversal, zip-slip containment, information disclosure, CORS, cache controls, resource-exhaustion boundaries, token revocation, the login throttle, and the Phase 26 tenant-continuity probes |
| `mo7_p27_probe.py` | 2 assertions + header evidence | the same conclusions over raw `http.client` (wire-level `Set-Cookie`/CORS headers), plus failed-login cost measurement |
| `mo7_p29_probe.py` | 15 | the Phase 29 checklist as recorded in `PHASE_29_QA_CLOSURE_REPORT.md` §6 (anonymous refusal, six token shapes, bootstrap, admin login/provisioning, closed registration, second-user isolation, wrong password) |

Run them from the repository root with the project venv present:

```bash
python3 docs-for-user/phase27-harnesses/mo7_p27_attack.py   # exit 0 = all checks passed
python3 docs-for-user/phase27-harnesses/mo7_p27_probe.py
python3 docs-for-user/phase27-harnesses/mo7_p29_probe.py
```

Each script needs no arguments; it picks a free port, seeds a throwaway home, and prints
`total / passed / failed` before exiting. `mo7_p27_attack.py` also writes
`harness-results.json` into its temporary home.
