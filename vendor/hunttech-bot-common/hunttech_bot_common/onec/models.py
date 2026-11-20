"""Pydantic models for 1C OData entities.

Defines data models for catalogs, documents, and other 1C entities
exposed via OData HTTP services.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OneCMeta(BaseModel):
    """OData metadata wrapper."""

    etag: str | None = Field(default=None, alias="@odata.etag")
    context: str | None = Field(default=None, alias="@odata.context")
    id: str | None = Field(default=None, alias="@odata.id")
    edit_link: str | None = Field(default=None, alias="@odata.editLink")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class DataState(str, Enum):
    """1C object data state."""

    ACTIVE = "Active"
    DELETED = "Deleted"
    MARKED_FOR_DELETION = "MarkedForDeletion"


class RefType(str, Enum):
    """1C reference types."""

    CATALOG = "Catalog"
    DOCUMENT = "Document"
    CHART_OF_CHARACTERISTIC_TYPES = "ChartOfCharacteristicTypes"
    CHART_OF_ACCOUNTS = "ChartOfAccounts"
    ACCUMULATION_REGISTER = "AccumulationRegister"
    INFORMATION_REGISTER = "InformationRegister"
    CONSTANT = "Constant"
    ENUMERATION = "Enumeration"


class OneCRef(BaseModel):
    """1C reference (key) to another object."""

    ref_key: str = Field(alias="Ref_Key")
    description: str | None = Field(default=None, alias="Description")
    deletion_mark: bool | None = Field(default=None, alias="DeletionMark")
    data_version: str | None = Field(default=None, alias="DataVersion")

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    @property
    def key(self) -> str:
        """Get the reference key."""
        return self.ref_key

    def __str__(self) -> str:
        return self.description or self.ref_key

    def __repr__(self) -> str:
        return f"OneCRef(key={self.ref_key!r}, description={self.description!r})"


class OneCCatalogObject(BaseModel):
    """Base model for 1C catalog objects."""

    ref_key: str = Field(alias="Ref_Key")
    description: str
    code: str | None = None
    deletion_mark: bool = Field(default=False, alias="DeletionMark")
    parent_key: str | None = Field(default=None, alias="Parent_Key")
    is_folder: bool = Field(default=False, alias="IsFolder")
    data_version: str | None = Field(default=None, alias="DataVersion")

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    @property
    def key(self) -> str:
        return self.ref_key


class Counterparty(OneCCatalogObject):
    """1C Counterparty (Контрагент) catalog entry."""

    inn: str | None = None
    kpp: str | None = None
    okpo: str | None = None
    oktmo: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    legal_address: str | None = None
    bank_account: str | None = Field(default=None, alias="BankAccount")
    bank_name: str | None = Field(default=None, alias="BankName")
    bik: str | None = None
    correspondent_account: str | None = Field(default=None, alias="CorrespondentAccount")
    contact_person: str | None = Field(default=None, alias="ContactPerson")
    is_our_company: bool = Field(default=False, alias="IsOurCompany")

    @field_validator("inn", mode="before")
    @classmethod
    def _normalize_inn(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return "".join(filter(str.isdigit, v))

    @field_validator("kpp", mode="before")
    @classmethod
    def _normalize_kpp(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return "".join(filter(str.isdigit, v))


class Organization(OneCCatalogObject):
    """1C Organization (Организация) catalog entry."""

    inn: str | None = None
    kpp: str | None = None
    okpo: str | None = None
    oktmo: str | None = None
    full_name: str | None = Field(default=None, alias="FullName")
    legal_address: str | None = Field(default=None, alias="LegalAddress")
    actual_address: str | None = Field(default=None, alias="ActualAddress")
    phone: str | None = None
    email: str | None = None
    director: str | None = None
    accountant: str | None = None
    bank_account: str | None = Field(default=None, alias="BankAccount")
    bank_name: str | None = Field(default=None, alias="BankName")
    bik: str | None = None
    correspondent_account: str | None = Field(default=None, alias="CorrespondentAccount")
    is_default: bool = Field(default=False, alias="IsDefault")

    @field_validator("inn", mode="before")
    @classmethod
    def _normalize_inn(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return "".join(filter(str.isdigit, v))


class Warehouse(OneCCatalogObject):
    """1C Warehouse (Склад) catalog entry."""

    address: str | None = None
    responsible_person_key: str | None = Field(default=None, alias="ResponsiblePerson_Key")
    organization_key: str | None = Field(default=None, alias="Organization_Key")
    is_retail: bool = Field(default=False, alias="IsRetail")


class Nomenclature(OneCCatalogObject):
    """1C Nomenclature (Номенклатура) catalog entry."""

    article: str | None = None
    full_name: str | None = Field(default=None, alias="FullName")
    unit_key: str | None = Field(default=None, alias="Unit_Key")
    unit_name: str | None = Field(default=None, alias="Unit")
    vat_rate: Decimal | None = Field(default=None, alias="VATRate")
    group_key: str | None = Field(default=None, alias="Group_Key")
    is_service: bool = Field(default=False, alias="IsService")
    default_price: Decimal | None = Field(default=None, alias="DefaultPrice")
    default_currency_key: str | None = Field(default=None, alias="DefaultCurrency_Key")


class UnitOfMeasure(OneCCatalogObject):
    """1C Unit of Measure (ЕдиницыИзмерения) catalog entry."""

    code: str | None = None
    international_code: str | None = Field(default=None, alias="InternationalCode")


class Currency(OneCCatalogObject):
    """1C Currency (Валюты) catalog entry."""

    code: str | None = None
    iso_code: str | None = Field(default=None, alias="ISOCode")
    rate: Decimal | None = None
    multi_rate: bool = Field(default=False, alias="MultiRate")


class VatRate(OneCCatalogObject):
    """1C VAT Rate (СтавкиНДС) catalog entry."""

    rate: Decimal
    description: str


class Contract(OneCCatalogObject):
    """1C Contract (ДоговорыКонтрагентов) catalog entry."""

    counterparty_key: str | None = Field(default=None, alias="Counterparty_Key")
    organization_key: str | None = Field(default=None, alias="Organization_Key")
    contract_type: str | None = Field(default=None, alias="ContractType")
    currency_key: str | None = Field(default=None, alias="Currency_Key")
    start_date: datetime | None = Field(default=None, alias="StartDate")
    end_date: datetime | None = Field(default=None, alias="EndDate")
    responsible_person_key: str | None = Field(default=None, alias="ResponsiblePerson_Key")
    is_active: bool = Field(default=True, alias="IsActive")


class OneCDocumentObject(BaseModel):
    """Base model for 1C document objects."""

    ref_key: str = Field(alias="Ref_Key")
    number: str
    date: datetime
    posted: bool = Field(default=False, alias="Posted")
    deletion_mark: bool = Field(default=False, alias="DeletionMark")
    author_key: str | None = Field(default=None, alias="Author_Key")
    comment: str | None = None
    data_version: str | None = Field(default=None, alias="DataVersion")

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    @property
    def key(self) -> str:
        return self.ref_key


class GoodsReceipt(OneCDocumentObject):
    """1C Goods Receipt (ПоступлениеТоваровУслуг) document."""

    counterparty_key: str | None = Field(default=None, alias="Counterparty_Key")
    contract_key: str | None = Field(default=None, alias="Contract_Key")
    warehouse_key: str | None = Field(default=None, alias="Warehouse_Key")
    organization_key: str | None = Field(default=None, alias="Organization_Key")
    currency_key: str | None = Field(default=None, alias="Currency_Key")
    exchange_rate: Decimal = Field(default=Decimal("1"), alias="ExchangeRate")
    total_amount: Decimal = Field(default=Decimal("0"), alias="TotalAmount")
    vat_amount: Decimal = Field(default=Decimal("0"), alias="VATAmount")
    items: list[dict[str, Any]] = Field(default_factory=list, alias="Items")


class SalesInvoice(OneCDocumentObject):
    """1C Sales Invoice (СчетНаОплатуПокупателю) document."""

    counterparty_key: str | None = Field(default=None, alias="Counterparty_Key")
    contract_key: str | None = Field(default=None, alias="Contract_Key")
    warehouse_key: str | None = Field(default=None, alias="Warehouse_Key")
    organization_key: str | None = Field(default=None, alias="Organization_Key")
    currency_key: str | None = Field(default=None, alias="Currency_Key")
    exchange_rate: Decimal = Field(default=Decimal("1"), alias="ExchangeRate")
    total_amount: Decimal = Field(default=Decimal("0"), alias="TotalAmount")
    vat_amount: Decimal = Field(default=Decimal("0"), alias="VATAmount")
    payment_due_date: datetime | None = Field(default=None, alias="PaymentDueDate")
    items: list[dict[str, Any]] = Field(default_factory=list, alias="Items")


class AdvanceReport(OneCDocumentObject):
    """1C Advance Report (АвансовыйОтчет) document."""

    employee_key: str | None = Field(default=None, alias="Employee_Key")
    organization_key: str | None = Field(default=None, alias="Organization_Key")
    currency_key: str | None = Field(default=None, alias="Currency_Key")
    exchange_rate: Decimal = Field(default=Decimal("1"), alias="ExchangeRate")
    total_amount: Decimal = Field(default=Decimal("0"), alias="TotalAmount")
    accountable_amount: Decimal = Field(default=Decimal("0"), alias="AccountableAmount")
    items: list[dict[str, Any]] = Field(default_factory=list, alias="Items")


class DocumentItem(BaseModel):
    """Generic document line item."""

    nomenclature_key: str | None = Field(default=None, alias="Nomenclature_Key")
    nomenclature_description: str | None = Field(default=None, alias="Nomenclature")
    unit_key: str | None = Field(default=None, alias="Unit_Key")
    unit_name: str | None = Field(default=None, alias="Unit")
    quantity: Decimal = Field(default=Decimal("0"), alias="Quantity")
    price: Decimal = Field(default=Decimal("0"), alias="Price")
    amount: Decimal = Field(default=Decimal("0"), alias="Amount")
    vat_rate: Decimal | None = Field(default=None, alias="VATRate")
    vat_amount: Decimal = Field(default=Decimal("0"), alias="VATAmount")
    total_amount: Decimal = Field(default=Decimal("0"), alias="TotalAmount")
    warehouse_key: str | None = Field(default=None, alias="Warehouse_Key")
    account_key: str | None = Field(default=None, alias="Account_Key")
    vat_account_key: str | None = Field(default=None, alias="VATAccount_Key")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class ODataResponse(BaseModel):
    """Generic OData response wrapper."""

    value: list[dict[str, Any]] = Field(default_factory=list)
    next_link: str | None = Field(default=None, alias="@odata.nextLink")
    count: int | None = Field(default=None, alias="@odata.count")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class ODataError(BaseModel):
    """OData error response."""

    code: str
    message: str
    target: str | None = None
    details: list[dict[str, Any]] | None = None
    innererror: dict[str, Any] | None = None

    model_config = ConfigDict(extra="allow")


class SyncResult(BaseModel):
    """Result of a catalog synchronization operation."""

    catalog_name: str
    total_fetched: int = 0
    created: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    errors: list[str] = Field(default_factory=list)
    duration_seconds: float = 0.0
    last_sync_token: str | None = None

    @property
    def success(self) -> bool:
        return len(self.errors) == 0

    @property
    def processed(self) -> int:
        return self.created + self.updated + self.deleted


class SyncState(BaseModel):
    """Persistent sync state for incremental synchronization."""

    catalog_name: str
    last_sync_timestamp: datetime | None = None
    last_sync_token: str | None = None
    last_full_sync: datetime | None = None
    etag: str | None = None

    model_config = ConfigDict(extra="allow")


class DocumentBuildResult(BaseModel):
    """Result of building a 1C document."""

    success: bool
    document_ref: OneCRef | None = None
    document_number: str | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    raw_response: dict[str, Any] | None = None


class BatchOperationResult(BaseModel):
    """Result of a batch OData operation."""

    success: bool
    results: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[ODataError] = Field(default_factory=list)
    changeset_boundary: str | None = None


__all__ = [
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
]