from __future__ import annotations

import http.cookiejar
import http.client
import json
import re
import random
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .models import SearchResultItem


class WildberriesError(RuntimeError):
    pass


class WildberriesRateLimitError(WildberriesError):
    pass


SEARCH_ENDPOINTS = (
    "https://search.wb.ru/exactmatch/ru/common/v18/search",
    "https://search.wb.ru/exactmatch/ru/common/v17/search",
    "https://search.wb.ru/exactmatch/ru/common/v16/search",
    "https://search.wb.ru/exactmatch/ru/common/v15/search",
    "https://search.wb.ru/exactmatch/ru/common/v14/search",
    "https://search.wb.ru/exactmatch/ru/common/v13/search",
    "https://search.wb.ru/exactmatch/ru/common/v12/search",
    "https://search.wb.ru/exactmatch/ru/common/v11/search",
    "https://search.wb.ru/exactmatch/ru/common/v10/search",
    "https://search.wb.ru/exactmatch/ru/common/v9/search",
    "https://search.wb.ru/exactmatch/ru/common/v8/search",
    "https://search.wb.ru/exactmatch/ru/common/v7/search",
    "https://search.wb.ru/exactmatch/ru/common/v6/search",
    "https://search.wb.ru/exactmatch/ru/common/v5/search",
    "https://search.wb.ru/exactmatch/ru/common/v4/search",
)


USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
)


TRANSIENT_NETWORK_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    socket.timeout,
    ConnectionError,
    http.client.BadStatusLine,
    http.client.IncompleteRead,
)


class WildberriesClient:
    def __init__(
        self,
        dest: str = "-1257786",
        currency: str = "rub",
        locale: str = "ru",
        timeout: float = 60.0,
        request_delay_seconds: float = 0.8,
        request_delay_jitter_seconds: float = 0.0,
        retries: int = 3,
        rate_limit_cooldown_seconds: float = 15.0,
        proxy_url: str = "",
        proxy_auth_token: str = "",
        proxy_insecure_ssl: bool = False,
    ) -> None:
        self.dest = dest
        self.currency = currency
        self.locale = locale
        self.timeout = timeout
        self.request_delay_seconds = max(float(request_delay_seconds or 0), 0.0)
        self.request_delay_jitter_seconds = max(float(request_delay_jitter_seconds or 0), 0.0)
        self.retries = max(int(retries or 1), 1)
        self.rate_limit_cooldown_seconds = max(float(rate_limit_cooldown_seconds or 0), 0.0)
        self.proxy_url = str(proxy_url or "")
        self._last_request_at = 0.0
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = build_opener(proxy_url, self.cookie_jar, proxy_insecure_ssl, proxy_auth_token)
        self._warmed_up = False

    def search(self, query: str, page: int = 1) -> list[SearchResultItem]:
        self._warm_up()
        params = {
            "ab_testing": "false",
            "appType": "1",
            "curr": self.currency,
            "dest": self.dest,
            "hide_dtype": "13",
            "lang": self.locale,
            "page": str(page),
            "query": query,
            "resultset": "catalog",
            "sort": "popular",
            "spp": "30",
            "suppressSpellcheck": "false",
        }
        last_error: Exception | None = None
        saw_empty_response = False
        for endpoint in SEARCH_ENDPOINTS:
            try:
                payload = self._get_json(endpoint, params)
                products = extract_products(payload)
                parsed = parse_search_items(products)
                if parsed:
                    return parsed
                saw_empty_response = True
            except WildberriesRateLimitError as error:
                raise error
            except Exception as error:
                last_error = error
                continue
        if saw_empty_response:
            return []
        raise WildberriesError(f"не удалось получить выдачу WB: {last_error}")

    def _get_json(self, endpoint: str, params: dict[str, str]) -> dict[str, Any]:
        url = endpoint + "?" + urllib.parse.urlencode(params)
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._wait_for_slot()
            try:
                request = self._request(url)
                with self.opener.open(request, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                break
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code == 429:
                    wait_seconds = self._rate_limit_wait_seconds(error, attempt)
                    if attempt < self.retries:
                        time.sleep(wait_seconds)
                        continue
                    raise WildberriesRateLimitError(
                        f"HTTP 429: WB временно ограничил запросы. Подожди {int(wait_seconds)} сек или включи прокси."
                    ) from error
                if error.code in {403, 429, 500, 502, 503, 504} and attempt < self.retries:
                    time.sleep(min(2.0 * attempt, 8.0))
                    continue
                raise WildberriesError(f"HTTP {error.code}") from error
            except TRANSIENT_NETWORK_ERRORS as error:
                last_error = error
                if attempt < self.retries:
                    time.sleep(min(2.5 * attempt, 10.0))
                    continue
                raise WildberriesError(timeout_or_network_error_message(error, self.timeout)) from error
        else:
            raise WildberriesError(str(last_error))

        return parse_json_response(raw)

    def _wait_for_slot(self) -> None:
        delay = self.request_delay_seconds
        if self.request_delay_jitter_seconds > 0:
            delay += random.uniform(0, self.request_delay_jitter_seconds)
        if delay <= 0:
            self._last_request_at = time.monotonic()
            return
        now = time.monotonic()
        wait_for = delay - (now - self._last_request_at)
        if wait_for > 0:
            time.sleep(wait_for)
        self._last_request_at = time.monotonic()

    def _request(self, url: str) -> urllib.request.Request:
        return urllib.request.Request(
            url,
            headers={
                "Accept": "application/json,text/plain,*/*",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                "Connection": "keep-alive",
                "Origin": "https://www.wildberries.ru",
                "Referer": "https://www.wildberries.ru/",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-site",
                "User-Agent": random.choice(USER_AGENTS),
            },
        )

    def _warm_up(self) -> None:
        if self._warmed_up:
            return
        self._warmed_up = True
        if "unblock.decodo.com" in self.proxy_url:
            return
        request = urllib.request.Request(
            "https://www.wildberries.ru/",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                "Connection": "keep-alive",
                "User-Agent": random.choice(USER_AGENTS),
            },
        )
        try:
            self._wait_for_slot()
            with self.opener.open(request, timeout=self.timeout) as response:
                response.read(2048)
        except Exception:
            return

    def _rate_limit_wait_seconds(self, error: urllib.error.HTTPError, attempt: int) -> float:
        retry_after = error.headers.get("Retry-After") if error.headers else None
        if retry_after:
            try:
                return max(float(retry_after), self.rate_limit_cooldown_seconds)
            except ValueError:
                pass
        return max(self.rate_limit_cooldown_seconds, min(10.0 * attempt, 60.0))


def build_opener(
    proxy_url: str = "",
    cookie_jar: http.cookiejar.CookieJar | None = None,
    insecure_ssl: bool = False,
    proxy_auth_token: str = "",
) -> urllib.request.OpenerDirector:
    proxy = str(proxy_url or "").strip()
    auth_token = str(proxy_auth_token or "").strip()
    handlers: list[urllib.request.BaseHandler] = []
    if cookie_jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(cookie_jar))
    if insecure_ssl or "unblock.decodo.com" in proxy:
        handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
    if not proxy:
        return urllib.request.build_opener(*handlers)
    handlers.append(
        urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy}
        )
    )
    opener = urllib.request.build_opener(*handlers)
    if auth_token:
        opener.addheaders = [("Proxy-Authorization", f"Basic {auth_token}")]
    return opener


def response_preview(raw: str, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    if len(text) > limit:
        text = text[:limit] + "..."
    return text or "<empty>"


def timeout_or_network_error_message(error: Exception, timeout: float) -> str:
    text = str(error) or error.__class__.__name__
    if "timed out" in text.lower() or isinstance(error, (TimeoutError, socket.timeout)):
        return (
            f"таймаут ответа WB/Site Unblocker после {int(timeout)} сек. "
            "Для Decodo Site Unblocker поставь REQUEST_TIMEOUT=60 или 90 в Railway."
        )
    if "remote end closed connection" in text.lower() or isinstance(error, http.client.BadStatusLine):
        return (
            "Site Unblocker оборвал соединение без ответа. "
            "Это временная сетевая ошибка; поставь WB_REQUEST_RETRIES=4 или 5 в Railway."
        )
    return text


def parse_json_response(raw: str) -> dict[str, Any]:
    text = str(raw or "").lstrip("\ufeff")
    if has_not_found_marker(text) and not has_product_marker(text):
        raise WildberriesError("endpoint WB вернул Not Found без списка товаров")

    for candidate in json_candidates(text):
        value = try_decode_json(candidate)
        if isinstance(value, dict):
            return value

    raise WildberriesError(f"WB вернул не JSON: {response_preview(text)}")


def json_candidates(text: str) -> list[str]:
    candidates = [text]
    marker_pos = not_found_marker_pos(text)
    if marker_pos >= 0:
        repaired = repair_json_prefix(text[:marker_pos])
        if repaired:
            candidates.append(repaired)
    return candidates


def try_decode_json(text: str) -> Any:
    candidate = str(text or "").strip()
    if not candidate:
        return None
    try:
        return json.loads(candidate, strict=False)
    except json.JSONDecodeError:
        pass

    start = candidate.find("{")
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder(strict=False).raw_decode(candidate[start:])
    except json.JSONDecodeError:
        return None
    return value


def repair_json_prefix(text: str) -> str:
    candidate = str(text or "").strip().rstrip(",")
    if not candidate:
        return ""

    stack: list[str] = []
    in_string = False
    escaped = False
    for char in candidate:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            stack.append("}")
        elif char == "[":
            stack.append("]")
        elif char in ("}", "]") and stack and stack[-1] == char:
            stack.pop()

    if in_string:
        candidate += '"'
    return candidate + "".join(reversed(stack))


def has_not_found_marker(text: str) -> bool:
    return not_found_marker_pos(text) >= 0


def not_found_marker_pos(text: str) -> int:
    lower = str(text or "").lower()
    positions = [pos for pos in (lower.find("not found"), lower.find("ot found")) if pos >= 0]
    return min(positions) if positions else -1


def has_product_marker(text: str) -> bool:
    lower = str(text or "").lower()
    return '"products"' in lower or '"cards"' in lower or '"items"' in lower


def extract_products(payload: dict[str, Any]) -> list[Any]:
    for container in (payload.get("data"), payload):
        if isinstance(container, dict):
            for key in ("products", "cards", "items"):
                products = container.get(key)
                if is_product_list(products):
                    return products

    found = find_product_list(payload)
    if found is not None:
        return found
    return []


def find_product_list(value: Any, depth: int = 0) -> list[Any] | None:
    if depth > 5:
        return None
    if is_product_list(value):
        return value
    if isinstance(value, dict):
        for key in ("products", "cards", "items", "goods", "nms"):
            found = find_product_list(value.get(key), depth + 1)
            if found is not None:
                return found
        for nested in value.values():
            found = find_product_list(nested, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value[:20]:
            found = find_product_list(nested, depth + 1)
            if found is not None:
                return found
    return None


def is_product_list(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    checked = [item for item in value[:8] if isinstance(item, dict)]
    if not checked:
        return False
    return any(looks_like_product(item) for item in checked)


def looks_like_product(item: dict[str, Any]) -> bool:
    if not extract_nm_id(item):
        return False
    product_keys = {
        "brand",
        "brandName",
        "feedbacks",
        "name",
        "price",
        "priceU",
        "rating",
        "reviewRating",
        "salePrice",
        "salePriceU",
        "seller",
        "sellerName",
        "sizes",
        "supplier",
        "supplierName",
        "title",
    }
    return bool(product_keys.intersection(item))


def first_value(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def first_text(item: dict[str, Any], *keys: str) -> str:
    value = first_value(item, *keys)
    return str(value or "").strip()


def extract_nm_id(item: dict[str, Any]) -> int | None:
    for key in ("id", "nmId", "nm_id", "nm", "productId", "product_id"):
        value = parse_int(item.get(key))
        if value and value > 0:
            return value
    return None


def extract_price_from_sizes(item: dict[str, Any], *keys: str) -> float | None:
    sizes = item.get("sizes")
    if not isinstance(sizes, list):
        return None
    for size in sizes:
        if not isinstance(size, dict):
            continue
        price = size.get("price")
        if not isinstance(price, dict):
            continue
        for key in keys:
            parsed = parse_price(price.get(key))
            if parsed is not None:
                return parsed
    return None


def parse_price(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value / 100


def parse_int(raw: Any) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def parse_float(raw: Any) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def parse_search_items(products: list[Any]) -> list[SearchResultItem]:
    parsed: list[SearchResultItem] = []
    for item in products:
        if not isinstance(item, dict) or not extract_nm_id(item):
            continue
        try:
            parsed.append(parse_search_item(item))
        except WildberriesError:
            continue
    return parsed


def parse_search_item(item: dict[str, Any]) -> SearchResultItem:
    nm_id = extract_nm_id(item)
    if not nm_id:
        raise WildberriesError(f"WB вернул карточку без nm_id: {response_preview(item)}")
    supplier_id = parse_int(first_value(item, "supplierId", "supplier_id", "sellerId", "seller_id"))
    supplier_name = first_text(item, "supplier", "supplierName", "supplier_name", "seller", "sellerName", "seller_name")
    name = first_text(item, "name", "title", "productName", "product_name", "imtName", "imt_name")
    brand = first_text(item, "brand", "brandName", "brand_name")
    price = parse_price(first_value(item, "priceU", "price", "basicPriceU", "basicPrice"))
    sale_price = parse_price(first_value(item, "salePriceU", "salePrice", "totalPriceU", "totalPrice"))
    return SearchResultItem(
        rank=0,
        nm_id=nm_id,
        name=name,
        brand=brand,
        supplier_id=supplier_id,
        supplier_name=supplier_name,
        price=price or extract_price_from_sizes(item, "basic", "product"),
        sale_price=sale_price or extract_price_from_sizes(item, "total", "product", "basic"),
        rating=parse_float(item.get("reviewRating") or item.get("rating")),
        feedbacks=parse_int(item.get("feedbacks")),
        url=f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx",
    )
