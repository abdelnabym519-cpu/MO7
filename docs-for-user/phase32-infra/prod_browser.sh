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
PROD_ROOT="${PROD_ROOT:-/home/user/mo7-prod}"

# The browser is the Chromium build inflated from @sparticuz/chromium, because
# the Playwright CDN is unreachable from this host. Its ELF libraries are not on
# the default search path, so the wrapper finds them: the dependency archive is
# untarred either directly under chromium-deps/ or under a nested directory
# (the AL2023 layout), and both are accepted rather than hard-coding one host's
# extracted shape.
CHROMIUM_HOME="${MO7_CHROMIUM_HOME:-}"
if [ -z "$CHROMIUM_HOME" ]; then
  for candidate in /home/user/mo7-build/chromium /home/user/mo7-prod-build/chromium; do
    if [ -x "$candidate/chrome-linux/chrome" ]; then
      CHROMIUM_HOME="$candidate"
      break
    fi
  done
fi
[ -n "$CHROMIUM_HOME" ] && [ -x "$CHROMIUM_HOME/chrome-linux/chrome" ] || {
  echo "FATAL: no Chromium build found; set MO7_CHROMIUM_HOME to the extracted @sparticuz/chromium directory" >&2
  exit 78
}

DEPS_LIB=""
for candidate in "$CHROMIUM_HOME"/chromium-deps/lib "$CHROMIUM_HOME"/chromium-deps/*/lib; do
  if compgen -G "$candidate/libnspr4.so" >/dev/null; then
    DEPS_LIB="$candidate"
    break
  fi
done
[ -n "$DEPS_LIB" ] || {
  echo "FATAL: the Chromium library directory (libnspr4.so) is missing under $CHROMIUM_HOME/chromium-deps" >&2
  exit 78
}

export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"
export LD_LIBRARY_PATH="$DEPS_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export WEB_BASE_URL="${WEB_BASE_URL:-https://127.0.0.1:8443}"
export MO7_STORAGE_STATE="${MO7_STORAGE_STATE:-$PROD_ROOT/harness/storage-state.json}"

if [ ! -e "$HERE/node_modules" ]; then
  # Playwright resolves @playwright/test relative to the config file.
  ln -sfn "$WEB_DIR/node_modules" "$HERE/node_modules"
fi

cd "$WEB_DIR"
exec ./node_modules/.bin/playwright test --config="$HERE/playwright.production.config.ts" "$@"
