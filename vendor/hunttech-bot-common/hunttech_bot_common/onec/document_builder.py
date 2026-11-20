"""1C document builder for HuntTech bots.

Provides utilities for constructing 1C documents (GoodsReceipt, SalesInvoice, AdvanceReport, etc.)
from recognized document data, with validation and mapping.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from hunttech_bot_common.onec.client import OneCClient
from hunttech_bot_common.onec.config import DocumentBuilderSettings
from hunttech_bot_common.onec.mapper import (
    document_header_to_onec,
    document_items_to_onec,
    make_ref,
    ref_to_key,
)
from hunttech_bot_common.onec.models import (
    DocumentBuildResult,
    DocumentItem,
    GoodsReceipt,
    SalesInvoice,
    AdvanceReport,
    OneCRef,
)

logger = logging.getLogger(__name__)


@dataclass
class DocumentBuilder:
    """Build and send 1C documents from structured data.

    Usage:
        builder = DocumentBuilder(client, settings)
        result = await builder.build_goods_receipt(
            counterparty_key="...",
            items=[{"nomenclature_key": "...", "quantity": 10, "price": 100}],
            warehouse_key="...",
            organization_key="...",
        )
    """

    client: OneCClient
    settings: DocumentBuilderSettings
    catalog_cache: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ── Goods Receipt (ПоступлениеТоваровУслуг) ─────────────────

    async def build_goods_receipt(
        self,
        *,
        counterparty_key: str,
        items: list[dict[str, Any]],
        warehouse_key: str,
        organization_key: str,
        contract_key: str | None = None,
        currency_key: str | None = None,
        exchange_rate: Decimal | float | None = None,
        date: date | datetime | str | None = None,
        number: str | None = None,
        comment: str | None = None,
        author_key: str | None = None,
        post: bool = False,
    ) -> DocumentBuildResult:
        """Build and create a Goods Receipt document in 1C.

        Args:
            counterparty_key: Supplier counterparty key
            items: List of line items (nomenclature_key, quantity, price, etc.)
            warehouse_key: Warehouse key
            organization_key: Organization key
            contract_key: Optional contract key
            currency_key: Optional currency key
            exchange_rate: Exchange rate (default 1)
            date: Document date (default today)
            number: Document number (auto-generated if not provided)
            comment: Optional comment
            author_key: Optional author user key
            post: Whether to post the document immediately

        Returns:
            DocumentBuildResult with document reference or errors.
        """
        # Validate items
        validated_items = []
        errors = []
        warnings = []
        total_amount = Decimal("0")
        vat_amount = Decimal("0")

        for i, item in enumerate(items):
            validated_item, item_errors, item_warnings = self._validate_document_item(item, i)
            errors.extend(item_errors)
            warnings.extend(item_warnings)
            if validated_item:
                validated_items.append(validated_item)
                total_amount += validated_item.get("TotalAmount", Decimal("0"))
                vat_amount += validated_item.get("VATAmount", Decimal("0"))

        if errors:
            return DocumentBuildResult(
                success=False,
                errors=errors,
                warnings=warnings,
            )

        # Build document header
        doc_date = date or datetime.now().date()
        doc_data = {
            "number": number or "",
            "date": doc_date,
            "posted": post,
            "counterparty_key": counterparty_key,
            "contract_key": contract_key,
            "warehouse_key": warehouse_key,
            "organization_key": organization_key,
            "currency_key": currency_key or self.settings.default_currency,
            "exchange_rate": Decimal(str(exchange_rate)) if exchange_rate else Decimal("1"),
            "total_amount": total_amount,
            "vat_amount": vat_amount,
            "comment": comment,
            "author_key": author_key,
        }

        header = document_header_to_onec("GoodsReceipt", doc_data, settings={
            "default_currency_key": currency_key or self.settings.default_currency,
            "default_warehouse_key": warehouse_key,
            "default_organization_key": organization_key,
        })

        items_onec = document_items_to_onec(validated_items)
        header["Items"] = items_onec

        # Create document
        try:
            doc_ref = await self.client.create_document("GoodsReceipt", header, post=post)
            return DocumentBuildResult(
                success=True,
                document_ref=doc_ref,
                document_number=doc_ref.description,
                warnings=warnings,
            )
        except Exception as e:
            logger.exception("Failed to create GoodsReceipt")
            return DocumentBuildResult(
                success=False,
                errors=[f"Failed to create document: {e}"],
                warnings=warnings,
            )

    # ── Sales Invoice (СчетНаОплатуПокупателю) ──────────────────

    async def build_sales_invoice(
        self,
        *,
        counterparty_key: str,
        items: list[dict[str, Any]],
        warehouse_key: str,
        organization_key: str,
        contract_key: str | None = None,
        currency_key: str | None = None,
        exchange_rate: Decimal | float | None = None,
        date: date | datetime | str | None = None,
        number: str | None = None,
        payment_due_date: date | datetime | str | None = None,
        comment: str | None = None,
        author_key: str | None = None,
        post: bool = False,
    ) -> DocumentBuildResult:
        """Build and create a Sales Invoice document in 1C."""
        validated_items = []
        errors = []
        warnings = []
        total_amount = Decimal("0")
        vat_amount = Decimal("0")

        for i, item in enumerate(items):
            validated_item, item_errors, item_warnings = self._validate_document_item(item, i)
            errors.extend(item_errors)
            warnings.extend(item_warnings)
            if validated_item:
                validated_items.append(validated_item)
                total_amount += validated_item.get("TotalAmount", Decimal("0"))
                vat_amount += validated_item.get("VATAmount", Decimal("0"))

        if errors:
            return DocumentBuildResult(
                success=False,
                errors=errors,
                warnings=warnings,
            )

        doc_date = date or datetime.now().date()
        doc_data = {
            "number": number or "",
            "date": doc_date,
            "posted": post,
            "counterparty_key": counterparty_key,
            "contract_key": contract_key,
            "warehouse_key": warehouse_key,
            "organization_key": organization_key,
            "currency_key": currency_key or self.settings.default_currency,
            "exchange_rate": Decimal(str(exchange_rate)) if exchange_rate else Decimal("1"),
            "total_amount": total_amount,
            "vat_amount": vat_amount,
            "payment_due_date": payment_due_date,
            "comment": comment,
            "author_key": author_key,
        }

        header = document_header_to_onec("SalesInvoice", doc_data, settings={
            "default_currency_key": currency_key or self.settings.default_currency,
            "default_warehouse_key": warehouse_key,
            "default_organization_key": organization_key,
        })

        items_onec = document_items_to_onec(validated_items)
        header["Items"] = items_onec

        try:
            doc_ref = await self.client.create_document("SalesInvoice", header, post=post)
            return DocumentBuildResult(
                success=True,
                document_ref=doc_ref,
                document_number=doc_ref.description,
                warnings=warnings,
            )
        except Exception as e:
            logger.exception("Failed to create SalesInvoice")
            return DocumentBuildResult(
                success=False,
                errors=[f"Failed to create document: {e}"],
                warnings=warnings,
            )

    # ── Advance Report (АвансовыйОтчет) ─────────────────────────

    async def build_advance_report(
        self,
        *,
        employee_key: str,
        items: list[dict[str, Any]],
        organization_key: str,
        currency_key: str | None = None,
        exchange_rate: Decimal | float | None = None,
        date: date | datetime | str | None = None,
        number: str | None = None,
        accountable_amount: Decimal | float | None = None,
        comment: str | None = None,
        author_key: str | None = None,
        post: bool = False,
    ) -> DocumentBuildResult:
        """Build and create an Advance Report document in 1C."""
        validated_items = []
        errors = []
        warnings = []
        total_amount = Decimal("0")
        vat_amount = Decimal("0")

        for i, item in enumerate(items):
            validated_item, item_errors, item_warnings = self._validate_document_item(item, i)
            errors.extend(item_errors)
            warnings.extend(item_warnings)
            if validated_item:
                validated_items.append(validated_item)
                total_amount += validated_item.get("TotalAmount", Decimal("0"))
                vat_amount += validated_item.get("VATAmount", Decimal("0"))

        if accountable_amount is None:
            accountable_amount = total_amount

        if errors:
            return DocumentBuildResult(
                success=False,
                errors=errors,
                warnings=warnings,
            )

        doc_date = date or datetime.now().date()
        doc_data = {
            "number": number or "",
            "date": doc_date,
            "posted": post,
            "employee_key": employee_key,
            "organization_key": organization_key,
            "currency_key": currency_key or self.settings.default_currency,
            "exchange_rate": Decimal(str(exchange_rate)) if exchange_rate else Decimal("1"),
            "total_amount": total_amount,
            "vat_amount": vat_amount,
            "accountable_amount": Decimal(str(accountable_amount)),
            "comment": comment,
            "author_key": author_key,
        }

        header = document_header_to_onec("AdvanceReport", doc_data, settings={
            "default_currency_key": currency_key or self.settings.default_currency,
            "default_organization_key": organization_key,
        })

        items_onec = document_items_to_onec(validated_items)
        header["Items"] = items_onec

        try:
            doc_ref = await self.client.create_document("AdvanceReport", header, post=post)
            return DocumentBuildResult(
                success=True,
                document_ref=doc_ref,
                document_number=doc_ref.description,
                warnings=warnings,
            )
        except Exception as e:
            logger.exception("Failed to create AdvanceReport")
            return DocumentBuildResult(
                success=False,
                errors=[f"Failed to create document: {e}"],
                warnings=warnings,
            )

    # ── Generic document creation ───────────────────────────────

    async def create_document(
        self,
        document_type: str,
        header_data: dict[str, Any],
        items: list[dict[str, Any]],
        *,
        post: bool = False,
    ) -> DocumentBuildResult:
        """Generic document creation for any 1C document type."""
        validated_items = []
        errors = []
        warnings = []
        total_amount = Decimal("0")
        vat_amount = Decimal("0")

        for i, item in enumerate(items):
            validated_item, item_errors, item_warnings = self._validate_document_item(item, i)
            errors.extend(item_errors)
            warnings.extend(item_warnings)
            if validated_item:
                validated_items.append(validated_item)
                total_amount += validated_item.get("TotalAmount", Decimal("0"))
                vat_amount += validated_item.get("VATAmount", Decimal("0"))

        if errors:
            return DocumentBuildResult(
                success=False,
                errors=errors,
                warnings=warnings,
            )

        header = document_header_to_onec(document_type, header_data)
        header["Items"] = document_items_to_onec(validated_items)
        header["TotalAmount"] = total_amount
        header["VATAmount"] = vat_amount

        try:
            doc_ref = await self.client.create_document(document_type, header, post=post)
            return DocumentBuildResult(
                success=True,
                document_ref=doc_ref,
                document_number=doc_ref.description,
                warnings=warnings,
            )
        except Exception as e:
            logger.exception("Failed to create document %s", document_type)
            return DocumentBuildResult(
                success=False,
                errors=[f"Failed to create document: {e}"],
                warnings=warnings,
            )

    # ── Document posting/unposting ──────────────────────────────

    async def post_document(
        self,
        document_type: str,
        document_key: str,
        *,
        etag: str | None = None,
    ) -> DocumentBuildResult:
        """Post (approve) a document in 1C."""
        try:
            doc_ref = await self.client.post_document(document_type, document_key, etag=etag)
            return DocumentBuildResult(
                success=True,
                document_ref=doc_ref,
                document_number=doc_ref.description,
            )
        except Exception as e:
            logger.exception("Failed to post document %s", document_type)
            return DocumentBuildResult(
                success=False,
                errors=[f"Failed to post document: {e}"],
            )

    async def unpost_document(
        self,
        document_type: str,
        document_key: str,
        *,
        etag: str | None = None,
    ) -> DocumentBuildResult:
        """Unpost (unapprove) a document in 1C."""
        try:
            doc_ref = await self.client.unpost_document(document_type, document_key, etag=etag)
            return DocumentBuildResult(
                success=True,
                document_ref=doc_ref,
                document_number=doc_ref.description,
            )
        except Exception as e:
            logger.exception("Failed to unpost document %s", document_type)
            return DocumentBuildResult(
                success=False,
                errors=[f"Failed to unpost document: {e}"],
            )

    # ── Item validation and helpers ─────────────────────────────

    def _validate_document_item(
        self,
        item: dict[str, Any],
        index: int,
    ) -> tuple[dict[str, Any] | None, list[str], list[str]]:
        """Validate and normalize a document line item.

        Returns:
            Tuple of (validated_item, errors, warnings)
        """
        errors = []
        warnings = []
        validated = {}

        # Required fields
        nomenclature_key = item.get("nomenclature_key") or item.get("Nomenclature_Key")
        if not nomenclature_key:
            errors.append(f"Item {index}: missing nomenclature_key")
            return None, errors, warnings

        quantity = item.get("quantity") or item.get("Quantity")
        if quantity is None:
            errors.append(f"Item {index}: missing quantity")
            return None, errors, warnings

        price = item.get("price") or item.get("Price")
        if price is None:
            errors.append(f"Item {index}: missing price")
            return None, errors, warnings

        try:
            qty = Decimal(str(quantity))
            prc = Decimal(str(price))
            if qty <= 0:
                errors.append(f"Item {index}: quantity must be positive")
            if prc < 0:
                errors.append(f"Item {index}: price cannot be negative")
        except Exception:
            errors.append(f"Item {index}: invalid quantity or price format")
            return None, errors, warnings

        # Calculate amounts
        amount = qty * prc
        vat_rate = item.get("vat_rate") or item.get("VATRate")
        if vat_rate is not None:
            try:
                vat_rate_dec = Decimal(str(vat_rate))
                vat_amt = amount * vat_rate_dec / (Decimal("1") + vat_rate_dec)
            except Exception:
                vat_rate_dec = Decimal(str(self.settings.default_vat_rate))
                vat_amt = amount * vat_rate_dec / (Decimal("1") + vat_rate_dec)
                warnings.append(f"Item {index}: invalid vat_rate, using default")
        else:
            vat_rate_dec = Decimal(str(self.settings.default_vat_rate))
            vat_amt = amount * vat_rate_dec / (Decimal("1") + vat_rate_dec)
            warnings.append(f"Item {index}: vat_rate not specified, using default {self.settings.default_vat_rate}")

        total_amt = amount + vat_amt

        validated = {
            "Nomenclature_Key": nomenclature_key,
            "Nomenclature": item.get("nomenclature_description") or item.get("Nomenclature"),
            "Unit_Key": item.get("unit_key") or item.get("Unit_Key"),
            "Unit": item.get("unit_name") or item.get("Unit"),
            "Quantity": qty,
            "Price": prc,
            "Amount": amount,
            "VATRate": vat_rate_dec,
            "VATAmount": vat_amt,
            "TotalAmount": total_amt,
            "Warehouse_Key": item.get("warehouse_key") or item.get("Warehouse_Key"),
            "Account_Key": item.get("account_key") or item.get("Account_Key"),
            "VATAccount_Key": item.get("vat_account_key") or item.get("VATAccount_Key"),
        }

        return validated, errors, warnings

    # ── Catalog lookup helpers ──────────────────────────────────

    async def find_counterparty_by_inn(self, inn: str) -> OneCRef | None:
        """Find counterparty by INN."""
        if not inn:
            return None
        inn_clean = "".join(filter(str.isdigit, inn))
        try:
            response = await self.client.get_catalog(
                "Counterparties",
                filter_=f"INN eq '{inn_clean}'",
                top=1,
            )
            if response.value:
                item = response.value[0]
                return OneCRef(Ref_Key=item["Ref_Key"], Description=item.get("Description"))
        except Exception as e:
            logger.warning("Failed to find counterparty by INN %s: %s", inn, e)
        return None

    async def find_organization_by_inn(self, inn: str) -> OneCRef | None:
        """Find organization by INN."""
        if not inn:
            return None
        inn_clean = "".join(filter(str.isdigit, inn))
        try:
            response = await self.client.get_catalog(
                "Organizations",
                filter_=f"INN eq '{inn_clean}'",
                top=1,
            )
            if response.value:
                item = response.value[0]
                return OneCRef(Ref_Key=item["Ref_Key"], Description=item.get("Description"))
        except Exception as e:
            logger.warning("Failed to find organization by INN %s: %s", inn, e)
        return None

    async def find_nomenclature_by_article(self, article: str) -> OneCRef | None:
        """Find nomenclature by article/code."""
        if not article:
            return None
        try:
            response = await self.client.get_catalog(
                "Nomenclature",
                filter_=f"Article eq '{article}'",
                top=1,
            )
            if response.value:
                item = response.value[0]
                return OneCRef(Ref_Key=item["Ref_Key"], Description=item.get("Description"))
        except Exception as e:
            logger.warning("Failed to find nomenclature by article %s: %s", article, e)
        return None

    async def find_warehouse_by_name(self, name: str) -> OneCRef | None:
        """Find warehouse by name/description."""
        if not name:
            return None
        try:
            response = await self.client.get_catalog(
                "Warehouses",
                filter_=f"Description eq '{name}'",
                top=1,
            )
            if response.value:
                item = response.value[0]
                return OneCRef(Ref_Key=item["Ref_Key"], Description=item.get("Description"))
        except Exception as e:
            logger.warning("Failed to find warehouse by name %s: %s", name, e)
        return None

    async def get_default_warehouse(self) -> OneCRef | None:
        """Get default warehouse from settings or first available."""
        if self.settings.default_warehouse_ref:
            return make_ref(self.settings.default_warehouse_ref)
        try:
            response = await self.client.get_catalog("Warehouses", filter_="IsFolder eq false", top=1)
            if response.value:
                item = response.value[0]
                return OneCRef(Ref_Key=item["Ref_Key"], Description=item.get("Description"))
        except Exception as e:
            logger.warning("Failed to get default warehouse: %s", e)
        return None

    async def get_default_organization(self) -> OneCRef | None:
        """Get default organization from settings or first available."""
        if self.settings.default_organization_ref:
            return make_ref(self.settings.default_organization_ref)
        try:
            response = await self.client.get_catalog("Organizations", filter_="IsDefault eq true", top=1)
            if not response.value:
                response = await self.client.get_catalog("Organizations", filter_="IsFolder eq false", top=1)
            if response.value:
                item = response.value[0]
                return OneCRef(Ref_Key=item["Ref_Key"], Description=item.get("Description"))
        except Exception as e:
            logger.warning("Failed to get default organization: %s", e)
        return None


def create_document_builder(
    client: OneCClient,
    settings: DocumentBuilderSettings | None = None,
) -> DocumentBuilder:
    """Factory function to create DocumentBuilder."""
    return DocumentBuilder(client=client, settings=settings or DocumentBuilderSettings())


__all__ = [
    "DocumentBuilder",
    "create_document_builder",
]