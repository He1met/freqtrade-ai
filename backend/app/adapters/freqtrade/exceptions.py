class FreqtradeAdapterError(Exception):
    """Base exception for Freqtrade adapter failures."""


class FreqtradeCommandError(FreqtradeAdapterError):
    """Raised when a Freqtrade CLI command fails."""


class FreqtradeResultParseError(FreqtradeAdapterError):
    """Raised when a Freqtrade result file cannot be parsed."""
