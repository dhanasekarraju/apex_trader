"""Control utilities — authority moved to ICB (services.icb)."""

from services.control.actions import ControlAction
from services.control.system_state import SystemState

__all__ = [
    "ControlAction",
    "ControlDecision",
    "ControlDenied",
    "ControlLayer",
    "SystemState",
    "control_layer",
]


def __getattr__(name: str):
    if name in ("ControlDecision", "ControlDenied", "ControlLayer", "control_layer"):
        from services.control.layer import ControlDecision, ControlDenied, ControlLayer, control_layer

        return {
            "ControlDecision": ControlDecision,
            "ControlDenied": ControlDenied,
            "ControlLayer": ControlLayer,
            "control_layer": control_layer,
        }[name]
    raise AttributeError(name)
