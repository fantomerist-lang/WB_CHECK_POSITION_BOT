from __future__ import annotations

import http.cookiejar
import json
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
    "https://search.wb.ru/exactmatch/ru/common/v13/search",
    "https://search.wb.ru/exactmatch/ru/common/v12/search",
    "https://search.wb.ru/exactmatch/ru/common/v11/search",
)


USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
)


class WildberriesClient:
    def __init__(
        self,
        dest: str = "-1257786",
        currency: str = "rub",
        locale: str = "ru",
        timeout: float = 25.0,
        request_delay_seconds: float = 0.8,
        request_delay_jitter_seconds: float = 0.0,
        retries: int = 3,
        rate_limit_cooldown_seconds: float = 15.0,
        proxy_url: str = "",
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
        self._last_request_at = 0.0
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = build_opener(proxy_url, self.cookie_jar, proxy_insecure_ssl)
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
        for endpoint in SEARCH_ENDPOINTS:
            try:
                payload = self._get_json(endpoint, params)
                products = payload.get("data", {}).get("products", [])
                if not isinstance(products, list):
                    return []
                return [parse_search_item(item) for item in products if isinstance(item, dict) and item.get("id")]
            except WildberriesRateLimitError as error:
                raise error
            except Exception as error:
                last_error = error
                continue
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
            except (urllib.error.URLError, TimeoutError, socket.timeout) as error:
                last_error = error
                if attempt < self.retries:
                    time.sleep(min(1.5 * attempt, 6.0))
                    continue
                raise WildberriesError(str(error)) from error
        else:
            raise WildberriesError(str(last_error))

        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise WildberriesError("WB вернул не JSON") from error

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
) -> urllib.request.OpenerDirector:
    proxy = str(proxy_url or "").strip()
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
    return urllib.request.build_opener(*handlers)


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


def parse_search_item(item: dict[str, Any]) -> SearchResultItem:
    nm_id = int(item["id"])
    supplier_id = parse_int(item.get("supplierId") or item.get("supplier_id"))
    supplier_name = str(item.get("supplier") or item.get("supplierName") or "").strip()
    name = str(item.get("name") or "").strip()
    brand = str(item.get("brand") or "").strip()
    return SearchResultItem(
        rank=0,
        nm_id=nm_id,
        name=name,
        brand=brand,
        supplier_id=supplier_id,
        supplier_name=supplier_name,
        price=parse_price(item.get("priceU") or item.get("price")),
        sale_price=parse_price(item.get("salePriceU") or item.get("salePrice")),
        rating=parse_float(item.get("reviewRating") or item.get("rating")),
        feedbacks=parse_int(item.get("feedbacks")),
        url=f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx",
    )
