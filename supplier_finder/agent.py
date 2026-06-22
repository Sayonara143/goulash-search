"""Асинхронная обёртка над LLM-извлечением поставщиков."""

from __future__ import annotations

import asyncio
import logging
import time

from .config import settings
from .llm.gigachat import build_client
from .models import ExtractionDiagnostic, GeneratedSearchQuery, PageExtractionResult
from .tools import ScrapedPage

logger = logging.getLogger("supplier_finder.gigachat")
_GIGACHAT_REQUEST_LOCK = asyncio.Lock()


def _usage_from_response(response: dict) -> dict[str, int]:
    usage = response.get("usage") or {}
    return {
        "prompt_tokens": _int_usage_value(usage.get("prompt_tokens")),
        "completion_tokens": _int_usage_value(usage.get("completion_tokens")),
        "total_tokens": _int_usage_value(usage.get("total_tokens")),
    }


def _int_usage_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _run_gigachat(page: ScrapedPage, category: str, region: str | None):
    client = build_client()
    return client.extract_suppliers_from_page(page, category, region)


def _run_yandex_query_generation(
    category: str, city: str | None, region: str | None
):
    client = build_client()
    return client.generate_yandex_search_queries(category, city, region)


async def generate_yandex_queries(
    category: str, city: str | None = None, region: str | None = None
) -> tuple[list[GeneratedSearchQuery], dict[str, int]]:
    """Сгенерировать поисковые запросы для последующего веб-поиска."""
    started = time.monotonic()

    async with _GIGACHAT_REQUEST_LOCK:
        logger.info(
            "Старт генерации поисковых запросов в GigaChat: category=%r city=%r region=%r",
            category,
            city,
            region,
        )
        output, response, prompt = await asyncio.to_thread(
            _run_yandex_query_generation,
            category,
            city,
            region,
        )

    usage = _usage_from_response(response)
    logger.info(
        "GigaChat сгенерировал поисковые запросы: count=%d за %.2f c "
        "(tokens: req=%s resp=%s total=%s)",
        len(output.queries),
        time.monotonic() - started,
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
        usage.get("total_tokens"),
    )
    logger.debug("GigaChat search-query prompt:\n%s", prompt)
    logger.debug("GigaChat search-query output:\n%s", output)
    return output.queries, usage


async def extract_from_page(
    page: ScrapedPage, category: str, region: str | None = None
) -> PageExtractionResult:
    """Извлечь поставщиков из одной скрапнутой страницы."""
    started = time.monotonic()

    try:
        async with _GIGACHAT_REQUEST_LOCK:
            logger.info("Старт запроса в GigaChat: url=%s", page.url)
            output, response, prompt = await asyncio.to_thread(
                _run_gigachat,
                page,
                category,
                region,
            )
    except Exception as exc:
        # Страница могла оказаться мусорной — не валим весь пайплайн,
        # но фиксируем причину с трейсбеком, иначе ошибка теряется молча.
        logger.exception(
            "Ошибка запроса в GigaChat (url=%s, %.2f c) — страница пропущена",
            page.url,
            time.monotonic() - started,
        )
        return PageExtractionResult(
            diagnostic=ExtractionDiagnostic(
                url=page.url,
                status="error",
                error=f"{type(exc).__name__}: {exc}",
            )
        )

    logger.info(
        "Запрос в GigaChat: model=%s category=%r url=%s prompt_chars=%d "
        "phones=%d emails=%d",
        settings.gigachat_model,
        category,
        page.url,
        len(prompt),
        len(page.phones),
        len(page.emails),
    )
    # Полный текст промпта — только в DEBUG, чтобы не засорять обычные логи.
    logger.debug("GigaChat prompt (url=%s):\n%s", page.url, prompt)

    elapsed = time.monotonic() - started
    usage = _usage_from_response(response)
    logger.info(
        "Ответ GigaChat: url=%s suppliers=%d за %.2f c (tokens: req=%s resp=%s total=%s)",
        page.url,
        len(output.suppliers),
        elapsed,
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
        usage.get("total_tokens"),
    )
    logger.debug("GigaChat output (url=%s):\n%s", page.url, output)

    # Подстрахуем источник и контакты тем, что нашли регуляркой.
    for supplier in output.suppliers:
        supplier.source_url = supplier.source_url or page.url
        if not supplier.phone and page.phones:
            supplier.phone = page.phones[0]
        if not supplier.email and page.emails:
            supplier.email = page.emails[0]

    status = "ok" if output.suppliers else "empty"
    error = None if output.suppliers else "GigaChat не нашёл поставщиков на странице"
    return PageExtractionResult(
        suppliers=output.suppliers,
        diagnostic=ExtractionDiagnostic(
            url=page.url,
            status=status,
            suppliers_found=len(output.suppliers),
            error=error,
            **usage,
        ),
    )
