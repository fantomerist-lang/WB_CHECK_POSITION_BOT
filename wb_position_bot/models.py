from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class ProductTarget:
    id: int | None = None
    owner_chat_id: int = 0
    marketplace: str = "wb"
    external_id: str = ""
    nm_id: int | None = None
    sku: str = ""
    name: str = ""
    search_query: str = ""
    own_supplier_id: int | None = None
    own_supplier_name: str = ""
    note: str = ""
    active: bool = True

    def marketplace_label(self) -> str:
        return "Яндекс Маркет" if self.marketplace == "ym" else "Wildberries"

    def product_id(self) -> str:
        return self.external_id or (str(self.nm_id) if self.nm_id else "")

    def label(self) -> str:
        if self.name:
            return self.name
        if self.marketplace == "ym" and self.external_id:
            return f"Яндекс Маркет {self.external_id}"
        if self.nm_id:
            return f"WB {self.nm_id}"
        return self.search_query


@dataclass(frozen=True)
class SearchResultItem:
    rank: int
    nm_id: int = 0
    external_id: str = ""
    name: str = ""
    brand: str = ""
    supplier_id: int | None = None
    supplier_name: str = ""
    price: float | None = None
    sale_price: float | None = None
    rating: float | None = None
    feedbacks: int | None = None
    url: str = ""
    alternate_ids: tuple[str, ...] = ()

    def identity_key(self) -> str:
        if self.external_id:
            return self.external_id
        if self.nm_id:
            return str(self.nm_id)
        return self.url or self.name

    def seller_label(self) -> str:
        if self.supplier_name:
            return self.supplier_name
        if self.supplier_id:
            return f"ID {self.supplier_id}"
        return "не указан"


@dataclass(frozen=True)
class PositionAnalysis:
    target: ProductTarget
    query: str
    checked_at: str
    top_items: list[SearchResultItem]
    own_item: SearchResultItem | None
    own_position: int | None
    match_reason: str
    pages_checked: int
    warnings: list[str]
    search_scope: str = ""

    def with_target(self, target: ProductTarget) -> "PositionAnalysis":
        return replace(self, target=target)
