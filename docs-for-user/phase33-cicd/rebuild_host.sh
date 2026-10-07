#!/usr/bin/env bash
# Rebuild the deployment host, the way it was actually rebuilt on 2026-10-07.
#
# The host is the thing the release process is measured against, so rebuilding it
# is a release-process activity, not ad-hoc operation: the point of running this
# sequence again is to prove the documented order works from an empty machine.
#
# The order is not decorative, and two of its steps exist because the obvious
# order does not work on a host that has never been configured:
#
#   * the FIRST promotion has to run with --skip-smoke. Auth is disabled until
#     init-config writes the settings, and init-config needs a *current* release
#     (it uses the promoted release's interpreter), so a virgin host cannot pass
#     the post-promote smoke -- the smoke authenticates an account that cannot
#     exist yet. The bootstrap promotion is therefore documented with the smoke
#     deferred; the same release is re-promoted with the complete gate three steps
#     later, and every subsequent promotion (including the certified one) runs it.
#     If you run the first promotion without --skip-smoke the deployment flips the
#     symlink, fails the smoke, dies, and never writes run/current-release.json or
#     its deployment-log line -- verify then reports four failures against a
#     release that is in fact serving (observed, and it is what this script is
#     written from).
#   * `session <account>` mints the browser harness's storage state. Without it the
#     whole Chromium matrix fails to launch (P33-M18).
set -euo pipefail

MO7=${MO7_CHECKOUT:-/home/user/MO7}
CICD=${MO7_RELEASE_WORKDIR:-/home/user/mo7-cicd}
PROD_ROOT=${PROD_ROOT:-/home/user/mo7-prod}
FIRST_RELEASE=${FIRST_RELEASE:-1.6.11-0af36b8-bootstrap}
FIRST_COMMIT=${FIRST_COMMIT:-0af36b89c97d92ed2a411e6db4f67601645d4ee7}
FIRST_WHEEL=${FIRST_WHEEL:-$CICD/build/bootstrap-0af36b8/deeptutor-1.6.11-py3-none-any.whl}

step() { printf '\n=== %s ===\n' "$*"; }

step "1. bootstrap the host (layout, contract, secrets, instruments, first release staged)"
MO7_SOURCE_CHECKOUT="$MO7" bash "$MO7/docs-for-user/phase32-infra/prod_bootstrap.sh" \
  "$FIRST_WHEEL" --release-id "$FIRST_RELEASE" --commit "$FIRST_COMMIT"

step "2. first promotion, smoke deferred (see the header for why)"
PROD_ROOT="$PROD_ROOT" "$PROD_ROOT/bin/prod.sh" promote --release "$FIRST_RELEASE" --skip-smoke

step "3. render the settings (needs a current release) and restart so they are read"
PROD_ROOT="$PROD_ROOT" "$PROD_ROOT/bin/prod.sh" init-config

step "4. start the validation-only TLS ingress"
PROD_ROOT="$PROD_ROOT" "$PROD_ROOT/bin/prod.sh" ingress start

step "5. create the deployment accounts"
PROD_ROOT="$PROD_ROOT" "$PROD_ROOT/bin/prod.sh" provision

step "6. re-promote the first release with the complete gate"
PROD_ROOT="$PROD_ROOT" "$PROD_ROOT/bin/prod.sh" promote --release "$FIRST_RELEASE"

step "7. mint the browser harness session and verify the host"
PROD_ROOT="$PROD_ROOT" "$PROD_ROOT/bin/prod.sh" session admin
PROD_ROOT="$PROD_ROOT" "$PROD_ROOT/bin/prod.sh" verify --quick

step "8. the host runs the tooling from the repository, not a stale copy"
bash "$MO7/docs-for-user/phase33-cicd/check_host_convergence.sh"

echo
echo "host rebuilt: $PROD_ROOT is live on $FIRST_RELEASE"
echo "next: ingest the CI evidence, validate, approve and promote the certified release"
