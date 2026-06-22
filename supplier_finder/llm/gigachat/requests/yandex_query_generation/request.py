"""Запрос генерации поисковых запросов для Yandex Search API."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from .....config import settings
from .....models import GeneratedSearchQueries


class ChatClient(Protocol):
    def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Выполнить запрос к chat completions."""


_USER_PROMPT_TEMPLATE = """\
Ты генерируешь поисковые запросы для Yandex Search API.

Цель: найти реальные компании-поставщики продуктов питания, упаковки, ингредиентов или товаров для food-направления.

Входные данные:
Категория товара: {{category}}
Город: {{city}}
Регион: {{region}}

Сгенерируй от 8 до 12 поисковых запросов на русском языке.

Требования к запросам:

1. Запросы должны быть коммерческими и B2B-ориентированными.
2. Каждый запрос должен содержать категорию товара.
3. Большинство запросов должны содержать город или регион.
4. Используй разные поисковые намерения:

   * поставщики;
   * опт;
   * производители;
   * дистрибьюторы;
   * HoReCa;
   * продукты для кафе и ресторанов.
5. Не выдумывай названия компаний.
6. Не используй кавычки.
7. Не используй поисковые операторы site:, inurl:, filetype:.
8. Не делай слишком общие запросы вроде “еда Екатеринбург”.
9. Не добавляй пояснения вне JSON.
10. Верни только валидный JSON.

Формат ответа:

{
"queries": [
{
"type": "supplier",
"query": "поставщики молочной продукции Екатеринбург оптом",
"reason": "поиск компаний, которые могут поставлять товар оптом"
}
]
}

Допустимые значения поля type:

* supplier
* wholesale
* manufacturer
* distributor
* horeca
* catalog\
"""

_PROMPT_CONFIG_PATH = Path(__file__).with_name("prompt.json")


@lru_cache(maxsize=1)
def _prompt_config() -> dict[str, Any]:
    if not _PROMPT_CONFIG_PATH.exists():
        return {
            "messages": [{"role": "user", "content": _USER_PROMPT_TEMPLATE}],
            "temperature": 0.2,
        }

    with _PROMPT_CONFIG_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def _response_format() -> dict[str, Any]:
    value = _prompt_config().get("response_format")
    if isinstance(value, dict):
        return value
    return {
        "type": "json_schema",
        "schema": GeneratedSearchQueries.model_json_schema(),
        "strict": True,
    }


def _temperature() -> float:
    value = _prompt_config().get("temperature", 0.2)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.2


def _render_template(template: str, values: dict[str, str]) -> str:
    result = template
    for key, value in values.items():
        result = result.replace(f"{{{{{key}}}}}", value)
    return result


def _message_templates() -> list[dict[str, str]]:
    messages = _prompt_config().get("messages")
    if not isinstance(messages, list):
        return [{"role": "user", "content": _USER_PROMPT_TEMPLATE}]

    result: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if isinstance(role, str) and isinstance(content, str):
            result.append({"role": role, "content": content})
    return result or [{"role": "user", "content": _USER_PROMPT_TEMPLATE}]


def build_prompt(category: str, city: str | None, region: str | None) -> str:
    values = {
        "category": category,
        "city": city or "не указан",
        "region": region or "не указан",
    }
    return "\n\n".join(
        _render_template(message["content"], values)
        for message in _message_templates()
        if message["role"] == "user"
    )


def _build_messages(
    category: str, city: str | None, region: str | None
) -> list[dict[str, str]]:
    values = {
        "category": category,
        "city": city or "не указан",
        "region": region or "не указан",
    }
    return [
        {
            "role": message["role"],
            "content": _render_template(message["content"], values),
        }
        for message in _message_templates()
    ]


def _build_payload(category: str, city: str | None, region: str | None) -> dict[str, Any]:
    return {
        "model": settings.gigachat_model,
        "messages": _build_messages(category, city, region),
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


def generate_yandex_search_queries(
    client: ChatClient,
    category: str,
    city: str | None = None,
    region: str | None = None,
) -> tuple[GeneratedSearchQueries, dict[str, Any], str]:
    """Сгенерировать набор поисковых запросов для Yandex Search API."""
    prompt = build_prompt(category, city, region)
    response = client.chat(_build_payload(category, city, region))
    output = GeneratedSearchQueries.model_validate(
        _parse_json_content(_response_content(response))
    )
    return output, response, prompt
