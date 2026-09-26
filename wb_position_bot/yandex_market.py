from __future__ import annotations

import html
import http.client
import json
import random
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from typing import Any

from .models import SearchResultItem
from .wildberries import USER_AGENTS, build_opener


class YandexMarketError(RuntimeError):
    pass


APIFY_YANDEX_ENDPOINT = (
    "https://api.apify.com/v2/actors/zen-studio~yandex-market-scraper-parser/"
    "run-sync-get-dataset-items"
)


TRANSIENT_NETWORK_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    socket.timeout,
    ConnectionError,
    http.client.BadStatusLine,
    http.client.IncompleteRead,
)


class YandexMarketClient:
    def __init__(
        self,
        region_id: int = 213,
        timeout: float = 60.0,
        request_delay_seconds: float = 2.0,
        request_delay_jitter_seconds: float = 3.0,
        retries: int = 4,
        proxy_url: str = "",
        proxy_auth_token: str = "",
        proxy_insecure_ssl: bool = False,
        enrich_sellers: bool = True,
        apify_api_token: str = "",
        apify_api_url: str = APIFY_YANDEX_ENDPOINT,
        apify_max_items: int = 50,
        apify_timeout: float = 240.0,
        apify_enrich_products: bool = True,
    ) -> None:
        self.region_id = int(region_id)
        self.timeout = float(timeout)
        self.request_delay_seconds = max(float(request_delay_seconds or 0), 0.0)
        self.request_delay_jitter_seconds = max(float(request_delay_jitter_seconds or 0), 0.0)
        self.retries = max(int(retries or 1), 1)
        self.proxy_url = str(proxy_url or "")
        self.enrich_sellers = bool(enrich_sellers)
        self.apify_api_token = str(apify_api_token or "").strip()
        self.apify_api_url = str(apify_api_url or APIFY_YANDEX_ENDPOINT).strip()
        self.apify_max_items = max(int(apify_max_items or 1), 1)
        self.apify_timeout = max(float(apify_timeout or 1), 1.0)
        self.apify_enrich_products = bool(apify_enrich_products)
        self.opener = build_opener(
            self.proxy_url,
            insecure_ssl=proxy_insecure_ssl,
            proxy_auth_token=proxy_auth_token,
        )
        self.apify_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._last_request_at = 0.0
        self._seller_cache: dict[int, str] = {}
        self._apify_cache: dict[str, list[SearchResultItem]] = {}
        self._operation_deadline: float | None = None

    def start_operation(self, timeout_seconds: float) -> None:
        timeout = max(float(timeout_seconds or 0), 0.01)
        self._operation_deadline = time.monotonic() + timeout

    def finish_operation(self) -> None:
        self._operation_deadline = None

    def search(self, query: str, page: int = 1) -> list[SearchResultItem]:
        if self.apify_api_token:
            return self._search_apify(query, page)
        params = {
            "text": query,
            "lr": str(self.region_id),
        }
        if page > 1:
            params["page"] = str(page)
        url = "https://market.yandex.ru/search?" + urllib.parse.urlencode(params)
        raw = self._get_html(url)
        items = parse_yandex_search_html(raw)
        if page == 1 and self.enrich_sellers:
            items = self._enrich_top_sellers(items, limit=5)
        return items

    def not_found_scope(self, pages_checked: int, items_checked: int) -> str:
        if self.apify_api_token:
            return f"среди первых {items_checked} позиций выдачи"
        return f"за {pages_checked} стр. выдачи"

    def _search_apify(self, query: str, page: int) -> list[SearchResultItem]:
        if page > 1:
            return []
        query_key = " ".join(str(query or "").casefold().split())
        if query_key in self._apify_cache:
            return self._apify_cache[query_key]

        payload = self._post_apify_json(
            {
                "query": query,
                "maxItems": self.apify_max_items,
                "enrichProducts": self.apify_enrich_products,
                "includeReviews": False,
                "region": str(self.region_id),
            }
        )
        items = parse_apify_search_items(payload)
        self._apify_cache[query_key] = items
        return items

    def _post_apify_json(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        params = urllib.parse.urlencode(
            {
                "timeout": min(max(int(self.apify_timeout), 1), 300),
                "maxChargedDatasetItems": self.apify_max_items,
            }
        )
        separator = "&" if "?" in self.apify_api_url else "?"
        url = f"{self.apify_api_url}{separator}{params}"
        request = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.apify_api_token}",
                "Content-Type": "application/json",
                "User-Agent": "marketplace-position-bot/1.0",
            },
        )
        remaining = self._remaining_time()
        timeout = min(self.apify_timeout, remaining) if remaining is not None else self.apify_timeout
        try:
            with self.apify_opener.open(request, timeout=max(timeout, 1.0)) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            details = error.read().decode("utf-8", errors="replace")
            raise YandexMarketError(
                f"Apify вернул HTTP {error.code}: {apify_error_preview(details)}"
            ) from error
        except TRANSIENT_NETWORK_ERRORS as error:
            raise YandexMarketError(
                "соединение с Apify прервалось; повтори проверку позже, чтобы не запустить платный запрос дважды"
            ) from error

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise YandexMarketError(
                f"Apify вернул ответ не в формате JSON: {apify_error_preview(raw)}"
            ) from error
        if not isinstance(payload, list):
            raise YandexMarketError(
                f"Apify не вернул список товаров: {apify_error_preview(payload)}"
            )
        return [item for item in payload if isinstance(item, dict)]

    def _enrich_top_sellers(self, items: list[SearchResultItem], limit: int) -> list[SearchResultItem]:
        enriched = list(items)
        for index, item in enumerate(enriched[:limit]):
            self._remaining_time()
            if item.supplier_name or not item.supplier_id:
                continue
            cached = self._seller_cache.get(item.supplier_id)
            if cached:
                enriched[index] = replace(item, supplier_name=cached)
                continue
            if not item.url:
                continue
            try:
                detail_html = self._get_html(item.url)
            except YandexMarketError:
                continue
            seller_name = extract_seller_name(detail_html, item.supplier_id)
            if seller_name:
                self._seller_cache[item.supplier_id] = seller_name
                enriched[index] = replace(item, supplier_name=seller_name)
        return enriched

    def _get_html(self, url: str) -> str:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._wait_for_slot()
            remaining = self._remaining_time()
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
                    "Cache-Control": "no-cache",
                    "Referer": "https://market.yandex.ru/",
                    "User-Agent": random.choice(USER_AGENTS),
                },
            )
            try:
                request_timeout = min(self.timeout, remaining) if remaining is not None else self.timeout
                with self.opener.open(request, timeout=max(request_timeout, 0.5)) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                if is_blocked_page(raw):
                    raise YandexMarketError("Яндекс Маркет повернув сторінку перевірки або CAPTCHA")
                return raw
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code in {403, 429, 500, 502, 503, 504} and attempt < self.retries:
                    self._sleep(min(3.0 * attempt, 15.0))
                    continue
                raise YandexMarketError(f"HTTP {error.code}") from error
            except TRANSIENT_NETWORK_ERRORS as error:
                last_error = error
                if attempt < self.retries:
                    self._sleep(min(2.5 * attempt, 10.0))
                    continue
                raise YandexMarketError(f"помилка мережі: {error}") from error
            except YandexMarketError:
                raise
        raise YandexMarketError(str(last_error or "не вдалося отримати пошукову видачу"))

    def _wait_for_slot(self) -> None:
        delay = self.request_delay_seconds
        if self.request_delay_jitter_seconds > 0:
            delay += random.uniform(0, self.request_delay_jitter_seconds)
        now = time.monotonic()
        wait_for = delay - (now - self._last_request_at)
        if wait_for > 0:
            self._sleep(wait_for)
        self._last_request_at = time.monotonic()

    def _remaining_time(self) -> float | None:
        if self._operation_deadline is None:
            return None
        remaining = self._operation_deadline - time.monotonic()
        if remaining <= 0:
            raise YandexMarketError("проверка превысила допустимое время и была остановлена")
        return remaining

    def _sleep(self, seconds: float) -> None:
        remaining = self._remaining_time()
        if remaining is None:
            time.sleep(seconds)
            return
        if seconds >= remaining:
            time.sleep(remaining)
            raise YandexMarketError("проверка превысила допустимое время и была остановлена")
        time.sleep(seconds)


def parse_apify_search_items(rows: list[dict[str, Any]]) -> list[SearchResultItem]:
    positioned: list[tuple[int, int, SearchResultItem]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        id_candidates = unique_text_values(
            row,
            "oskuId",
            "articleNumber",
            "marketSku",
            "sku",
            "modelId",
        )
        url = first_text(row, "productUrl", "canonicalUrl", "url")
        url_id = yandex_id_from_url(url)
        if url_id and url_id not in id_candidates:
            id_candidates.insert(0, url_id)
        if not id_candidates:
            continue

        external_id = id_candidates[0]
        identity = external_id or url
        if identity in seen:
            continue
        seen.add(identity)

        price = parse_market_price(row.get("price"))
        position = first_int(row, "searchPosition", "position") or index + 1
        item = SearchResultItem(
            rank=position,
            nm_id=first_int(row, "modelId") or 0,
            external_id=external_id,
            alternate_ids=tuple(value for value in id_candidates if value != external_id),
            name=first_text(row, "title", "name", "modelName"),
            brand=first_text(row, "brand"),
            supplier_id=first_int(row, "businessId", "shopId", "supplierId", "vendorId"),
            supplier_name=first_text(row, "sellerName", "shopName", "supplierName"),
            price=price,
            sale_price=price,
            rating=parse_float(row.get("rating")),
            feedbacks=first_int(row, "reviewCount", "ratingCount"),
            url=url or f"https://market.yandex.ru/card/-/{external_id}",
        )
        positioned.append((position, index, item))

    positioned.sort(key=lambda value: (value[0], value[1]))
    return [item for _, _, item in positioned]


def unique_text_values(item: dict[str, Any], *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        value = first_text(item, key)
        if value and value not in values:
            values.append(value)
    return values


def yandex_id_from_url(url: str) -> str:
    match = re.search(r"/(?:card/[^/?#]+|product--[^/?#]+)/(?P<id>\d+)(?:[/?#]|$)", str(url or ""))
    return match.group("id") if match else ""


def apify_error_preview(value: Any, limit: int = 240) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value or "")
    return re.sub(r"\s+", " ", text).strip()[:limit] or "пустой ответ"


def parse_yandex_search_html(raw: str) -> list[SearchResultItem]:
    text = html.unescape(str(raw or "")).replace("\\/", "/")
    if '"pendingCartItem":' not in text and '\\"pendingCartItem\\"' in text:
        text = text.replace('\\"', '"')
    offer_to_osku = extract_offer_to_osku(text)
    seller_names = extract_seller_names(text)
    parsed: list[SearchResultItem] = []
    seen: set[str] = set()

    for item in json_objects_after_marker(text, '"pendingCartItem":'):
        offer_id = first_text(item, "offerId", "wareId")
        product_id = first_int(item, "productId", "modelId")
        sku_id = first_text(item, "skuId", "marketSku")
        external_id = offer_to_osku.get(offer_id) or sku_id or str(product_id or "")
        identity = offer_id or external_id
        if not identity or identity in seen:
            continue
        seen.add(identity)

        business_id = first_int(item, "businessId", "shopId", "supplierId")
        supplier_name = first_text(
            item,
            "businessName",
            "shopName",
            "sellerName",
            "supplierName",
            "vendorName",
        )
        if not supplier_name and business_id:
            supplier_name = seller_names.get(business_id, "")

        price = parse_market_price(item.get("price"))
        url = find_product_url(text, external_id, offer_id)
        parsed.append(
            SearchResultItem(
                rank=0,
                nm_id=product_id or 0,
                external_id=external_id,
                name=first_text(item, "name", "title", "productName"),
                brand=first_text(item, "brand", "vendor"),
                supplier_id=business_id,
                supplier_name=supplier_name,
                price=price,
                sale_price=price,
                rating=parse_float(item.get("rating") or item.get("ratingValue")),
                feedbacks=first_int(item, "reviewCount", "opinions", "ratingCount"),
                url=url,
            )
        )
    return parsed


def json_objects_after_marker(text: str, marker: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    values: list[dict[str, Any]] = []
    cursor = 0
    while True:
        marker_pos = text.find(marker, cursor)
        if marker_pos < 0:
            break
        start = text.find("{", marker_pos + len(marker))
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        if isinstance(value, dict):
            values.append(value)
        cursor = start + max(end, 1)
    return values


def extract_offer_to_osku(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in re.finditer(r'"oskuId"\s*:\s*"?(\d+)"?', text):
        window = text[match.start() : match.start() + 900]
        offer = re.search(r'"offerId"\s*:\s*"([^"\\]+)"', window)
        if offer:
            result[offer.group(1)] = match.group(1)
    return result


def extract_seller_names(text: str) -> dict[int, str]:
    result: dict[int, str] = {}
    name_keys = r'(?:businessName|shopName|sellerName|supplierName|shopTitle)'
    direct_pattern = re.compile(
        rf'"businessId"\s*:\s*"?(\d+)"?[^{{}}]{{0,700}}?"{name_keys}"\s*:\s*"((?:\\.|[^"\\])*)"'
    )
    reverse_pattern = re.compile(
        rf'"{name_keys}"\s*:\s*"((?:\\.|[^"\\])*)"[^{{}}]{{0,700}}?"businessId"\s*:\s*"?(\d+)"?'
    )
    for match in direct_pattern.finditer(text):
        result[int(match.group(1))] = decode_json_text(match.group(2))
    for match in reverse_pattern.finditer(text):
        result[int(match.group(2))] = decode_json_text(match.group(1))

    for match in re.finditer(r'"businessId"\s*:\s*"?(\d+)"?', text):
        business_id = int(match.group(1))
        if business_id in result:
            continue
        window = text[match.start() : match.start() + 900]
        name = extract_name_from_window(window)
        if name:
            result[business_id] = name
    return result


def extract_seller_name(text: str, business_id: int) -> str:
    names = extract_seller_names(html.unescape(text).replace("\\/", "/"))
    if business_id in names:
        return names[business_id]
    visible = re.search(r'Продавец.{0,800}?>([^<>]{2,100})<', text, flags=re.IGNORECASE | re.DOTALL)
    return clean_text(visible.group(1)) if visible else ""


def extract_name_from_window(window: str) -> str:
    keys = ("businessName", "shopName", "sellerName", "supplierName", "shopTitle")
    for key in keys:
        match = re.search(rf'"{key}"\s*:\s*"((?:\\.|[^"\\])*)"', window)
        if not match:
            continue
        cleaned = decode_json_text(match.group(1))
        if cleaned:
            return cleaned
    return ""


def decode_json_text(value: str) -> str:
    try:
        decoded = json.loads(f'"{value}"')
    except json.JSONDecodeError:
        decoded = value
    return clean_text(decoded)


def find_product_url(text: str, external_id: str, offer_id: str) -> str:
    if external_id:
        pattern = rf'((?:https://market\.yandex\.ru)?/card/[^"\s<>]+/{re.escape(external_id)}[^"\s<>]*)'
        match = re.search(pattern, text)
        if match:
            url = match.group(1).replace("&amp;", "&")
            return url if url.startswith("http") else "https://market.yandex.ru" + url
    if external_id:
        params = {"do-waremd5": offer_id} if offer_id else {}
        suffix = "?" + urllib.parse.urlencode(params) if params else ""
        return f"https://market.yandex.ru/product--/{external_id}{suffix}"
    return "https://market.yandex.ru/"


def parse_market_price(raw: Any) -> float | None:
    if isinstance(raw, dict):
        raw = raw.get("value") or raw.get("current") or raw.get("discount")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def first_text(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return clean_text(value)
    return ""


def first_int(item: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        try:
            value = int(item.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def parse_float(raw: Any) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def is_blocked_page(raw: str) -> bool:
    lower = str(raw or "").lower()
    markers = (
        "showcaptcha",
        "smart-captcha",
        "подтвердите, что запросы отправляли вы",
        "<title>ой!</title>",
    )
    return any(marker in lower for marker in markers)
