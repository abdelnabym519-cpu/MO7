#!/usr/bin/env bash
# =============================================================================
# MO7 production browser matrix
# =============================================================================
# Runs the repository's Playwright audit suites against the deployed production
# frontend through the *validation* ingress (a local TLS terminator used only by
# the harnesses; production TLS terminates at the platform edge and is verified
# from outside the deployment host).
#
# The browser is the Chromium build supplied by @sparticuz/chromium, because
# the Playwright CDN is unreachable from this host; the bundled AL2023
# libraries are put on LD_LIBRARY_PATH rather than installed system-wide.
#
#   prod_browser.sh [playwright args...]
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_DIR="${MO7_WEB_DIR:-/home/user/MO7/web}"
CHROMIUM_HOME="${MO7_CHROMIUM_HOME:-/home/user/mo7-prod-build/chromium}"
PROD_ROOT="${PROD_ROOT:-/home/user/mo7-prod}"

export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"
export LD_LIBRARY_PATH="$CHROMIUM_HOME/chromium-deps/al2023/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export WEB_BASE_URL="${WEB_BASE_URL:-https://127.0.0.1:8443}"
export MO7_STORAGE_STATE="${MO7_STORAGE_STATE:-$PROD_ROOT/harness/storage-state.json}"

if [ ! -e "$HERE/node_modules" ]; then
  # Playwright resolves @playwright/test relative to the config file.
  ln -sfn "$WEB_DIR/node_modules" "$HERE/node_modules"
fi

cd "$WEB_DIR"
exec ./node_modules/.bin/playwright test --config="$HERE/playwright.production.config.ts" "$@"
