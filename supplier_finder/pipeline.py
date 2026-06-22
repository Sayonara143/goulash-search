"""Оркестрация пайплайна: запрос → генерация поиска → скрапинг → извлечение → ранжирование.

Почему пайплайн, а не полностью автономный агент:
GigaChat function-calling в бете и не всегда стабилен. Детерминированный пайплайн
даёт предсказуемость и контроль стоимости, а LLM используется точечно
там, где он силён — извлечение структуры из текста. (Вариант с автономным
агентом, где скрапинг подключён как @agent.tool, описан в README.)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from .agent import extract_from_page, generate_yandex_queries
from .config import settings
from .models import ExtractionDiagnostic, RankedSupplier, SearchRequest, Supplier
from .ranking import deduplicate, rank
from .tools import (
    ScrapedPage,
    scrape_many,
    search_suppliers_by_queries,
)

PagesFoundCallback = Callable[[list[str], list[str], list[ScrapedPage]], None]
ExtractionCallback = Callable[[list[ExtractionDiagnostic]], None]
PipelineCallback = Callable[[str, str, dict[str, Any]], None]
logger = logging.getLogger("supplier_finder.pipeline")

PipelineStage = Literal[
    "generate_queries",
    "search_pages",
    "scrape_pages",
    "extract_suppliers",
    "rank_suppliers",
]

ALL_PIPELINE_STAGES: tuple[PipelineStage, ...] = (
    "generate_queries",
    "search_pages",
    "scrape_pages",
    "extract_suppliers",
    "rank_suppliers",
)


@dataclass(frozen=True)
class PipelineConfig:
    """Конфиг этапов пайплайна.

    Чтобы выключить этап глобально, уберите его из `PIPELINE_CONFIG.enabled_stages`.
    Для разового запуска можно передать свой `PipelineConfig` в `find_suppliers`.
    """

    enabled_stages: tuple[PipelineStage, ...] = ALL_PIPELINE_STAGES

    def is_enabled(self, stage: PipelineStage) -> bool:
        return stage in self.enabled_stages


PIPELINE_CONFIG = PipelineConfig(
    enabled_stages=(
        "generate_queries",
        "search_pages",
        "scrape_pages",
        "extract_suppliers",
        "rank_suppliers",
    )
)


def _emit_pipeline_update(
    callback: PipelineCallback | None,
    stage: str,
    status: str,
    **payload: Any,
) -> None:
    if callback:
        callback(stage, status, payload)


def _skip_remaining_stages(
    callback: PipelineCallback | None,
    stages: tuple[PipelineStage, ...],
) -> None:
    for stage in stages:
        _emit_pipeline_update(callback, stage, "skipped")


async def find_suppliers(
    req: SearchRequest,
    on_pages_found: PagesFoundCallback | None = None,
    on_extraction_update: ExtractionCallback | None = None,
    on_pipeline_update: PipelineCallback | None = None,
    config: PipelineConfig | None = None,
    yandex_groups_on_page: int = 20,
) -> list[RankedSupplier]:
    """Главная точка входа сервиса."""
    pipeline_config = config or PIPELINE_CONFIG

    # 1. GigaChat генерирует поисковые запросы для Yandex Search API.
    generated_queries = []
    query_generation_usage = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    if pipeline_config.is_enabled("generate_queries"):
        _emit_pipeline_update(on_pipeline_update, "generate_queries", "running")
        try:
            generated_queries, query_generation_usage = await generate_yandex_queries(
                category=req.category,
                city=req.city or req.region,
                region=req.region,
            )
        except Exception as exc:
            logger.exception("Не удалось сгенерировать поисковые запросы через GigaChat")
            generated_queries = []
            _emit_pipeline_update(
                on_pipeline_update,
                "generate_queries",
                "error",
                error=f"{type(exc).__name__}: {exc}",
                generated_queries=[],
                gigachat_usage=query_generation_usage,
            )
            if on_pages_found:
                on_pages_found([], [], [])
            _skip_remaining_stages(
                on_pipeline_update,
                ("search_pages", "scrape_pages", "extract_suppliers", "rank_suppliers"),
            )
            return []
        if not generated_queries:
            _emit_pipeline_update(
                on_pipeline_update,
                "generate_queries",
                "error",
                error="GigaChat не вернул поисковые запросы",
                generated_queries=[],
                gigachat_usage=query_generation_usage,
            )
            if on_pages_found:
                on_pages_found([], [], [])
            _skip_remaining_stages(
                on_pipeline_update,
                ("search_pages", "scrape_pages", "extract_suppliers", "rank_suppliers"),
            )
            return []
        _emit_pipeline_update(
            on_pipeline_update,
            "generate_queries",
            "done",
            generated_queries=generated_queries,
            gigachat_usage=query_generation_usage,
        )
    else:
        _emit_pipeline_update(
            on_pipeline_update,
            "generate_queries",
            "skipped",
            generated_queries=[],
            gigachat_usage=query_generation_usage,
        )

    # 2. Поиск страниц-кандидатов с общим лимитом из запроса.
    urls: list[str] = []
    search_query_strings = list(
        dict.fromkeys(
            item.query.strip() for item in generated_queries if item.query.strip()
        )
    )
    uses_yandex_search = bool(
        settings.yandex_search_api_key
        and settings.yandex_folder_id
        and not settings.use_fixed_search_urls
    )
    yandex_request_count = 0
    if pipeline_config.is_enabled("search_pages"):
        def _count_yandex_request() -> None:
            nonlocal yandex_request_count
            if uses_yandex_search:
                yandex_request_count += 1

        _emit_pipeline_update(
            on_pipeline_update,
            "search_pages",
            "running",
            generated_queries=generated_queries,
            yandex_request_count=yandex_request_count,
        )
        urls = await search_suppliers_by_queries(
            search_query_strings,
            per_query_limit=yandex_groups_on_page,
            max_total=req.max_suppliers,
            on_query_searched=_count_yandex_request,
        )
        _emit_pipeline_update(
            on_pipeline_update,
            "search_pages",
            "done",
            urls=urls,
            generated_queries=generated_queries,
            yandex_request_count=yandex_request_count,
        )
    else:
        _emit_pipeline_update(
            on_pipeline_update,
            "search_pages",
            "skipped",
            urls=[],
            generated_queries=generated_queries,
            yandex_request_count=yandex_request_count,
        )
    if not urls:
        if on_pages_found:
            on_pages_found([], [], [])
        _emit_pipeline_update(on_pipeline_update, "scrape_pages", "skipped", pages=[])
        _emit_pipeline_update(
            on_pipeline_update, "extract_suppliers", "skipped", diagnostics=[]
        )
        _emit_pipeline_update(on_pipeline_update, "rank_suppliers", "skipped", ranked=[])
        return []
    if on_pages_found:
        on_pages_found(urls, [], [])

    # 3. Параллельный скрапинг.
    pages: list[ScrapedPage] = []
    if pipeline_config.is_enabled("scrape_pages"):
        _emit_pipeline_update(on_pipeline_update, "scrape_pages", "running", urls=urls)
        pages = await scrape_many(urls)
        _emit_pipeline_update(on_pipeline_update, "scrape_pages", "done", pages=pages)
    else:
        _emit_pipeline_update(
            on_pipeline_update,
            "scrape_pages",
            "skipped",
            urls=urls,
            pages=[],
        )
    if on_pages_found:
        on_pages_found(urls, [p.url for p in pages], pages)

    # 4. Извлечение структуры из каждой страницы через GigaChat.
    # GigaChat-запросы выполняем строго последовательно: следующий запрос
    # стартует только после получения ответа на предыдущий.
    extracted: list[Supplier] = []
    diagnostics: list[ExtractionDiagnostic] = []
    if pipeline_config.is_enabled("extract_suppliers") and pages:
        _emit_pipeline_update(
            on_pipeline_update,
            "extract_suppliers",
            "running",
            diagnostics=[],
            current=0,
            total=len(pages),
        )
        for index, page in enumerate(pages, 1):
            result = await extract_from_page(page, req.category, req.city or req.region)
            extracted.extend(result.suppliers)
            diagnostics.append(result.diagnostic)
            if on_extraction_update:
                on_extraction_update(diagnostics)
            _emit_pipeline_update(
                on_pipeline_update,
                "extract_suppliers",
                "running",
                diagnostics=diagnostics,
                current=index,
                total=len(pages),
            )
        _emit_pipeline_update(
            on_pipeline_update,
            "extract_suppliers",
            "done",
            diagnostics=diagnostics,
            extracted_count=len(extracted),
        )
    else:
        _emit_pipeline_update(
            on_pipeline_update,
            "extract_suppliers",
            "skipped",
            diagnostics=diagnostics,
            extracted_count=0,
        )

    # 5. Дедуп + ранжирование.
    if pipeline_config.is_enabled("rank_suppliers"):
        _emit_pipeline_update(
            on_pipeline_update,
            "rank_suppliers",
            "running",
            extracted_count=len(extracted),
        )
        unique = deduplicate(extracted)
        ranked = rank(unique, req)
        ranked = ranked[: req.max_suppliers]
        _emit_pipeline_update(
            on_pipeline_update,
            "rank_suppliers",
            "done",
            unique_count=len(unique),
            ranked=ranked,
        )
    else:
        ranked = []
        _emit_pipeline_update(
            on_pipeline_update,
            "rank_suppliers",
            "skipped",
            extracted_count=len(extracted),
            ranked=[],
        )

    return ranked
