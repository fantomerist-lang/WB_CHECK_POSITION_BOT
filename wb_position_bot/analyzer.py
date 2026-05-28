from __future__ import annotations

from dataclasses import replace
from typing import Protocol

from .models import PositionAnalysis, ProductTarget, SearchResultItem, utc_now_iso


class SearchClient(Protocol):
    def search(self, query: str, page: int = 1) -> list[SearchResultItem]:
        ...


def normalize_name(value: str) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def target_identity_warning(target: ProductTarget) -> str | None:
    if target.nm_id:
        return None
    if target.own_supplier_id:
        return "Карточка ищется по ID продавца, а не по nm_id. Если у магазина несколько карточек в выдаче, позиция может быть не той карточкой."
    if target.own_supplier_name:
        return "Карточка ищется по названию магазина, а не по nm_id. Лучше добавить nm_id карточки."
    return "Не задан nm_id, ID продавца или название магазина. Свою карточку невозможно надежно найти."


def match_target(item: SearchResultItem, target: ProductTarget) -> tuple[bool, str]:
    if target.nm_id and item.nm_id == target.nm_id:
        return True, "nm_id"
    if target.own_supplier_id and item.supplier_id == target.own_supplier_id:
        return True, "supplier_id"
    if target.own_supplier_name:
        left = normalize_name(item.supplier_name)
        right = normalize_name(target.own_supplier_name)
        if left and right and (left == right or right in left or left in right):
            return True, "supplier_name"
    return False, ""


def analyze_target(
    target: ProductTarget,
    client: SearchClient,
    max_pages: int = 10,
    top_limit: int = 5,
) -> PositionAnalysis:
    query = (target.search_query or target.name or str(target.nm_id or "")).strip()
    if not query:
        raise ValueError("У карточки нет поискового запроса.")

    warnings: list[str] = []
    identity_warning = target_identity_warning(target)
    if identity_warning:
        warnings.append(identity_warning)

    top_items: list[SearchResultItem] = []
    own_item: SearchResultItem | None = None
    match_reason = ""
    rank = 0
    seen_nm_ids: set[int] = set()
    pages_checked = 0

    for page in range(1, max(max_pages, 1) + 1):
        products = client.search(query, page=page)
        pages_checked = page
        if not products:
            break

        for product in products:
            if product.nm_id in seen_nm_ids:
                continue
            seen_nm_ids.add(product.nm_id)
            rank += 1
            ranked = replace(product, rank=rank)
            if len(top_items) < top_limit:
                top_items.append(ranked)
            if own_item is None:
                matched, reason = match_target(ranked, target)
                if matched:
                    own_item = ranked
                    match_reason = reason

        if own_item is not None and len(top_items) >= top_limit:
            break

    if own_item is None:
        warnings.append(f"Своя карточка не найдена за {pages_checked} стр. выдачи.")

    return PositionAnalysis(
        target=target,
        query=query,
        checked_at=utc_now_iso(),
        top_items=top_items,
        own_item=own_item,
        own_position=own_item.rank if own_item else None,
        match_reason=match_reason,
        pages_checked=pages_checked,
        warnings=warnings,
    )
