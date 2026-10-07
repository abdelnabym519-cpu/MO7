#!/usr/bin/env bash
# Rebuild the Chromium the browser harness uses, after the second recycle.
#
# The Playwright CDN is unreachable from this environment, so the browser comes
# from the npm package @sparticuz/chromium (a Chromium built for AL2023 whose
# bundled libraries are not on this image). Two layouts are produced, because
# two consumers want different ones:
#
#   $CHROMIUM_HOME/chrome-linux/chrome          -- what prod_browser.sh looks for
#   $CHROMIUM_HOME/chromium-deps/.../lib        -- the AL2023 shared libraries
#   ~/.cache/ms-playwright/chromium-1200/...    -- what Playwright resolves itself
#
# The package is ESM-only (`"type": "module"`, an `exports` map and no `main`), so
# its API is reached through `import`, not `require` -- a detail that cost a build
# cycle.  Runbook section 17 step 3.
set -euo pipefail

CHROMIUM_HOME=${MO7_CHROMIUM_HOME:-/home/user/mo7-prod-build/chromium}
WORK=${MO7_CHROMIUM_WORK:-/home/user/mo7-prod-build/chromium-work}
mkdir -p "$CHROMIUM_HOME" "$HOME/.cache/ms-playwright" "$WORK"

cd "$WORK"
if [ ! -d node_modules/@sparticuz/chromium ]; then
  npm init -y >/dev/null 2>&1
  npm install --no-audit --no-fund @sparticuz/chromium@153.0.0 2>&1 | tail -2
fi
PKG="$WORK/node_modules/@sparticuz/chromium"

cat >"$WORK/get-path.mjs" <<'EOF'
const chromium = (await import(process.argv[2] + "/build/index.js")).default;
console.log(await chromium.executablePath());
EOF
BROWSER=$(node "$WORK/get-path.mjs" "$PKG" | tail -1)
[ -x "$BROWSER" ] || { echo "FATAL: @sparticuz/chromium did not materialise a browser" >&2; exit 1; }
echo "materialised browser at $BROWSER"

rm -rf "$CHROMIUM_HOME/chrome-linux"
mkdir -p "$CHROMIUM_HOME/chrome-linux"
cp -f "$BROWSER" "$CHROMIUM_HOME/chrome-linux/chrome"
chmod 755 "$CHROMIUM_HOME/chrome-linux/chrome"

# The AL2023 dependency archive, inflated with the package's own helper (it is
# only auto-inflated on Amazon Linux).
cat >"$WORK/inflate-deps.mjs" <<'EOF'
import { createBrotliDecompress } from "node:zlib";
import { createReadStream, createWriteStream } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { execFile } from "node:child_process";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";

const run = promisify(execFile);
const pkg = process.argv[2];
const out = process.argv[3];
await rm(out, { recursive: true, force: true });
const { mkdir } = await import("node:fs/promises");
await mkdir(out, { recursive: true });
const tar = join(await mkdtemp(join(tmpdir(), "al2023-")), "al2023.tar");
await new Promise((resolve, reject) => {
  createReadStream(join(pkg, "bin/al2023.tar.br"))
    .pipe(createBrotliDecompress())
    .pipe(createWriteStream(tar))
    .on("finish", resolve)
    .on("error", reject);
});
await run("tar", ["-xf", tar, "-C", out]);
console.log("al2023 libraries inflated into " + out);
EOF
node "$WORK/inflate-deps.mjs" "$PKG" "$WORK/al2023"
rm -rf "$CHROMIUM_HOME/chromium-deps"
mkdir -p "$CHROMIUM_HOME/chromium-deps"
cp -rf "$WORK"/al2023/* "$CHROMIUM_HOME/chromium-deps/"
DEPS=$(dirname "$(find "$CHROMIUM_HOME/chromium-deps" -name libnspr4.so | head -1)")
[ -n "$DEPS" ] || { echo "FATAL: libnspr4.so not found after inflating the dependency archive" >&2; exit 1; }
echo "libraries at $DEPS"

# The Playwright layout: chromium-1200/chrome-linux64/chrome. Playwright 1.57
# looks for the directory name chrome-linux64; a hand-built cache with the other
# name made every test fail to launch (P33-M18).
mkdir -p "$HOME/.cache/ms-playwright/chromium-1200/chrome-linux64"
cp -f "$CHROMIUM_HOME/chrome-linux/chrome" "$HOME/.cache/ms-playwright/chromium-1200/chrome-linux64/chrome"
touch "$HOME/.cache/ms-playwright/chromium-1200/INSTALLATION_COMPLETE"

echo "chromium version check:"
LD_LIBRARY_PATH="$DEPS" "$CHROMIUM_HOME/chrome-linux/chrome" --version
echo "CHROMIUM_REBUILD_DONE"
