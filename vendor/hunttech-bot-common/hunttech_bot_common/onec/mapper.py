"""Mapping utilities between internal formats and 1C OData entities.

Provides bidirectional mapping for catalogs, documents, and other entities
between the bot's internal representation and 1C OData format.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from hunttech_bot_common.onec.models import (
    Counterparty,
    Organization,
    Warehouse,
    Nomenclature,
    UnitOfMeasure,
    Currency,
    VatRate,
    Contract,
    GoodsReceipt,
    SalesInvoice,
    AdvanceReport,
    DocumentItem,
    OneCRef,
)


# ── Counterparty mapping ────────────────────────────────────────

def counterparty_to_onec(data: dict[str, Any]) -> dict[str, Any]:
    """Map internal counterparty data to 1C OData format.

    Expected input keys:
        - name (required): Counterparty name
        - inn: Tax ID (INN)
        - kpp: Tax registration code (KPP)
        - okpo: OKPO code
        - oktmo: OKTMO code
        - phone: Phone number
        - email: Email address
        - address: Postal address
        - legal_address: Legal address
        - bank_account: Bank account number
        - bank_name: Bank name
        - bik: Bank identifier code (BIC)
        - correspondent_account: Correspondent account
        - contact_person: Contact person name
        - is_our_company: Whether this is our own company
        - parent_key: Parent group key (for folders)
        - is_folder: Whether this is a group/folder
        - code: Code
        - description: Description (defaults to name)

    Returns:
        Dict ready for 1C OData POST/PATCH.
    """
    result = {
        "Description": data.get("description") or data.get("name", ""),
        "Code": data.get("code", ""),
        "IsFolder": data.get("is_folder", False),
        "DeletionMark": data.get("deletion_mark", False),
    }

    if parent_key := data.get("parent_key"):
        result["Parent_Key"] = parent_key

    # Counterparty-specific fields
    field_map = {
        "inn": "INN",
        "kpp": "KPP",
        "okpo": "OKPO",
        "oktmo": "OKTMO",
        "phone": "Phone",
        "email": "Email",
        "address": "Address",
        "legal_address": "LegalAddress",
        "bank_account": "BankAccount",
        "bank_name": "BankName",
        "bik": "BIK",
        "correspondent_account": "CorrespondentAccount",
        "contact_person": "ContactPerson",
        "is_our_company": "IsOurCompany",
    }

    for src, dst in field_map.items():
        if value := data.get(src):
            result[dst] = value

    # Normalize INN/KPP
    if "INN" in result and result["INN"]:
        result["INN"] = "".join(filter(str.isdigit, str(result["INN"])))
    if "KPP" in result and result["KPP"]:
        result["KPP"] = "".join(filter(str.isdigit, str(result["KPP"])))

    return result


def counterparty_from_onec(onec_data: dict[str, Any]) -> dict[str, Any]:
    """Map 1C OData counterparty to internal format."""
    return {
        "key": onec_data.get("Ref_Key"),
        "name": onec_data.get("Description"),
        "code": onec_data.get("Code"),
        "is_folder": onec_data.get("IsFolder", False),
        "deletion_mark": onec_data.get("DeletionMark", False),
        "parent_key": onec_data.get("Parent_Key"),
        "data_version": onec_data.get("DataVersion"),
        "inn": onec_data.get("INN"),
        "kpp": onec_data.get("KPP"),
        "okpo": onec_data.get("OKPO"),
        "oktmo": onec_data.get("OKTMO"),
        "phone": onec_data.get("Phone"),
        "email": onec_data.get("Email"),
        "address": onec_data.get("Address"),
        "legal_address": onec_data.get("LegalAddress"),
        "bank_account": onec_data.get("BankAccount"),
        "bank_name": onec_data.get("BankName"),
        "bik": onec_data.get("BIK"),
        "correspondent_account": onec_data.get("CorrespondentAccount"),
        "contact_person": onec_data.get("ContactPerson"),
        "is_our_company": onec_data.get("IsOurCompany", False),
    }


# ── Organization mapping ────────────────────────────────────────

def organization_to_onec(data: dict[str, Any]) -> dict[str, Any]:
    """Map internal organization data to 1C OData format."""
    result = {
        "Description": data.get("description") or data.get("name", ""),
        "Code": data.get("code", ""),
        "IsFolder": data.get("is_folder", False),
        "DeletionMark": data.get("deletion_mark", False),
    }

    if parent_key := data.get("parent_key"):
        result["Parent_Key"] = parent_key

    field_map = {
        "inn": "INN",
        "kpp": "KPP",
        "okpo": "OKPO",
        "oktmo": "OKTMO",
        "full_name": "FullName",
        "legal_address": "LegalAddress",
        "actual_address": "ActualAddress",
        "phone": "Phone",
        "email": "Email",
        "director": "Director",
        "accountant": "Accountant",
        "bank_account": "BankAccount",
        "bank_name": "BankName",
        "bik": "BIK",
        "correspondent_account": "CorrespondentAccount",
        "is_default": "IsDefault",
    }

    for src, dst in field_map.items():
        if value := data.get(src):
            result[dst] = value

    if "INN" in result and result["INN"]:
        result["INN"] = "".join(filter(str.isdigit, str(result["INN"])))

    return result


def organization_from_onec(onec_data: dict[str, Any]) -> dict[str, Any]:
    """Map 1C OData organization to internal format."""
    return {
        "key": onec_data.get("Ref_Key"),
        "name": onec_data.get("Description"),
        "code": onec_data.get("Code"),
        "is_folder": onec_data.get("IsFolder", False),
        "deletion_mark": onec_data.get("DeletionMark", False),
        "parent_key": onec_data.get("Parent_Key"),
        "data_version": onec_data.get("DataVersion"),
        "inn": onec_data.get("INN"),
        "kpp": onec_data.get("KPP"),
        "okpo": onec_data.get("OKPO"),
        "oktmo": onec_data.get("OKTMO"),
        "full_name": onec_data.get("FullName"),
        "legal_address": onec_data.get("LegalAddress"),
        "actual_address": onec_data.get("ActualAddress"),
        "phone": onec_data.get("Phone"),
        "email": onec_data.get("Email"),
        "director": onec_data.get("Director"),
        "accountant": onec_data.get("Accountant"),
        "bank_account": onec_data.get("BankAccount"),
        "bank_name": onec_data.get("BankName"),
        "bik": onec_data.get("BIK"),
        "correspondent_account": onec_data.get("CorrespondentAccount"),
        "is_default": onec_data.get("IsDefault", False),
    }


# ── Warehouse mapping ───────────────────────────────────────────

def warehouse_to_onec(data: dict[str, Any]) -> dict[str, Any]:
    """Map internal warehouse data to 1C OData format."""
    result = {
        "Description": data.get("description") or data.get("name", ""),
        "Code": data.get("code", ""),
        "IsFolder": data.get("is_folder", False),
        "DeletionMark": data.get("deletion_mark", False),
    }

    if parent_key := data.get("parent_key"):
        result["Parent_Key"] = parent_key

    field_map = {
        "address": "Address",
        "responsible_person_key": "ResponsiblePerson_Key",
        "organization_key": "Organization_Key",
        "is_retail": "IsRetail",
    }

    for src, dst in field_map.items():
        if value := data.get(src):
            result[dst] = value

    return result


def warehouse_from_onec(onec_data: dict[str, Any]) -> dict[str, Any]:
    """Map 1C OData warehouse to internal format."""
    return {
        "key": onec_data.get("Ref_Key"),
        "name": onec_data.get("Description"),
        "code": onec_data.get("Code"),
        "is_folder": onec_data.get("IsFolder", False),
        "deletion_mark": onec_data.get("DeletionMark", False),
        "parent_key": onec_data.get("Parent_Key"),
        "data_version": onec_data.get("DataVersion"),
        "address": onec_data.get("Address"),
        "responsible_person_key": onec_data.get("ResponsiblePerson_Key"),
        "organization_key": onec_data.get("Organization_Key"),
        "is_retail": onec_data.get("IsRetail", False),
    }


# ── Nomenclature mapping ────────────────────────────────────────

def nomenclature_to_onec(data: dict[str, Any]) -> dict[str, Any]:
    """Map internal nomenclature data to 1C OData format."""
    result = {
        "Description": data.get("description") or data.get("name", ""),
        "Code": data.get("code", "") or data.get("article", ""),
        "IsFolder": data.get("is_folder", False),
        "DeletionMark": data.get("deletion_mark", False),
    }

    if parent_key := data.get("parent_key") or data.get("group_key"):
        result["Parent_Key"] = parent_key

    field_map = {
        "article": "Article",
        "full_name": "FullName",
        "unit_key": "Unit_Key",
        "vat_rate": "VATRate",
        "group_key": "Group_Key",
        "is_service": "IsService",
        "default_price": "DefaultPrice",
        "default_currency_key": "DefaultCurrency_Key",
    }

    for src, dst in field_map.items():
        if value := data.get(src):
            if src == "vat_rate" and isinstance(value, (int, float, Decimal)):
                result[dst] = Decimal(str(value))
            else:
                result[dst] = value

    return result


def nomenclature_from_onec(onec_data: dict[str, Any]) -> dict[str, Any]:
    """Map 1C OData nomenclature to internal format."""
    return {
        "key": onec_data.get("Ref_Key"),
        "name": onec_data.get("Description"),
        "code": onec_data.get("Code"),
        "article": onec_data.get("Article"),
        "full_name": onec_data.get("FullName"),
        "is_folder": onec_data.get("IsFolder", False),
        "deletion_mark": onec_data.get("DeletionMark", False),
        "parent_key": onec_data.get("Parent_Key") or onec_data.get("Group_Key"),
        "data_version": onec_data.get("DataVersion"),
        "unit_key": onec_data.get("Unit_Key"),
        "unit_name": onec_data.get("Unit"),
        "vat_rate": onec_data.get("VATRate"),
        "group_key": onec_data.get("Group_Key"),
        "is_service": onec_data.get("IsService", False),
        "default_price": onec_data.get("DefaultPrice"),
        "default_currency_key": onec_data.get("DefaultCurrency_Key"),
    }


# ── Unit of Measure mapping ─────────────────────────────────────

def unit_to_onec(data: dict[str, Any]) -> dict[str, Any]:
    """Map internal unit data to 1C OData format."""
    result = {
        "Description": data.get("description") or data.get("name", ""),
        "Code": data.get("code", ""),
        "DeletionMark": data.get("deletion_mark", False),
    }

    if parent_key := data.get("parent_key"):
        result["Parent_Key"] = parent_key

    if value := data.get("international_code"):
        result["InternationalCode"] = value

    return result


def unit_from_onec(onec_data: dict[str, Any]) -> dict[str, Any]:
    """Map 1C OData unit to internal format."""
    return {
        "key": onec_data.get("Ref_Key"),
        "name": onec_data.get("Description"),
        "code": onec_data.get("Code"),
        "deletion_mark": onec_data.get("DeletionMark", False),
        "parent_key": onec_data.get("Parent_Key"),
        "data_version": onec_data.get("DataVersion"),
        "international_code": onec_data.get("InternationalCode"),
    }


# ── Currency mapping ────────────────────────────────────────────

def currency_to_onec(data: dict[str, Any]) -> dict[str, Any]:
    """Map internal currency data to 1C OData format."""
    result = {
        "Description": data.get("description") or data.get("name", ""),
        "Code": data.get("code", ""),
        "DeletionMark": data.get("deletion_mark", False),
    }

    if parent_key := data.get("parent_key"):
        result["Parent_Key"] = parent_key

    field_map = {
        "iso_code": "ISOCode",
        "rate": "Rate",
        "multi_rate": "MultiRate",
    }

    for src, dst in field_map.items():
        if value := data.get(src):
            if src == "rate" and isinstance(value, (int, float, Decimal)):
                result[dst] = Decimal(str(value))
            else:
                result[dst] = value

    return result


def currency_from_onec(onec_data: dict[str, Any]) -> dict[str, Any]:
    """Map 1C OData currency to internal format."""
    return {
        "key": onec_data.get("Ref_Key"),
        "name": onec_data.get("Description"),
        "code": onec_data.get("Code"),
        "deletion_mark": onec_data.get("DeletionMark", False),
        "parent_key": onec_data.get("Parent_Key"),
        "data_version": onec_data.get("DataVersion"),
        "iso_code": onec_data.get("ISOCode"),
        "rate": onec_data.get("Rate"),
        "multi_rate": onec_data.get("MultiRate", False),
    }


# ── VAT Rate mapping ────────────────────────────────────────────

def vat_rate_to_onec(data: dict[str, Any]) -> dict[str, Any]:
    """Map internal VAT rate data to 1C OData format."""
    result = {
        "Description": data.get("description") or data.get("name", ""),
        "Code": data.get("code", ""),
        "DeletionMark": data.get("deletion_mark", False),
        "Rate": Decimal(str(data.get("rate", 0))),
    }

    if parent_key := data.get("parent_key"):
        result["Parent_Key"] = parent_key

    return result


def vat_rate_from_onec(onec_data: dict[str, Any]) -> dict[str, Any]:
    """Map 1C OData VAT rate to internal format."""
    return {
        "key": onec_data.get("Ref_Key"),
        "name": onec_data.get("Description"),
        "code": onec_data.get("Code"),
        "deletion_mark": onec_data.get("DeletionMark", False),
        "parent_key": onec_data.get("Parent_Key"),
        "data_version": onec_data.get("DataVersion"),
        "rate": onec_data.get("Rate"),
    }


# ── Contract mapping ────────────────────────────────────────────

def contract_to_onec(data: dict[str, Any]) -> dict[str, Any]:
    """Map internal contract data to 1C OData format."""
    result = {
        "Description": data.get("description") or data.get("name", ""),
        "Code": data.get("code", ""),
        "IsFolder": data.get("is_folder", False),
        "DeletionMark": data.get("deletion_mark", False),
    }

    if parent_key := data.get("parent_key"):
        result["Parent_Key"] = parent_key

    field_map = {
        "counterparty_key": "Counterparty_Key",
        "organization_key": "Organization_Key",
        "contract_type": "ContractType",
        "currency_key": "Currency_Key",
        "start_date": "StartDate",
        "end_date": "EndDate",
        "responsible_person_key": "ResponsiblePerson_Key",
        "is_active": "IsActive",
    }

    for src, dst in field_map.items():
        if value := data.get(src):
            if src in ("start_date", "end_date") and isinstance(value, (date, datetime)):
                result[dst] = value.isoformat() if isinstance(value, date) else value
            else:
                result[dst] = value

    return result


def contract_from_onec(onec_data: dict[str, Any]) -> dict[str, Any]:
    """Map 1C OData contract to internal format."""
    return {
        "key": onec_data.get("Ref_Key"),
        "name": onec_data.get("Description"),
        "code": onec_data.get("Code"),
        "is_folder": onec_data.get("IsFolder", False),
        "deletion_mark": onec_data.get("DeletionMark", False),
        "parent_key": onec_data.get("Parent_Key"),
        "data_version": onec_data.get("DataVersion"),
        "counterparty_key": onec_data.get("Counterparty_Key"),
        "organization_key": onec_data.get("Organization_Key"),
        "contract_type": onec_data.get("ContractType"),
        "currency_key": onec_data.get("Currency_Key"),
        "start_date": onec_data.get("StartDate"),
        "end_date": onec_data.get("EndDate"),
        "responsible_person_key": onec_data.get("ResponsiblePerson_Key"),
        "is_active": onec_data.get("IsActive", True),
    }


# ── Document item mapping ───────────────────────────────────────

def document_item_to_onec(data: dict[str, Any]) -> dict[str, Any]:
    """Map internal document item to 1C OData format."""
    result = {}

    field_map = {
        "nomenclature_key": "Nomenclature_Key",
        "nomenclature_description": "Nomenclature",
        "unit_key": "Unit_Key",
        "unit_name": "Unit",
        "quantity": "Quantity",
        "price": "Price",
        "amount": "Amount",
        "vat_rate": "VATRate",
        "vat_amount": "VATAmount",
        "total_amount": "TotalAmount",
        "warehouse_key": "Warehouse_Key",
        "account_key": "Account_Key",
        "vat_account_key": "VATAccount_Key",
    }

    for src, dst in field_map.items():
        if value := data.get(src):
            if src in ("quantity", "price", "amount", "vat_rate", "vat_amount", "total_amount"):
                if isinstance(value, (int, float, Decimal)):
                    result[dst] = Decimal(str(value))
                else:
                    result[dst] = value
            else:
                result[dst] = value

    return result


def document_item_from_onec(onec_data: dict[str, Any]) -> dict[str, Any]:
    """Map 1C OData document item to internal format."""
    return {
        "nomenclature_key": onec_data.get("Nomenclature_Key"),
        "nomenclature_description": onec_data.get("Nomenclature"),
        "unit_key": onec_data.get("Unit_Key"),
        "unit_name": onec_data.get("Unit"),
        "quantity": onec_data.get("Quantity"),
        "price": onec_data.get("Price"),
        "amount": onec_data.get("Amount"),
        "vat_rate": onec_data.get("VATRate"),
        "vat_amount": onec_data.get("VATAmount"),
        "total_amount": onec_data.get("TotalAmount"),
        "warehouse_key": onec_data.get("Warehouse_Key"),
        "account_key": onec_data.get("Account_Key"),
        "vat_account_key": onec_data.get("VATAccount_Key"),
    }


# ── Document mapping (generic) ──────────────────────────────────

def document_header_to_onec(
    document_type: str,
    data: dict[str, Any],
    *,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map internal document header to 1C OData format.

    Args:
        document_type: 1C document type name (e.g., "GoodsReceipt", "SalesInvoice")
        data: Document header data
        settings: Optional settings for defaults (currency, warehouse, etc.)

    Returns:
        Dict ready for 1C OData POST.
    """
    settings = settings or {}
    result = {
        "Number": data.get("number", ""),
        "Date": _format_date(data.get("date")),
        "Posted": data.get("posted", False),
        "DeletionMark": data.get("deletion_mark", False),
        "Comment": data.get("comment", ""),
    }

    if author_key := data.get("author_key"):
        result["Author_Key"] = author_key

    # Common document fields
    common_fields = {
        "counterparty_key": "Counterparty_Key",
        "contract_key": "Contract_Key",
        "warehouse_key": "Warehouse_Key",
        "organization_key": "Organization_Key",
        "currency_key": "Currency_Key",
        "exchange_rate": "ExchangeRate",
        "total_amount": "TotalAmount",
        "vat_amount": "VATAmount",
    }

    for src, dst in common_fields.items():
        if value := data.get(src):
            if src == "exchange_rate" and isinstance(value, (int, float, Decimal)):
                result[dst] = Decimal(str(value))
            elif src in ("total_amount", "vat_amount") and isinstance(value, (int, float, Decimal)):
                result[dst] = Decimal(str(value))
            else:
                result[dst] = value

    # Apply defaults from settings
    if not result.get("Currency_Key") and settings.get("default_currency_key"):
        result["Currency_Key"] = settings["default_currency_key"]
    if not result.get("ExchangeRate"):
        result["ExchangeRate"] = Decimal("1")
    if not result.get("Warehouse_Key") and settings.get("default_warehouse_key"):
        result["Warehouse_Key"] = settings["default_warehouse_key"]
    if not result.get("Organization_Key") and settings.get("default_organization_key"):
        result["Organization_Key"] = settings["default_organization_key"]

    # Document-specific fields
    if document_type == "SalesInvoice":
        if value := data.get("payment_due_date"):
            result["PaymentDueDate"] = _format_date(value)
    elif document_type == "AdvanceReport":
        specific_fields = {
            "employee_key": "Employee_Key",
            "accountable_amount": "AccountableAmount",
        }
        for src, dst in specific_fields.items():
            if value := data.get(src):
                if src == "accountable_amount" and isinstance(value, (int, float, Decimal)):
                    result[dst] = Decimal(str(value))
                else:
                    result[dst] = value

    return result


def document_items_to_onec(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map list of internal document items to 1C OData format."""
    return [document_item_to_onec(item) for item in items]


def document_from_onec(onec_data: dict[str, Any]) -> dict[str, Any]:
    """Map 1C OData document to internal format (header only)."""
    items = []
    for item_data in onec_data.get("Items", []):
        items.append(document_item_from_onec(item_data))

    result = {
        "key": onec_data.get("Ref_Key"),
        "number": onec_data.get("Number"),
        "date": onec_data.get("Date"),
        "posted": onec_data.get("Posted", False),
        "deletion_mark": onec_data.get("DeletionMark", False),
        "author_key": onec_data.get("Author_Key"),
        "comment": onec_data.get("Comment"),
        "data_version": onec_data.get("DataVersion"),
        "items": items,
    }

    # Common fields
    common_fields = {
        "counterparty_key": "Counterparty_Key",
        "contract_key": "Contract_Key",
        "warehouse_key": "Warehouse_Key",
        "organization_key": "Organization_Key",
        "currency_key": "Currency_Key",
        "exchange_rate": "ExchangeRate",
        "total_amount": "TotalAmount",
        "vat_amount": "VATAmount",
    }

    for src, dst in common_fields.items():
        result[src] = onec_data.get(dst)

    # Document-specific fields
    if "PaymentDueDate" in onec_data:
        result["payment_due_date"] = onec_data.get("PaymentDueDate")
    if "Employee_Key" in onec_data:
        result["employee_key"] = onec_data.get("Employee_Key")
    if "AccountableAmount" in onec_data:
        result["accountable_amount"] = onec_data.get("AccountableAmount")

    return result


# ── Generic reference helpers ───────────────────────────────────

def make_ref(key: str, description: str | None = None) -> OneCRef:
    """Create a 1C reference object."""
    return OneCRef(Ref_Key=key, Description=description)


def ref_to_key(ref: OneCRef | dict[str, Any] | str | None) -> str | None:
    """Extract key from various reference formats."""
    if ref is None:
        return None
    if isinstance(ref, str):
        return ref
    if isinstance(ref, OneCRef):
        return ref.ref_key
    if isinstance(ref, dict):
        return ref.get("Ref_Key") or ref.get("ref_key") or ref.get("key")
    return None


def _format_date(value: date | datetime | str | None) -> str | None:
    """Format date for 1C OData (YYYY-MM-DDTHH:MM:SS)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat() + "T00:00:00"
    if isinstance(value, str):
        # Try to parse and reformat
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.isoformat()
        except ValueError:
            return value
    return None


# ── Catalog mapper registry ─────────────────────────────────────

CATALOG_MAPPERS: dict[str, dict[str, Any]] = {
    "Counterparties": {
        "to_onec": counterparty_to_onec,
        "from_onec": counterparty_from_onec,
        "model": Counterparty,
    },
    "Organizations": {
        "to_onec": organization_to_onec,
        "from_onec": organization_from_onec,
        "model": Organization,
    },
    "Warehouses": {
        "to_onec": warehouse_to_onec,
        "from_onec": warehouse_from_onec,
        "model": Warehouse,
    },
    "Nomenclature": {
        "to_onec": nomenclature_to_onec,
        "from_onec": nomenclature_from_onec,
        "model": Nomenclature,
    },
    "UnitsOfMeasure": {
        "to_onec": unit_to_onec,
        "from_onec": unit_from_onec,
        "model": UnitOfMeasure,
    },
    "Currencies": {
        "to_onec": currency_to_onec,
        "from_onec": currency_from_onec,
        "model": Currency,
    },
    "VATRates": {
        "to_onec": vat_rate_to_onec,
        "from_onec": vat_rate_from_onec,
        "model": VatRate,
    },
    "Contracts": {
        "to_onec": contract_to_onec,
        "from_onec": contract_from_onec,
        "model": Contract,
    },
}


def get_catalog_mapper(catalog_name: str) -> dict[str, Any] | None:
    """Get mapper functions for a catalog by name."""
    return CATALOG_MAPPERS.get(catalog_name)


def map_to_onec(catalog_name: str, data: dict[str, Any]) -> dict[str, Any]:
    """Map internal data to 1C format using registered mapper."""
    mapper = get_catalog_mapper(catalog_name)
    if mapper and "to_onec" in mapper:
        return mapper["to_onec"](data)
    # Fallback: generic mapping
    return data


def map_from_onec(catalog_name: str, data: dict[str, Any]) -> dict[str, Any]:
    """Map 1C OData to internal format using registered mapper."""
    mapper = get_catalog_mapper(catalog_name)
    if mapper and "from_onec" in mapper:
        return mapper["from_onec"](data)
    # Fallback: return as-is
    return data


__all__ = [
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
]