from __future__ import annotations

from enum import Enum

from app.adapters.okx_demo.write_semantics import OkxDemoWriteBlocked


class WriteState(str, Enum):
    PREPARED = "PREPARED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    REJECTED = "REJECTED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    RESIDUAL_CLOSE_REQUIRED = "RESIDUAL_CLOSE_REQUIRED"
    RECONCILED = "RECONCILED"


class WriteEvent(str, Enum):
    ACKNOWLEDGE = "ACKNOWLEDGE"
    EXPLICIT_REJECTION = "EXPLICIT_REJECTION"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    RECOVERY_STILL_UNKNOWN = "RECOVERY_STILL_UNKNOWN"
    RESIDUAL_DETECTED = "RESIDUAL_DETECTED"
    RECONCILE = "RECONCILE"


TRANSITIONS = {
    (WriteState.PREPARED, WriteEvent.ACKNOWLEDGE): WriteState.ACKNOWLEDGED,
    (WriteState.PREPARED, WriteEvent.EXPLICIT_REJECTION): WriteState.REJECTED,
    (WriteState.PREPARED, WriteEvent.OUTCOME_UNKNOWN): WriteState.RECOVERY_REQUIRED,
    (WriteState.ACKNOWLEDGED, WriteEvent.RECONCILE): WriteState.RECONCILED,
    (WriteState.ACKNOWLEDGED, WriteEvent.OUTCOME_UNKNOWN): WriteState.RECOVERY_REQUIRED,
    (
        WriteState.ACKNOWLEDGED,
        WriteEvent.RESIDUAL_DETECTED,
    ): WriteState.RESIDUAL_CLOSE_REQUIRED,
    (
        WriteState.RECOVERY_REQUIRED,
        WriteEvent.RECOVERY_STILL_UNKNOWN,
    ): WriteState.RECOVERY_REQUIRED,
    (WriteState.RECOVERY_REQUIRED, WriteEvent.RECONCILE): WriteState.RECONCILED,
    (
        WriteState.RECOVERY_REQUIRED,
        WriteEvent.RESIDUAL_DETECTED,
    ): WriteState.RESIDUAL_CLOSE_REQUIRED,
    (
        WriteState.RESIDUAL_CLOSE_REQUIRED,
        WriteEvent.RECOVERY_STILL_UNKNOWN,
    ): WriteState.RESIDUAL_CLOSE_REQUIRED,
    (
        WriteState.RESIDUAL_CLOSE_REQUIRED,
        WriteEvent.RESIDUAL_DETECTED,
    ): WriteState.RESIDUAL_CLOSE_REQUIRED,
    (
        WriteState.RESIDUAL_CLOSE_REQUIRED,
        WriteEvent.RECONCILE,
    ): WriteState.RECONCILED,
}


def transition_write_state(current: WriteState, event: WriteEvent) -> WriteState:
    try:
        return TRANSITIONS[(current, event)]
    except KeyError:
        raise OkxDemoWriteBlocked(
            "invalid OKX writer state transition {} + {}".format(
                current.value,
                event.value,
            )
        ) from None
