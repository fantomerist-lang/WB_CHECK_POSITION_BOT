from __future__ import annotations

import re

from .models import ProductTarget


def parse_add_args(text: str, marketplace: str = "wb") -> ProductTarget:
    payload = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
    parts = [part.strip() for part in payload.split("|")]
    command = "/addym" if marketplace == "ym" else "/add"
    if marketplace == "wb" and len(parts) == 2 and all(parts):
        query, supplier = parts
        return ProductTarget(
            marketplace="wb",
            name=query,
            search_query=query,
            own_supplier_name=supplier,
        )
    if len(parts) < 3 or not all(parts[:3]):
        raise ValueError(f"Формат: {command} ID_КАРТОЧКИ | поисковый запрос | Название магазина")

    raw_id, query, supplier = parts[:3]
    if marketplace == "ym":
        external_id = extract_yandex_product_id(raw_id)
        if not external_id:
            raise ValueError("Не удалось определить ID карточки Яндекс Маркета. Пришли число или ссылку на карточку.")
        return ProductTarget(
            marketplace="ym",
            external_id=external_id,
            sku=external_id,
            name=f"Яндекс Маркет {external_id}",
            search_query=query,
            own_supplier_name=supplier,
        )

    try:
        nm_id = int(raw_id)
    except ValueError as error:
        raise ValueError("Артикул Wildberries должен быть числом.") from error
    return ProductTarget(
        marketplace="wb",
        external_id=str(nm_id),
        nm_id=nm_id,
        sku=str(nm_id),
        name=f"WB {nm_id}",
        search_query=query,
        own_supplier_name=supplier,
    )


def extract_yandex_product_id(value: str) -> str:
    text = str(value or "").strip()
    if text.isdigit():
        return text
    card_match = re.search(r"/(?:card|product--)[^?#]*/(\d+)(?:[/?#]|$)", text)
    if card_match:
        return card_match.group(1)
    matches = re.findall(r"(?<!\d)(\d{6,})(?!\d)", text)
    return matches[-1] if matches else ""
