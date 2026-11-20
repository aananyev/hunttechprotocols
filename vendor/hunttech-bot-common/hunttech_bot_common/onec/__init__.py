"""1C integration module for HuntTech bots.

Provides comprehensive integration with 1C via OData HTTP services:
- Configuration management (config.py)
- Async HTTP client with retries and batch operations (client.py)
- Pydantic models for 1C entities (models.py)
- Bidirectional mapping between internal and 1C formats (mapper.py)
- Catalog synchronization with incremental/full sync (catalog_sync.py)
- Document building for GoodsReceipt, SalesInvoice, AdvanceReport (document_builder.py)
- Telegram admin commands (commands.py)

Usage:
    from hunttech_bot_common.onec import (
        OneCConfig, OneCClient, DocumentBuilder,
        Counterparty, Organization, Nomenclature,
        create_onec_client, create_document_builder,
        create_sync_engine,
    )

    # Load configuration
    config = OneCConfig.from_env()

    # Create client and use it
    async with create_onec_client(config.service) as client:
        response = await client.get_catalog("Counterparties", top=10)
        for item in response.value:
            print(item["Description"])

    # Build documents
    builder = create_document_builder(client)
    result = await builder.build_goods_receipt(...)

    # Sync catalogs
    engine = await create_sync_engine(config, state_dir, storage_dir)
    results = await engine.sync_all()
"""

from __future__ import annotations

from hunttech_bot_common.onec.config import (
    OneCSettings,
    CatalogSyncSettings,
    DocumentBuilderSettings,
    OneCConfig,
)
from hunttech_bot_common.onec.client import (
    OneCClient,
    OneCClientError,
    OneCAuthenticationError,
    OneCAuthorizationError,
    OneCNotFoundError,
    OneCConflictError,
    OneCServerError,
    OneCRateLimitError,
    create_onec_client,
)
from hunttech_bot_common.onec.models import (
    OneCMeta,
    DataState,
    RefType,
    OneCRef,
    OneCCatalogObject,
    Counterparty,
    Organization,
    Warehouse,
    Nomenclature,
    UnitOfMeasure,
    Currency,
    VatRate,
    Contract,
    OneCDocumentObject,
    GoodsReceipt,
    SalesInvoice,
    AdvanceReport,
    DocumentItem,
    ODataResponse,
    ODataError,
    SyncResult,
    SyncState,
    DocumentBuildResult,
    BatchOperationResult,
)
from hunttech_bot_common.onec.mapper import (
    counterparty_to_onec,
    counterparty_from_onec,
    organization_to_onec,
    organization_from_onec,
    warehouse_to_onec,
    warehouse_from_onec,
    nomenclature_to_onec,
    nomenclature_from_onec,
    unit_to_onec,
    unit_from_onec,
    currency_to_onec,
    currency_from_onec,
    vat_rate_to_onec,
    vat_rate_from_onec,
    contract_to_onec,
    contract_from_onec,
    document_item_to_onec,
    document_item_from_onec,
    document_header_to_onec,
    document_items_to_onec,
    document_from_onec,
    make_ref,
    ref_to_key,
    get_catalog_mapper,
    map_to_onec,
    map_from_onec,
    CATALOG_MAPPERS,
)
from hunttech_bot_common.onec.catalog_sync import (
    CatalogSyncEngine,
    LocalStorageSyncEngine,
    create_sync_engine,
)
from hunttech_bot_common.onec.document_builder import (
    DocumentBuilder,
    create_document_builder,
)
from hunttech_bot_common.onec.commands import (
    OneCSetupStates,
    OneCSyncStates,
    cmd_onec_setup,
    get_onec_command_def,
    register_onec_handlers,
)

__all__ = [
    # Config
    "OneCSettings",
    "CatalogSyncSettings",
    "DocumentBuilderSettings",
    "OneCConfig",
    # Client
    "OneCClient",
    "OneCClientError",
    "OneCAuthenticationError",
    "OneCAuthorizationError",
    "OneCNotFoundError",
    "OneCConflictError",
    "OneCServerError",
    "OneCRateLimitError",
    "create_onec_client",
    # Models
    "OneCMeta",
    "DataState",
    "RefType",
    "OneCRef",
    "OneCCatalogObject",
    "Counterparty",
    "Organization",
    "Warehouse",
    "Nomenclature",
    "UnitOfMeasure",
    "Currency",
    "VatRate",
    "Contract",
    "OneCDocumentObject",
    "GoodsReceipt",
    "SalesInvoice",
    "AdvanceReport",
    "DocumentItem",
    "ODataResponse",
    "ODataError",
    "SyncResult",
    "SyncState",
    "DocumentBuildResult",
    "BatchOperationResult",
    # Mapper
    "counterparty_to_onec",
    "counterparty_from_onec",
    "organization_to_onec",
    "organization_from_onec",
    "warehouse_to_onec",
    "warehouse_from_onec",
    "nomenclature_to_onec",
    "nomenclature_from_onec",
    "unit_to_onec",
    "unit_from_onec",
    "currency_to_onec",
    "currency_from_onec",
    "vat_rate_to_onec",
    "vat_rate_from_onec",
    "contract_to_onec",
    "contract_from_onec",
    "document_item_to_onec",
    "document_item_from_onec",
    "document_header_to_onec",
    "document_items_to_onec",
    "document_from_onec",
    "make_ref",
    "ref_to_key",
    "get_catalog_mapper",
    "map_to_onec",
    "map_from_onec",
    "CATALOG_MAPPERS",
    # Catalog Sync
    "CatalogSyncEngine",
    "LocalStorageSyncEngine",
    "create_sync_engine",
    # Document Builder
    "DocumentBuilder",
    "create_document_builder",
    # Commands
    "OneCSetupStates",
    "OneCSyncStates",
    "cmd_onec_setup",
    "get_onec_command_def",
    "register_onec_handlers",
]