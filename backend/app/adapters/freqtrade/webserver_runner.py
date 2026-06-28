class FreqtradeWebserverRunner:
    """Adapter boundary for future Freqtrade webserver shortcuts."""

    def start(self) -> None:
        raise NotImplementedError("Phase 0 does not start Freqtrade webserver.")
