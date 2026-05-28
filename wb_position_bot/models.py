from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class ProductTarget:
    id: int | None = None
    nm_id: int | None = None
    sku: str = ""
    name: str = ""
    search_query: str = ""
    own_supplier_id: int | None = None
    own_supplier_name: str = ""
    note: str = ""
    active: bool = True

    def label(self) -> str:
        if self.name:
            return self.name
        if self.nm_id:
            return f"WB {self.nm_id}"
        return self.search_query


@dataclass(frozen=True)
class SearchResultItem:
    rank: int
    nm_id: int
    name: str
    brand: str = ""
    supplier_id: int | None = None
    supplier_name: str = ""
    price: float | None = None
    sale_price: float | None = None
    rating: float | None = None
    feedbacks: int | None = None
    url: str = ""

    def seller_label(self) -> str:
        if self.supplier_name:
            return self.supplier_name
        if self.supplier_id:
            return str(self.supplier_id)
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

    def with_target(self, target: ProductTarget) -> "PositionAnalysis":
        return replace(self, target=target)
