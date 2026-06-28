class FreqtradeRuntimeManager:
    """Coordinates future local Freqtrade runtime processes through adapters."""

    def status(self) -> dict[str, object]:
        return {
            "available": False,
            "phase": "phase_0",
            "message": "Freqtrade runtime management is a future adapter capability.",
        }
