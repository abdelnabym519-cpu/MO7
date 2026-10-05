#!/usr/bin/env python3
"""Render the staging deployment's application settings.

Settings are written through the application's own settings service, so the
files staging runs on have exactly the shape the application creates — no
hand-written JSON and no environment variable the app does not document.

Values come from etc/staging.env (the deployment contract): bind ports, the
origins the browser will actually use, security controls that must stay on, and
the release's disabled outbound update check.
"""
from __future__ import annotations

import json
import os

PUBLIC_ORIGIN = os.environ["STAGING_PUBLIC_ORIGIN"]
TLS_ORIGIN = os.environ["STAGING_TLS_ORIGIN"]
BACKEND_PORT = int(os.environ["STAGING_BACKEND_PORT"])
FRONTEND_PORT = int(os.environ["STAGING_FRONTEND_PORT"])

from deeptutor.services.config.runtime_settings import (  # noqa: E402
    ensure_runtime_settings_files,
    get_runtime_settings_service,
)


def main() -> int:
    ensure_runtime_settings_files()
    service = get_runtime_settings_service()

    system = dict(service.load_system())
    system.update(
        {
            "backend_port": BACKEND_PORT,
            "frontend_port": FRONTEND_PORT,
            # The browser reaches this deployment on the platform origin and on
            # the local TLS ingress; anything else is not a legitimate caller.
            "cors_origins": [
                PUBLIC_ORIGIN,
                TLS_ORIGIN,
                f"http://127.0.0.1:{FRONTEND_PORT}",
            ],
            # Staging must not call out to the release feed: no outbound update
            # traffic from a validation host, and no unreviewed version note.
            "version_check_enabled": False,
        }
    )
    service.save_system(system)

    auth = dict(service.load_auth())
    auth.update(
        {
            "enabled": True,
            "token_expire_hours": 24,
            # The deployment is reached over TLS (platform ingress and the local
            # ingress); a Secure cookie is therefore correct and required.
            "cookie_secure": True,
        }
    )
    service.save_auth(auth)

    summary = {
        "settings_dir": str(service.settings_dir),
        "system": {
            key: system[key]
            for key in (
                "backend_port",
                "frontend_port",
                "cors_origins",
                "version_check_enabled",
            )
        },
        "auth": {
            "enabled": auth["enabled"],
            "cookie_secure": auth["cookie_secure"],
            "token_expire_hours": auth["token_expire_hours"],
        },
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
