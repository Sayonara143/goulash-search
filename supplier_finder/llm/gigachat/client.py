"""Клиент GigaChat и публичные методы LLM-запросов."""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Any
from uuid import uuid4

from ...config import settings
from ...models import ExtractedSuppliers, GeneratedSearchQueries
from ...tools import ScrapedPage
from .requests import supplier_extraction, yandex_query_generation


class GigaChatClient:
    """Синхронный клиент GigaChat.

    Низкоуровневый метод `chat` отвечает за REST API, а публичные доменные
    методы проксируют выполнение в модули `requests`.
    """

    def __init__(self) -> None:
        if not settings.gigachat_credentials:
            raise RuntimeError(
                "Не задан GIGACHAT_CREDENTIALS. Скопируйте .env.example в .env "
                "и впишите авторизационный ключ из кабинета GigaChat."
            )

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Не установлен пакет httpx. Установите зависимости: pip install -r requirements.txt"
            ) from exc

        self._httpx = httpx
        self._access_token: str | None = None
        self._expires_at = 0.0

        verify: bool | str = settings.gigachat_verify_ssl
        if settings.gigachat_ca_bundle_file:
            verify = settings.gigachat_ca_bundle_file

        timeout = httpx.Timeout(settings.request_timeout)
        self._auth_client = httpx.Client(timeout=timeout, verify=verify)
        self._api_client = httpx.Client(
            base_url=settings.gigachat_base_url.rstrip("/"),
            timeout=timeout,
            verify=verify,
        )

    def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Выполнить POST `/chat/completions` и вернуть JSON-ответ."""
        headers = self._auth_headers()
        response = self._api_client.post("/chat/completions", json=payload, headers=headers)

        if response.status_code == 401:
            self._access_token = None
            headers = self._auth_headers()
            response = self._api_client.post(
                "/chat/completions", json=payload, headers=headers
            )

        self._raise_for_status(response)
        return response.json()

    def extract_suppliers_from_page(
        self, page: ScrapedPage, category: str, region: str | None = None
    ) -> tuple[ExtractedSuppliers, dict[str, Any], str]:
        """Извлечь поставщиков из текста страницы через request-модуль."""
        return supplier_extraction.extract_suppliers_from_page(
            self,
            page=page,
            category=category,
            region=region,
        )

    def generate_yandex_search_queries(
        self, category: str, city: str | None = None, region: str | None = None
    ) -> tuple[GeneratedSearchQueries, dict[str, Any], str]:
        """Сгенерировать поисковые запросы для Yandex Search API."""
        return yandex_query_generation.generate_yandex_search_queries(
            self,
            category=category,
            city=city,
            region=region,
        )

    def _auth_headers(self) -> dict[str, str]:
        token = self._get_access_token()
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }

    def _get_access_token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token

        response = self._auth_client.post(
            settings.gigachat_auth_url,
            headers={
                "Accept": "application/json",
                "Authorization": self._basic_auth_header(),
                "Content-Type": "application/x-www-form-urlencoded",
                "RqUID": str(uuid4()),
            },
            data={"scope": settings.gigachat_scope},
        )
        self._raise_for_status(response)

        data = response.json()
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("GigaChat OAuth вернул ответ без access_token")

        expires_at = float(data.get("expires_at") or 0)
        if expires_at > 10_000_000_000:
            expires_at /= 1000
        if expires_at <= 0:
            expires_at = time.time() + 30 * 60

        self._access_token = token
        self._expires_at = expires_at
        return token

    def _basic_auth_header(self) -> str:
        value = settings.gigachat_credentials.strip()
        if value.lower().startswith("basic "):
            return value
        return f"Basic {value}"

    def _raise_for_status(self, response: Any) -> None:
        try:
            response.raise_for_status()
        except self._httpx.HTTPStatusError as exc:
            detail = response.text
            try:
                detail = response.json()
            except ValueError:
                pass
            raise RuntimeError(
                f"GigaChat REST API вернул HTTP {response.status_code}: {detail}"
            ) from exc


@lru_cache(maxsize=1)
def build_client() -> GigaChatClient:
    """Вернуть готовый синхронный клиент GigaChat."""
    return GigaChatClient()
