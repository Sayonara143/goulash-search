"""Streamlit-интерфейс сервиса — то, что показываем как демо.

Запуск:  streamlit run app.py
Галочка «Демо-режим» позволяет показать UI без ключей API (на мок-данных).
"""

from __future__ import annotations

import asyncio
from html import escape

import pandas as pd
import streamlit as st

from supplier_finder.config import settings
from supplier_finder.models import SearchRequest
from supplier_finder.mock_data import mock_results

st.set_page_config(page_title="Поиск поставщиков продуктов питания", layout="wide")

GIGACHAT_PRICE_PER_100M_TOKENS = 6500
YANDEX_SEARCH_PRICE_PER_1000_REQUESTS = 500

PIPELINE_STAGES = [
    {
        "id": "generate_queries",
        "title": "1. GigaChat генерирует запросы для Яндекса",
        "description": "Составляет коммерческие B2B-запросы по категории, городу и региону.",
    },
    {
        "id": "search_pages",
        "title": "2. Поиск страниц-кандидатов",
        "description": "Ищет сайты поставщиков по сгенерированным запросам или fallback-запросу.",
    },
    {
        "id": "scrape_pages",
        "title": "3. Скрапинг найденных страниц",
        "description": "Загружает страницы, чистит текст и вытягивает контакты регулярками.",
    },
    {
        "id": "extract_suppliers",
        "title": "4. Извлечение поставщиков через GigaChat",
        "description": "Последовательно отправляет тексты страниц в GigaChat и валидирует JSON.",
    },
    {
        "id": "rank_suppliers",
        "title": "5. Дедупликация и ранжирование",
        "description": "Объединяет дубли, считает релевантность и полноту карточек.",
    },
]

st.markdown(
    """
    <style>
    .found-links-wrap {
        max-width: 980px;
    }
    .found-links-list {
        margin: 0.2rem 0 0;
        padding-left: 1.15rem;
    }
    .found-links-list li {
        margin: 0.12rem 0;
        line-height: 1.25;
        overflow-wrap: anywhere;
        word-break: break-word;
    }
    .found-links-list a {
        overflow-wrap: anywhere;
        word-break: break-word;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def check_password() -> None:
    """Ворота с паролем: блокируют приложение, пока пароль не введён верно.

    Пароль берётся из переменной окружения APP_PASSWORD (см. .env).
    Если пароль не задан — вход свободный.
    """
    if not settings.app_password or st.session_state.get("auth_ok"):
        return

    pwd = st.text_input("Пароль для доступа", type="password")
    if not pwd:
        st.stop()  # ещё ничего не ввели — дальше не идём
    if pwd == settings.app_password:
        st.session_state["auth_ok"] = True
        st.rerun()  # перерисовать без поля пароля
    else:
        st.error("Неверный пароль")
        st.stop()


check_password()

st.title("🔎 Поиск и сравнение поставщиков продуктов питания")

with st.container(border=True):
    st.subheader("Этапы пайплайна")
    st.caption("Включайте только те шаги, которые нужны для текущего запуска.")
    enabled_pipeline_stages = tuple(
        stage["id"]
        for stage in PIPELINE_STAGES
        if st.toggle(
            stage["title"],
            value=True,
            key=f"pipeline_stage_{stage['id']}",
            help=stage["description"],
        )
    )
    if not enabled_pipeline_stages:
        st.warning("Все этапы выключены — запуск ничего не выполнит.")

with st.sidebar:
    st.header("Параметры поиска")
    category = st.text_input("Категория товара", value="мясо говядина")
    city = st.text_input("Город", value="Екатеринбург")
    region = st.text_input("Регион", value="Свердловаская область")
    max_suppliers = st.slider("Сколько поставщиков", 3, 50, 10)
    yandex_groups_on_page = st.slider(
        "Ссылок с одной страницы Яндекса",
        min_value=1,
        max_value=100,
        value=20,
        help=(
            "Сколько ссылок-результатов брать с одной страницы выдачи Яндекса. "
            "Больше значение — шире охват и больше кандидатов на скрапинг, "
            "но дольше поиск и выше расход запросов к API."
        ),
    )
    demo_mode = st.toggle(
        "Демо-режим (без API)",
        value=True,
        help=(
            "Включено: интерфейс работает на заранее подготовленных мок-данных, "
            "реальные запросы к Яндексу и GigaChat не выполняются и ключи API не нужны — "
            "удобно для демонстрации и отладки UI без расходов. "
            "Выключено: выполняется настоящий поиск и извлечение данных через API "
            "(нужны действующие ключи)."
        ),
    )
    run = st.button("Найти поставщиков", type="primary")


def _to_dataframe(ranked) -> pd.DataFrame:
    rows = []
    for r in ranked:
        s = r.supplier
        rows.append(
            {
                "Балл": r.score,
                "Поставщик": s.name,
                "Регион": s.region or "—",
                "Телефон": s.phone or "—",
                "Email": s.email or "—",
                "Мин. заказ": s.min_order or "—",
                "Цена": s.price_info or "—",
                "Сертификаты": ", ".join(s.certificates) or "—",
                "Доставка": s.delivery or "—",
                "Источник": s.source_url,
                "Почему": "; ".join(r.reasons),
            }
        )
    return pd.DataFrame(rows)


def _initial_pipeline_state(enabled_stages: tuple[str, ...]) -> dict[str, dict]:
    enabled = set(enabled_stages)
    return {
        stage["id"]: {
            "status": "pending" if stage["id"] in enabled else "skipped",
            "payload": {},
        }
        for stage in PIPELINE_STAGES
    }


def _status_label(status: str) -> str:
    return {
        "pending": "○",
        "running": "⏳",
        "done": "✅",
        "error": "⚠️",
        "skipped": "⏭️",
    }.get(status, "○")


def _render_pipeline_plan(placeholder, state: dict[str, dict]) -> None:
    with placeholder.container():
        st.subheader("План выполнения")

        for stage in PIPELINE_STAGES:
            stage_id = stage["id"]
            entry = state.get(stage_id, {"status": "pending", "payload": {}})
            status = entry.get("status", "pending")
            payload = entry.get("payload", {})
            has_generated_queries = bool(payload.get("generated_queries"))
            expanded = status in {"running", "error"} or (
                stage_id == "generate_queries" and has_generated_queries
            )

            with st.expander(
                f"{_status_label(status)} {stage['title']}",
                expanded=expanded,
            ):
                st.caption(stage["description"])
                if payload.get("demo"):
                    st.info("Демо-режим: этап показан для структуры, реальные запросы не выполнялись.")

                if stage_id == "generate_queries":
                    queries = payload.get("generated_queries") or []
                    if queries:
                        rows = [
                            {
                                "Тип": item.type,
                                "Запрос": item.query,
                                "Зачем": item.reason,
                            }
                            for item in queries
                        ]
                        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
                    if payload.get("error"):
                        st.warning(f"Генерация запросов не удалась: {payload['error']}")

                elif stage_id == "search_pages":
                    queries = payload.get("generated_queries") or []
                    urls = payload.get("urls") or []
                    yandex_request_count = payload.get("yandex_request_count")
                    if queries:
                        st.write(f"Запросов отправлено в поиск: {len(queries)}")
                    if yandex_request_count is not None:
                        st.write(f"Платных запросов Yandex Search: {yandex_request_count}")
                    if urls:
                        st.write(f"Найдено ссылок: {len(urls)}")
                    elif status == "done":
                        st.write("Ссылки не найдены.")

                elif stage_id == "scrape_pages":
                    urls = payload.get("urls") or []
                    pages = payload.get("pages") or []
                    if urls:
                        st.write(f"Страниц поставлено в скрапинг: {len(urls)}")
                    if pages:
                        st.write(f"Успешно очищено страниц: {len(pages)}")

                elif stage_id == "extract_suppliers":
                    current = payload.get("current")
                    total = payload.get("total")
                    diagnostics = payload.get("diagnostics") or []
                    if total:
                        st.write(f"Обработано страниц: {current or 0} из {total}")
                    if diagnostics:
                        ok_count = sum(1 for item in diagnostics if item.status == "ok")
                        suppliers_count = sum(item.suppliers_found for item in diagnostics)
                        st.write(
                            f"Страниц с поставщиками: {ok_count}. "
                            f"Извлечено карточек: {suppliers_count}."
                        )
                    elif status == "done":
                        st.write("Данные поставщиков не извлечены.")

                elif stage_id == "rank_suppliers":
                    extracted_count = payload.get("extracted_count")
                    unique_count = payload.get("unique_count")
                    ranked = payload.get("ranked") or []
                    if extracted_count is not None:
                        st.write(f"Карточек до дедупликации: {extracted_count}")
                    if unique_count is not None:
                        st.write(f"Уникальных поставщиков: {unique_count}")
                    if ranked:
                        st.write(f"В финальной выдаче: {len(ranked)}")


def _render_found_links(
    placeholder, candidate_urls: list[str], scraped_urls: list[str], scraped_pages: list
) -> None:
    urls = scraped_urls or candidate_urls
    if not urls:
        return

    with placeholder.container():
        st.info(
            f"Найдено страниц: {len(urls)}. Показываю ссылки сразу, "
            "дальше идёт извлечение данных."
        )
        with st.expander("Найденные ссылки", expanded=True):
            items = "\n".join(
                (
                    f'<li><a href="{escape(url, quote=True)}" target="_blank" '
                    f'rel="noopener noreferrer">{escape(url)}</a></li>'
                )
                for url in urls
            )
            st.markdown(
                f'<div class="found-links-wrap"><ol class="found-links-list">{items}</ol></div>',
                unsafe_allow_html=True,
            )


def _usage_value(source, field: str) -> int:
    if isinstance(source, dict):
        value = source.get(field)
    else:
        value = getattr(source, field, 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _gigachat_usage_from_pipeline(state: dict[str, dict]) -> dict[str, int]:
    generate_payload = state.get("generate_queries", {}).get("payload", {})
    generate_usage = generate_payload.get("gigachat_usage") or {}

    extract_payload = state.get("extract_suppliers", {}).get("payload", {})
    diagnostics = extract_payload.get("diagnostics") or []

    prompt_tokens = _usage_value(generate_usage, "prompt_tokens")
    completion_tokens = _usage_value(generate_usage, "completion_tokens")
    total_tokens = _usage_value(generate_usage, "total_tokens")

    for item in diagnostics:
        prompt_tokens += _usage_value(item, "prompt_tokens")
        completion_tokens += _usage_value(item, "completion_tokens")
        total_tokens += _usage_value(item, "total_tokens")

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _rubles(value: float) -> str:
    return f"{value:.4f} ₽"


def _render_cost_summary(state: dict[str, dict], demo: bool) -> None:
    usage = _gigachat_usage_from_pipeline(state)
    search_payload = state.get("search_pages", {}).get("payload", {})
    yandex_request_count = _usage_value(search_payload, "yandex_request_count")

    gigachat_cost = (
        usage["total_tokens"] / 100_000_000 * GIGACHAT_PRICE_PER_100M_TOKENS
    )
    yandex_cost = (
        yandex_request_count / 1000 * YANDEX_SEARCH_PRICE_PER_1000_REQUESTS
    )
    total_cost = gigachat_cost + yandex_cost

    rows = [
        {
            "Статья": "GigaChat токены",
            "Объём": (
                f"{usage['total_tokens']} токенов "
                f"(запрос: {usage['prompt_tokens']}, ответ: {usage['completion_tokens']})"
            ),
            "Тариф": "6500 ₽ / 100 000 000 токенов",
            "Стоимость": _rubles(gigachat_cost),
        },
        {
            "Статья": "Yandex Search",
            "Объём": f"{yandex_request_count} запросов",
            "Тариф": "500 ₽ / 1000 запросов",
            "Стоимость": _rubles(yandex_cost),
        },
        {
            "Статья": "Итого",
            "Объём": "—",
            "Тариф": "—",
            "Стоимость": _rubles(total_cost),
        },
    ]

    with st.container(border=True):
        st.subheader("Стоимость запроса")
        if demo:
            st.caption("Демо-режим: реальные API-запросы не выполнялись.")
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


if run:
    req = SearchRequest(
        category=category,
        city=city or None,
        region=region or None,
        max_suppliers=max_suppliers,
    )
    pipeline_failed = False
    pipeline_state = _initial_pipeline_state(enabled_pipeline_stages)
    pipeline_placeholder = st.empty()
    _render_pipeline_plan(pipeline_placeholder, pipeline_state)

    if demo_mode:
        for stage in PIPELINE_STAGES:
            status = "done" if stage["id"] in enabled_pipeline_stages else "skipped"
            pipeline_state[stage["id"]] = {"status": status, "payload": {"demo": True}}
        _render_pipeline_plan(pipeline_placeholder, pipeline_state)
        ranked = mock_results(req)
    else:
        from supplier_finder.pipeline import PipelineConfig, find_suppliers

        pages_state: dict[str, list] = {
            "candidate_urls": [],
            "scraped_urls": [],
            "scraped_pages": [],
            "extraction_diagnostics": [],
        }
        found_links_placeholder = st.empty()

        def _on_pages_found(
            candidate_urls: list[str], scraped_urls: list[str], scraped_pages: list
        ) -> None:
            pages_state["candidate_urls"] = candidate_urls
            pages_state["scraped_urls"] = scraped_urls
            pages_state["scraped_pages"] = scraped_pages
            _render_found_links(
                found_links_placeholder, candidate_urls, scraped_urls, scraped_pages
            )

        def _on_extraction_update(diagnostics: list) -> None:
            pages_state["extraction_diagnostics"] = diagnostics

        def _on_pipeline_update(stage: str, status: str, payload: dict) -> None:
            entry = pipeline_state.setdefault(stage, {"status": "pending", "payload": {}})
            merged_payload = dict(entry.get("payload") or {})
            merged_payload.update(payload)
            pipeline_state[stage] = {"status": status, "payload": merged_payload}
            _render_pipeline_plan(pipeline_placeholder, pipeline_state)

        with st.spinner("Ищу, скрапаю страницы и собираю данные через GigaChat…"):
            try:
                ranked = asyncio.run(
                    find_suppliers(
                        req,
                        config=PipelineConfig(enabled_stages=enabled_pipeline_stages),
                        yandex_groups_on_page=yandex_groups_on_page,
                        on_pages_found=_on_pages_found,
                        on_extraction_update=_on_extraction_update,
                        on_pipeline_update=_on_pipeline_update,
                    )
                )
            except Exception as exc:
                pipeline_failed = True
                _render_found_links(
                    found_links_placeholder,
                    pages_state["candidate_urls"],
                    pages_state["scraped_urls"],
                    pages_state["scraped_pages"],
                )
                st.error(
                    "На этапе извлечения данных произошла ошибка. "
                    f"Тип ошибки: {type(exc).__name__}."
                )
                ranked = []

    if pipeline_failed:
        pass
    elif not ranked:
        st.warning("Ничего не нашлось. Попробуйте изменить категорию или регион.")
    else:
        st.success(f"Найдено поставщиков: {len(ranked)}. Отсортированы по релевантности и полноте.")
        st.dataframe(_to_dataframe(ranked), width="stretch", hide_index=True)

        st.subheader("Карточки")
        for r in ranked:
            s = r.supplier
            with st.expander(f"{s.name} — {r.score} баллов"):
                st.write(s.description or "")
                cols = st.columns(3)
                cols[0].metric("Полнота карточки", f"{int(r.completeness * 100)}%")
                cols[1].write(f"📞 {s.phone or '—'}")
                cols[2].write(f"✉️ {s.email or '—'}")
                if s.products:
                    st.write("**Товары:** " + ", ".join(s.products))
                if r.reasons:
                    st.write("**Почему стоит обратить внимание:** " + "; ".join(r.reasons))
                st.caption(f"Источник: {s.source_url}")

    _render_cost_summary(pipeline_state, demo_mode)
else:
    st.info("Задайте параметры слева и нажмите «Найти поставщиков».")
