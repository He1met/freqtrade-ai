class FreqtradeDataDownloader:
    """Adapter for future `freqtrade download-data` orchestration."""

    def download(self) -> None:
        raise NotImplementedError("Phase 0 does not download market data.")
