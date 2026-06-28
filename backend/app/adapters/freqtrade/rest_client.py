class FreqtradeRestClient:
    """Thin future client for Freqtrade REST API integration."""

    def health(self) -> dict[str, object]:
        raise NotImplementedError("Phase 0 does not call Freqtrade REST API.")
