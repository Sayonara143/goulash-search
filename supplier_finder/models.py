"""Схемы данных сервиса.

Всё крутится вокруг модели `Supplier`. Именно она превращает «сырой» текст
со страниц поставщиков в одинаково структурированные, сравнимые записи.
Эта же схема используется для structured output GigaChat и последующей
валидации ответа.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    """Запрос пользователя на поиск поставщиков."""

    category: str = Field(description="Категория товара, напр. 'мука пшеничная'")
    city: str | None = Field(default=None, description="Город, напр. 'Екатеринбург'")
    region: str | None = Field(
        default=None, description="Регион, напр. 'Свердловская область'"
    )
    max_suppliers: int = Field(
        default=8, ge=1, le=50, description="Сколько поставщиков вернуть"
    )


SearchQueryType = Literal[
    "supplier",
    "wholesale",
    "manufacturer",
    "distributor",
    "horeca",
    "catalog",
]


class GeneratedSearchQuery(BaseModel):
    """Один поисковый запрос для Yandex Search API."""

    type: SearchQueryType = Field(description="Тип поискового намерения")
    query: str = Field(description="Готовая строка запроса для поиска")
    reason: str = Field(description="Зачем нужен этот вариант запроса")


class GeneratedSearchQueries(BaseModel):
    """Набор поисковых запросов, сгенерированных LLM."""

    queries: list[GeneratedSearchQuery] = Field(
        default_factory=list, min_length=8, max_length=12
    )


class Supplier(BaseModel):
    """Структурированная карточка поставщика.

    Поля с `| None` — необязательные: если их нет на странице, модель
    обязана вернуть null, а не выдумывать. Это критично для доверия к данным.
    """

    name: str = Field(description="Название компании или бренда")
    description: str | None = Field(
        default=None, description="Чем занимается, 1–2 предложения"
    )
    products: list[str] = Field(
        default_factory=list, description="Релевантные товары/категории"
    )

    # Контакты и источник
    website: str | None = None
    phone: str | None = None
    email: str | None = None
    source_url: str = Field(description="Откуда взята информация")

    # Коммерческие условия (раздел 'дополнительно' из ТЗ)
    region: str | None = Field(default=None, description="Регион работы / город")
    min_order: str | None = Field(default=None, description="Минимальный объём заказа")
    price_info: str | None = Field(default=None, description="Цена или ценовой ориентир")
    certificates: list[str] = Field(
        default_factory=list, description="ГОСТ, ТР ТС, ISO, ХАССП и т.п."
    )
    delivery: str | None = Field(default=None, description="Условия доставки")
    notes: str | None = Field(default=None, description="Заметки / на что обратить внимание")


class ExtractedSuppliers(BaseModel):
    """Обёртка вокруг списка — целевой тип для structured output GigaChat.

    На одной странице (например, каталоге) может быть несколько поставщиков,
    поэтому модель извлекает список, а не одну запись.
    """

    suppliers: list[Supplier] = Field(default_factory=list)


class ExtractionDiagnostic(BaseModel):
    """Отладочный статус извлечения по одной ссылке."""

    url: str
    status: Literal["ok", "empty", "error"] = "ok"
    suppliers_found: int = 0
    error: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class PageExtractionResult(BaseModel):
    """Результат GigaChat по одной странице вместе с диагностикой."""

    suppliers: list[Supplier] = Field(default_factory=list)
    diagnostic: ExtractionDiagnostic


class RankedSupplier(BaseModel):
    """Поставщик с рассчитанной оценкой — финальная единица выдачи."""

    supplier: Supplier
    score: float = Field(description="Итоговый балл 0–100")
    completeness: float = Field(description="Полнота заполнения карточки 0–1")
    reasons: list[str] = Field(
        default_factory=list, description="Почему стоит / не стоит связываться"
    )
