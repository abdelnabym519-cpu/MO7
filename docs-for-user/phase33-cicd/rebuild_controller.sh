#!/usr/bin/env bash
# Rebuild the release controller's work directory after the second recycle.
set -euo pipefail

MO7=${MO7:-/home/user/MO7}
CICD=/home/user/mo7-cicd

mkdir -p "$CICD"/{state,rc,negative,build,packs,fonts,work}

echo "=== 1. build venv (MO7_BUILD_PYTHON) ==="
if [ ! -x "$CICD/venv/bin/python" ]; then
  python3 -m venv "$CICD/venv"
fi
"$CICD/venv/bin/pip" install -q --upgrade pip setuptools wheel build
"$CICD/venv/bin/python" -c "import build, setuptools, wheel; print('build venv ok: setuptools', setuptools.__version__)"

echo "=== 2. gate venv (ruff + pyyaml) ==="
if [ ! -x "$CICD/gate-venv/bin/python" ]; then
  python3 -m venv "$CICD/gate-venv"
fi
"$CICD/gate-venv/bin/pip" install -q ruff pyyaml
"$CICD/gate-venv/bin/ruff" --version

echo "=== 3. offline font payloads ==="
cp -f "$MO7/docs-for-user/phase33-cicd/font-mock.cjs" "$CICD/font-mock.cjs"
WORK=$(mktemp -d /tmp/fontpacks-XXXXXX)
( cd "$WORK" && npm pack geist@1.7.2 @fontsource/lora@5.3.0 >/dev/null )
for t in "$WORK"/*.tgz; do tar -xzf "$t" -C "$WORK"; done
for f in Geist-Variable.woff2 lora-latin-400-normal.woff2 lora-latin-500-normal.woff2 lora-latin-600-normal.woff2; do
  found=$(find "$WORK" -name "$f" | head -1)
  [ -n "$found" ] || { echo "FATAL: $f not found in the font packages" >&2; exit 1; }
  cp -f "$found" "$CICD/fonts/$f"
done
rm -rf "$WORK"
ls -la "$CICD/fonts/"
echo "=== rebuild of the controller's work directory complete ==="
