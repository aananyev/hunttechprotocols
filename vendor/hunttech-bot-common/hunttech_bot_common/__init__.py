"""HuntTech Bot Common — shared library for HuntTech Telegram bots."""

from __future__ import annotations

__version__ = "0.3.0"

from hunttech_bot_common.exceptions import (
    AIAuthenticationError,
    AIConnectionError,
    AIError,
    AIInvalidResponseError,
    AIRateLimitError,
    AISchemaValidationError,
    AITimeoutError,
    CommonLibraryError,
    ConfigurationError,
    DatabaseError,
    FileValidationError,
    OperationConflictError,
    PermissionDeniedError,
    TelegramIntegrationError,
)

from hunttech_bot_common.users.base import UserRecord, user_from_telegram
from hunttech_bot_common.users.access import AccessManager
from hunttech_bot_common.users.settings import UserSettingsManager
from hunttech_bot_common.database import DatabasePool, PoolConfig, BaseRepository, UnitOfWork, DatabaseMigrator
from hunttech_bot_common.recognition import recognize_document, RecognitionResult, DOCUMENT_SCHEMA, INSTRUCTIONS
from hunttech_bot_common.email import (
    ConnectionTestResult,
    clear_email_config,
    default_email_config,
    format_email_config,
    load_email_config,
    save_email_config,
    test_email_connections,
    test_imap_connection,
    test_smtp_connection,
    validate_email,
    validate_hostname,
    validate_port,
    validate_password,
)
from hunttech_bot_common.onec import (
    OneCConfig,
    OneCClient,
    create_onec_client,
    Counterparty,
    Organization,
    Nomenclature,
    DocumentBuilder,
    create_document_builder,
    create_sync_engine,
    OneCSettings,
    CatalogSyncSettings,
    DocumentBuilderSettings,
)

__all__ = [
    "__version__",
    "CommonLibraryError",
    "ConfigurationError",
    "AIError",
    "AIConnectionError",
    "AIAuthenticationError",
    "AIRateLimitError",
    "AITimeoutError",
    "AIInvalidResponseError",
    "AISchemaValidationError",
    "DatabaseError",
    "FileValidationError",
    "PermissionDeniedError",
    "TelegramIntegrationError",
    "OperationConflictError",
    "UserRecord",
    "user_from_telegram",
    "AccessManager",
    "UserSettingsManager",
    "DatabasePool",
    "PoolConfig",
    "BaseRepository",
    "UnitOfWork",
    "DatabaseMigrator",
    "recognize_document",
    "RecognitionResult",
    "DOCUMENT_SCHEMA",
    "INSTRUCTIONS",
    "ConnectionTestResult",
    "clear_email_config",
    "default_email_config",
    "format_email_config",
    "load_email_config",
    "save_email_config",
    "test_email_connections",
    "test_imap_connection",
    "test_smtp_connection",
    "validate_email",
    "validate_hostname",
    "validate_port",
    "validate_password",
    # 1C integration
    "OneCConfig",
    "OneCClient",
    "create_onec_client",
    "Counterparty",
    "Organization",
    "Nomenclature",
    "DocumentBuilder",
    "create_document_builder",
    "create_sync_engine",
    "OneCSettings",
    "CatalogSyncSettings",
    "DocumentBuilderSettings",
]