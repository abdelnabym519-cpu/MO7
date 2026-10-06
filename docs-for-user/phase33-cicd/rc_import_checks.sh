#!/usr/bin/env bash
# Import, lazy-startup and isolated-worker checks.
#
# These mirror the repository's own Import Check job (tests.yml): importing an
# entry point must not drag tool implementations into the process, and the
# isolated worker protocol must actually perform work in a child process. They
# are release gates because a wheel that imports but cannot run a tool is not a
# releasable artifact, and the failure would otherwise only appear in production.
set -euo pipefail

python -c "from deeptutor.runtime.orchestrator import ChatOrchestrator; print('Orchestrator imports OK')"
python -c "from deeptutor.runtime.registry.tool_registry import get_tool_registry; print('Tool registry imports OK')"
python -c "from deeptutor.runtime.registry.capability_registry import get_capability_registry; print('Capability registry imports OK')"
python -c "from deeptutor.services.config.runtime_settings import RuntimeSettingsService; print('RuntimeSettingsService imports OK')"
python -c "from deeptutor.api.routers.unified_ws import unified_websocket; print('Unified WS imports OK')"
python -c "import sys; import deeptutor.runtime.registry.tool_registry; assert 'deeptutor.agents.chat.agentic_pipeline' not in sys.modules; print('Tool implementations stay cold')"
python -c "from deeptutor.runtime.isolated_worker import run_in_isolated_process_sync; assert run_in_isolated_process_sync('operator:add', 20, 22, timeout=15) == 42; print('Isolated worker protocol OK')"
