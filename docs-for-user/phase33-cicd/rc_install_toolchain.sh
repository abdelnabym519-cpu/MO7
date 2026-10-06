#!/usr/bin/env bash
# Validation toolchain for the release pipeline.
#
# Split out of the workflow so the pipeline step is one reviewable command and
# the install list is versioned alongside the rest of the release tooling. The
# versions here are the ones the repository's own CI pins (tests.yml), so the
# release pipeline validates with the same tools as the repository gate.
set -euo pipefail

python -m pip install --upgrade pip
pip install -r requirements/server.txt -r requirements/partners.txt
pip install -e . --no-deps
# The build backend: rc_build.sh builds the wheel with `python -m build`.
pip install build wheel
# Gates: tests, lint/format, import boundaries, security scan.
pip install pytest pytest-asyncio ruff==0.16.0 import-linter==2.11 bandit

echo "validation toolchain installed"
python -c "import sys; print('python', sys.version.split()[0])"
ruff --version
lint-imports --version
bandit --version 2>&1 | head -1
