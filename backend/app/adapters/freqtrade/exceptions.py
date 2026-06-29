class FreqtradeAdapterError(Exception):
    """Base exception for Freqtrade adapter failures."""


class FreqtradeCommandError(FreqtradeAdapterError):
    """Raised when a Freqtrade CLI command fails."""


class FreqtradeCommandValidationError(FreqtradeAdapterError):
    """Raised when a Freqtrade CLI command is outside the allowed boundary."""


class FreqtradeConfigError(FreqtradeAdapterError):
    """Raised when a generated Freqtrade config would be unsafe or invalid."""


class FreqtradeResultParseError(FreqtradeAdapterError):
    """Raised when a Freqtrade result file cannot be parsed."""
