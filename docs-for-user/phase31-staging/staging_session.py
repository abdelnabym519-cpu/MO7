#!/usr/bin/env python3
"""Mint the browser harness's session state from a staging account.

The Chromium matrix acts as the staging administrator (the account whose
surfaces are not gated). Restricted-user behaviour inside the suite is
mock-driven in the specs themselves, and RBAC / tenant isolation are asserted
against the API by ``staging_validate.py`` and ``staging_security.py``.

Usage: staging_session.py [account]        # account defaults to ``admin``
Writes: <staging root>/harness/storage-state.json (mode 0600)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "harness"))
from staging_http import Actor, login  # noqa: E402

STAGING = Path(os.environ.get("STAGING_ROOT", "/home/user/mo7-staging"))
TLS_HOST = os.environ.get("STAGING_TLS_HOST", "127.0.0.1")


def main() -> int:
    account = sys.argv[1] if len(sys.argv) > 1 else "admin"
    credentials = json.loads((STAGING / "secrets/staging-credentials.json").read_text())
    if account not in credentials:
        print(
            f"unknown account {account!r}; known: {', '.join(sorted(credentials))}", file=sys.stderr
        )
        return 2

    actor = Actor(account)
    status, _, body, payload = login(
        actor, credentials[account]["username"], credentials[account]["password"]
    )
    token = actor.cookies.get("dt_token")
    if status != 200 or not token:
        print(f"login failed: status={status} body={body[:200]}", file=sys.stderr)
        return 1

    target = STAGING / "harness/storage-state.json"
    target.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "dt_token",
                        "value": token,
                        "domain": TLS_HOST,
                        "path": "/",
                        "expires": -1,
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    }
                ],
                "origins": [],
            },
            indent=2,
        )
    )
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(f"wrote {target} for account {account} ({payload.get('username', account)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
