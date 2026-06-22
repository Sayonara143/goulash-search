"""Запрос извлечения поставщиков из текста страницы."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from .....config import settings
from .....models import ExtractedSuppliers
from .....tools import ScrapedPage


class ChatClient(Protocol):
    def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Выполнить запрос к chat completions."""


_SYSTEM_PROMPT = """\
Ты — ассистент по закупкам. Тебе дают текст веб-страницы поставщика продуктов
питания. Извлеки информацию о поставщике(ах) строго по схеме.

Правила:
- Бери только то, что реально есть в тексте. Ничего не выдумывай.
- Если поля нет — оставь его пустым (null), не подставляй заглушки.
- В certificates перечисляй стандарты (ГОСТ, ТР ТС, ISO, ХАССП), если упомянуты.
- Заполняй поля, которые используются в таблице: name, region, phone, email,
  min_order, price_info, certificates, delivery, source_url.
- products — конкретные товары/категории, релевантные запросу.
- Если на странице нет ни одного поставщика — верни пустой список.
"""

_USER_PROMPT_TEMPLATE = """\
Проанализируй очищенный текст страницы и извлеки данные о поставщике food/B2B-направления.

Контекст поиска:
Категория товара: {{category}}
Регион или город поиска: {{region}}

Источник:
URL страницы: {{url}}
Заголовок страницы: не указан
Найденные телефоны регуляркой: {{phones}}
Найденные email регуляркой: {{emails}}

Текст страницы:
{{text}}\
"""

_PROMPT_CONFIG_PATH = Path(__file__).with_name("prompt.json")


@lru_cache(maxsize=1)
def _prompt_config() -> dict[str, Any]:
    if not _PROMPT_CONFIG_PATH.exists():
        return {
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _USER_PROMPT_TEMPLATE},
            ],
            "temperature": 0.1,
        }

    with _PROMPT_CONFIG_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def _response_format() -> dict[str, Any]:
    value = _prompt_config().get("response_format")
    if isinstance(value, dict):
        return value
    return {
        "type": "json_schema",
        "schema": ExtractedSuppliers.model_json_schema(),
        "strict": True,
    }


def _temperature() -> float:
    value = _prompt_config().get("temperature", 0.1)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.1


def _render_template(template: str, values: dict[str, str]) -> str:
    result = template
    for key, value in values.items():
        result = result.replace(f"{{{{{key}}}}}", value)
    return result


def _message_templates() -> list[dict[str, str]]:
    messages = _prompt_config().get("messages")
    if not isinstance(messages, list):
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _USER_PROMPT_TEMPLATE},
        ]

    result: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if isinstance(role, str) and isinstance(content, str):
            result.append({"role": role, "content": content})
    return result or [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _USER_PROMPT_TEMPLATE},
    ]


def build_prompt(page: ScrapedPage, category: str, region: str | None) -> str:
    values = {
        "category": category,
        "region": region or "не указан",
        "url": page.url,
        "phones": ", ".join(page.phones) or "нет",
        "emails": ", ".join(page.emails) or "нет",
        "text": page.text,
    }

    user_messages = [
        _render_template(message["content"], values)
        for message in _message_templates()
        if message["role"] == "user"
    ]
    return "\n\n".join(user_messages) if user_messages else _render_template(
        _USER_PROMPT_TEMPLATE, values
    )


def _build_messages(page: ScrapedPage, category: str, region: str | None) -> list[dict[str, str]]:
    values = {
        "category": category,
        "region": region or "не указан",
        "url": page.url,
        "phones": ", ".join(page.phones) or "нет",
        "emails": ", ".join(page.emails) or "нет",
        "text": page.text,
    }
    return [
        {
            "role": message["role"],
            "content": _render_template(message["content"], values),
        }
        for message in _message_templates()
    ]


def _build_payload(page: ScrapedPage, category: str, region: str | None) -> dict[str, Any]:
    return {
        "model": settings.gigachat_model,
        "messages": _build_messages(page, category, region),
        "response_format": _response_format(),
        "temperature": _temperature(),
    }


def _response_content(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
    raise RuntimeError("GigaChat вернул ответ без choices[0].message.content")


def _parse_json_content(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise TypeError("GigaChat вернул JSON не в виде объекта")
    return data


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_list_item(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    for item in value:
        if isinstance(item, str) and item.strip():
            return item.strip()
    return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _join_unique(*values: Any) -> str | None:
    parts: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
        elif isinstance(value, list):
            parts.extend(_string_list(value))
    unique = list(dict.fromkeys(parts))
    return "; ".join(unique) or None


def _host_from_url(url: str) -> str | None:
    host = urlsplit(url).netloc
    return host.removeprefix("www.") if host else None


def _supplier_from_prompt_schema(data: dict[str, Any], page: ScrapedPage) -> ExtractedSuppliers:
    if not data.get("is_supplier_found") or not data.get("is_relevant_food_b2b_supplier"):
        return ExtractedSuppliers(suppliers=[])

    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    supplier = data.get("supplier") if isinstance(data.get("supplier"), dict) else {}
    contacts = data.get("contacts") if isinstance(data.get("contacts"), dict) else {}
    location = data.get("location") if isinstance(data.get("location"), dict) else {}
    terms = (
        data.get("commercial_terms")
        if isinstance(data.get("commercial_terms"), dict)
        else {}
    )

    phones = _string_list(contacts.get("phones"))
    emails = _string_list(contacts.get("emails"))
    source_url = _first_string(source.get("url"), page.url) or page.url
    name = _first_string(
        supplier.get("company_name"),
        supplier.get("legal_name"),
        source.get("page_title"),
        _host_from_url(source_url),
        page.url,
    )

    price_info = _first_string(terms.get("price_info"))
    if not price_info and terms.get("has_price_list"):
        price_info = "Упомянут прайс-лист"

    notes = _join_unique(
        supplier.get("supplier_type"),
        supplier.get("legal_name"),
        f"ИНН: {supplier['inn']}" if supplier.get("inn") else None,
        f"Рекомендация: {data['recommendation']}" if data.get("recommendation") else None,
        (
            f"Приоритет контакта: {data['contact_priority_score']}/100"
            if data.get("contact_priority_score") is not None
            else None
        ),
        data.get("target_clients"),
        data.get("advantages"),
        data.get("strengths"),
        data.get("weaknesses"),
        terms.get("payment_terms"),
        terms.get("documents"),
        data.get("notes"),
    )

    output = {
        "suppliers": [
            {
                "name": name,
                "description": _first_string(supplier.get("description")),
                "products": _string_list(data.get("products")),
                "website": _first_string(contacts.get("website")),
                "phone": _first_string(_first_list_item(phones), _first_list_item(page.phones)),
                "email": _first_string(
                    contacts.get("order_email"),
                    contacts.get("cooperation_email"),
                    _first_list_item(emails),
                    _first_list_item(page.emails),
                ),
                "source_url": source_url,
                "region": _join_unique(
                    location.get("city"),
                    location.get("region"),
                    location.get("working_regions"),
                ),
                "min_order": _first_string(terms.get("min_order")),
                "price_info": price_info,
                "certificates": _string_list(terms.get("certificates")),
                "delivery": _first_string(terms.get("delivery_terms")),
                "notes": notes,
            }
        ]
    }
    return ExtractedSuppliers.model_validate(output)


def _parse_structured_content(content: str, page: ScrapedPage) -> ExtractedSuppliers:
    data = _parse_json_content(content)
    if "suppliers" in data:
        return ExtractedSuppliers.model_validate(data)
    return _supplier_from_prompt_schema(data, page)


def extract_suppliers_from_page(
    client: ChatClient, page: ScrapedPage, category: str, region: str | None = None
) -> tuple[ExtractedSuppliers, dict[str, Any], str]:
    """Выполнить запрос извлечения поставщиков и вернуть результат, ответ и промпт."""
    prompt = build_prompt(page, category, region)
    response = client.chat(_build_payload(page, category, region))
    output = _parse_structured_content(_response_content(response), page)
    return output, response, prompt
