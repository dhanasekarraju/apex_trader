import os

import pytest

os.environ.setdefault("KITE_API_KEY", "test")
os.environ.setdefault("KITE_API_SECRET", "test")
os.environ.setdefault("SECRET_KEY", "test-secret-for-ci")
os.environ.setdefault("API_ACCESS_KEY", "test-api-key-for-ci")
os.environ.setdefault("CHAOS_GATE_ENFORCE", "false")


@pytest.fixture(autouse=True)
def reset_system_state():
    import services.icb.system_state as state_mod
    from services.icb.engine import icb
    from services.icb.system_state import SystemState

    icb._healthy = True
    icb._safe_mode_reason = ""
    state_mod._memory_state = SystemState.ACTIVE.value
    state_mod._memory_reason = ""
    state_mod._memory_kill_latched = False
    yield
    icb._healthy = True
    state_mod._memory_state = SystemState.ACTIVE.value
    state_mod._memory_kill_latched = False
