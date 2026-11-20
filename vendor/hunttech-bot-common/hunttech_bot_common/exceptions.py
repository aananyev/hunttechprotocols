"""Exception hierarchy for hunttech-bot-common."""

from __future__ import annotations


class CommonLibraryError(Exception):
    """Base exception for all library errors."""

    def __init__(self, message: str = "", *args, **kwargs) -> None:
        self.message = message
        super().__init__(message, *args, **kwargs)


class ConfigurationError(CommonLibraryError):
    """Configuration-related error (missing env vars, invalid settings)."""
    pass


class AIError(CommonLibraryError):
    """Base exception for AI client errors."""
    pass


class AIConnectionError(AIError):
    """Failed to connect to the AI provider."""
    pass


class AIAuthenticationError(AIError):
    """Authentication failed (invalid API key)."""
    pass


class AIRateLimitError(AIError):
    """Rate limited by the AI provider."""
    pass


class AITimeoutError(AIError):
    """AI request timed out."""
    pass


class AIInvalidResponseError(AIError):
    """AI returned an unexpected or malformed response."""
    pass


class AISchemaValidationError(AIError):
    """AI response failed schema validation."""
    pass


class DatabaseError(CommonLibraryError):
    """Database operation error."""
    pass


class FileValidationError(CommonLibraryError):
    """File validation error (bad extension, too large, etc.)."""
    pass


class PermissionDeniedError(CommonLibraryError):
    """Permission denied for an operation."""
    pass


class TelegramIntegrationError(CommonLibraryError):
    """Telegram integration error."""
    pass


class OperationConflictError(CommonLibraryError):
    """Operation conflict (e.g., duplicate resource)."""
    pass
