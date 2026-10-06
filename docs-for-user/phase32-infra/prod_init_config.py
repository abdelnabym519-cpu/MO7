#!/usr/bin/env python3
"""Render the production deployment's application settings.

Settings are written through the application's own settings service, so the
files production runs on have exactly the shape the application creates — no
hand-written JSON and no environment variable the app does not document.

Values come from the deployment contract (`etc/production.env`): bind ports,
the origins the browser will actually use (the platform edge and the validation
ingress), security controls that must stay on, and the disabled outbound update
check. The auth secret is *not* created here: prod_bootstrap.sh materialises it
under secrets ownership so a production start can require it (see
templates/start-backend.sh.tmpl).
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prod_config as cfg  # noqa: E402

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
            "backend_port": cfg.BACKEND_PORT,
            "frontend_port": cfg.FRONTEND_PORT,
            # The browser reaches this deployment on the platform origin and on
            # the validation ingress; anything else is not a legitimate caller.
            "cors_origins": [
                cfg.PUBLIC_ORIGIN,
                cfg.TLS_VALIDATION_ORIGIN,
                f"http://127.0.0.1:{cfg.FRONTEND_PORT}",
            ],
            # Production must not call out to the release feed on its own: no
            # outbound update traffic and no unreviewed version note.
            "version_check_enabled": False,
        }
    )
    service.save_system(system)

    auth = dict(service.load_auth())
    auth.update(
        {
            "enabled": True,
            "token_expire_hours": 24,
            # The deployment is reached over TLS (platform edge, validation
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
