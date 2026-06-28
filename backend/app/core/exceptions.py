class FreqtradeAIError(Exception):
    """Base exception for project-owned backend errors."""


class ConfigurationError(FreqtradeAIError):
    """Raised when YAML or ENV configuration is missing or invalid."""
