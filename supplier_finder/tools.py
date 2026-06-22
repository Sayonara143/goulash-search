"""Сбор данных: веб-поиск + скрапинг страниц.

Это «руки» сервиса. GigaChat сам в интернет не ходит, поэтому:
  1. search_suppliers() — находит страницы-кандидаты (Tavily или бесплатный DDG);
  2. scrape_page() — скачивает страницу и достаёт чистый текст + контакты.

Контакты (телефон/почта) вытягиваем регуляркой ещё до LLM — так точность выше
и дешевле, чем просить модель угадывать их из общего текста.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx
import trafilatura

from .config import settings
from .fixed_search_urls import FIXED_SUPPLIER_URLS

# --- Регулярки для контактов (российский формат телефонов + email) ---
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"
)
_CONTACT_PATH_VARIANTS = (
    "contacts",
    "contact",
    "contact-us",
    "kontakty",
    "kontakty.html",
    "contacts.html",
)
_CONTACT_KEYWORDS = (
    "контакт",
    "contacts",
    "contact",
    "связаться",
    "обратная связь",
    "телефон",
    "e-mail",
    "email",
    "адрес",
)
_MAX_EXTRACTED_TEXT_CHARS = 6000
_MAX_MERGED_TEXT_CHARS = 9000
_CONTACT_REQUEST_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Поиск
# ---------------------------------------------------------------------------
async def search_suppliers(query: str, region: str | None, limit: int) -> list[str]:
    """Вернуть список URL страниц-кандидатов по запросу."""
    if settings.use_fixed_search_urls:
        return FIXED_SUPPLIER_URLS[:limit]

    q = f"{query} поставщик оптом купить"
    if region:
        q += f" {region}"

    return await _search_web(q, limit)


async def search_suppliers_by_queries(
    queries: list[str],
    per_query_limit: int = 20,
    max_total: int | None = None,
    on_query_searched: Callable[[], None] | None = None,
) -> list[str]:
    """Вернуть URL страниц-кандидатов по нескольким готовым поисковым запросам."""
    if max_total is not None:
        max_total = max(1, max_total)

    if settings.use_fixed_search_urls:
        return FIXED_SUPPLIER_URLS[:max_total]

    clean_queries = list(dict.fromkeys(q.strip() for q in queries if q.strip()))
    if not clean_queries:
        return []

    results_per_query = per_query_limit
    urls: list[str] = []

    for query in clean_queries:
        if max_total is not None and len(urls) >= max_total:
            break
        query_limit = results_per_query
        if max_total is not None:
            query_limit = min(query_limit, max_total - len(urls))
        try:
            if on_query_searched:
                on_query_searched()
            batch = await _search_web(query, query_limit)
        except Exception:
            continue
        urls.extend(batch)
        urls = list(dict.fromkeys(urls))
        if max_total is not None:
            urls = urls[:max_total]

    return urls


async def _search_web(query: str, limit: int) -> list[str]:
    """Выполнить поиск через доступный поисковый движок."""
    if settings.yandex_search_api_key and settings.yandex_folder_id:
        return await _search_yandex(query, limit)
    if settings.tavily_api_key:
        return await _search_tavily(query, limit)
    return await _search_ddg(query, limit)


async def _search_tavily(query: str, limit: int) -> list[str]:
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": settings.tavily_api_key,
                "query": query,
                "max_results": limit,
                "search_depth": "basic",
            },
        )
        resp.raise_for_status()
        data = resp.json()
    return [item["url"] for item in data.get("results", []) if item.get("url")]


async def _search_yandex(query: str, groups_on_page: int) -> list[str]:
    """Поиск через Yandex Search API (Yandex AI Studio SDK). Нужны ключ + folderId.

    Запрашиваем выдачу в XML и парсим из неё <url>. SDK синхронный — уводим
    в поток, чтобы не блокировать event loop. Количество ссылок на странице
    передаётся из UI как `groupsOnPage`.
    """
    try:
        from yandex_ai_studio_sdk import AIStudio  # pip install yandex-ai-studio-sdk
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Установите поиск Яндекса: pip install yandex-ai-studio-sdk"
        ) from exc

    groups_on_page = max(1, min(groups_on_page, 100))

    def _run() -> list[str]:
        sdk = AIStudio(
            folder_id=settings.yandex_folder_id,
            auth=settings.yandex_search_api_key,  # SDK сам определит тип ключа
        )
        # Плоская группировка: один документ в группе => groups_on_page = число ссылок.
        search = sdk.search_api.web("RU").configure(
            search_type="ru",
            group_mode="deep",
            groups_on_page=groups_on_page,
            docs_in_group=1,
        )
        # .run возвращает байты XML; ElementTree принимает их напрямую.
        xml_bytes = search.run(query, format="xml", page=0)
        root = ElementTree.fromstring(xml_bytes)
        urls = [el.text for el in root.iter("url") if el.text]
        # Убираем дубли, сохраняя порядок.
        return list(dict.fromkeys(urls))

    return await asyncio.to_thread(_run)


async def _search_ddg(query: str, limit: int) -> list[str]:
    """Бесплатный поиск через DuckDuckGo (пакет `ddgs`). Без API-ключа."""
    try:
        from ddgs import DDGS  # pip install ddgs
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Установите поиск: pip install ddgs") from exc

    def _run() -> list[str]:
        with DDGS() as ddgs:
            hits = ddgs.text(query, region="ru-ru", max_results=limit)
            return [h["href"] for h in hits if h.get("href")]

    # ddgs синхронный — уводим в поток, чтобы не блокировать event loop.
    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# Скрапинг
# ---------------------------------------------------------------------------
class ScrapedPage:
    """Результат скрапинга одной страницы."""

    def __init__(self, url: str, text: str, phones: list[str], emails: list[str]):
        self.url = url
        self.text = text
        self.phones = phones
        self.emails = emails


def _extract_contacts(text: str) -> tuple[list[str], list[str]]:
    """Вернуть уникальные телефоны и email из HTML или видимого текста."""
    phones = list(dict.fromkeys(_PHONE_RE.findall(text)))[:3]
    emails = list(dict.fromkeys(_EMAIL_RE.findall(text)))[:3]
    return phones, emails


def _contact_candidate_urls(url: str) -> list[str]:
    """Построить типовые URL contact-страниц от корня сайта."""
    root = _site_root_url(url)
    if root is None:
        return []

    candidates = [urljoin(f"{root}/", path) for path in _CONTACT_PATH_VARIANTS]
    return [candidate for candidate in dict.fromkeys(candidates) if candidate != url]


def _site_root_url(url: str) -> str | None:
    """Вернуть scheme://host для URL, если он полный."""
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _looks_like_contact_page(page: ScrapedPage) -> bool:
    """Отсеять успешные, но нерелевантные ответы вроде кастомных заглушек."""
    if page.phones or page.emails:
        return True

    text = page.text.lower()
    return any(keyword in text for keyword in _CONTACT_KEYWORDS)


def _merge_with_contact_page(
    page: ScrapedPage, contact_page: ScrapedPage | None
) -> ScrapedPage:
    """Объединить основную страницу и страницу контактов в один текст."""
    if contact_page is None:
        return page

    text = (
        f"{page.text}\n\n"
        f"--- Дополнительная страница контактов: {contact_page.url} ---\n"
        f"{contact_page.text}"
    )[:_MAX_MERGED_TEXT_CHARS]
    phones = list(dict.fromkeys([*page.phones, *contact_page.phones]))[:3]
    emails = list(dict.fromkeys([*page.emails, *contact_page.emails]))[:3]
    return ScrapedPage(url=page.url, text=text, phones=phones, emails=emails)


def _extract_from_html(url: str, html: str) -> ScrapedPage | None:
    """Извлечь основной текст и контакты из готового HTML."""
    # trafilatura отбрасывает меню/футеры и оставляет содержательный текст.
    text = trafilatura.extract(html, include_comments=False) or ""
    if not text.strip():
        return None

    # Контакты ищем по сырому HTML (часто они в шапке/подвале, вне основного текста).
    phones, emails = _extract_contacts(html)

    # Ограничиваем объём текста, чтобы не раздувать контекст и стоимость запроса.
    return ScrapedPage(
        url=url,
        text=text[:_MAX_EXTRACTED_TEXT_CHARS],
        phones=phones,
        emails=emails,
    )


async def scrape_page(
    client: httpx.AsyncClient, url: str, timeout: float | None = None
) -> ScrapedPage | None:
    """Скачать страницу через HTTP, извлечь основной текст и контакты."""
    try:
        if timeout is None:
            resp = await client.get(url, follow_redirects=True)
        else:
            resp = await client.get(url, follow_redirects=True, timeout=timeout)
        resp.raise_for_status()
        html = resp.text
    except (httpx.HTTPError, UnicodeDecodeError):
        return None

    return _extract_from_html(url, html)


async def scrape_contact_page(
    client: httpx.AsyncClient, source_url: str
) -> ScrapedPage | None:
    """Найти и скачать первую подходящую contact-страницу для сайта."""
    timeout = min(settings.request_timeout, _CONTACT_REQUEST_TIMEOUT)
    for contact_url in _contact_candidate_urls(source_url):
        page = await scrape_page(client, contact_url, timeout=timeout)
        if page is not None and _looks_like_contact_page(page):
            return page
    return None


async def _scrape_with_playwright(urls: list[str]) -> dict[str, ScrapedPage]:
    """Отрендерить JS-страницы браузером и вернуть очищенный текст."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {}

    pages: dict[str, ScrapedPage] = {}
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=settings.user_agent)
            sem = asyncio.Semaphore(settings.max_concurrency)

            async def _render(url: str) -> None:
                async with sem:
                    page = None
                    try:
                        page = await context.new_page()
                        await page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=settings.request_timeout * 1000,
                        )
                        try:
                            await page.wait_for_load_state("networkidle", timeout=3000)
                        except Exception:
                            pass
                        html = await page.content()
                        try:
                            visible_text = await page.locator("body").inner_text(timeout=1000)
                        except Exception:
                            visible_text = ""
                    except Exception:
                        return
                    finally:
                        if page is not None:
                            await page.close()

                    scraped = _extract_from_html(url, html)
                    if scraped is not None:
                        pages[url] = scraped
                    elif visible_text.strip():
                        phones, emails = _extract_contacts(f"{html}\n{visible_text}")
                        pages[url] = ScrapedPage(
                            url=url,
                            text=visible_text.strip()[:_MAX_EXTRACTED_TEXT_CHARS],
                            phones=phones,
                            emails=emails,
                        )

            await asyncio.gather(*[_render(url) for url in urls], return_exceptions=True)
            await context.close()
            await browser.close()
    except Exception:
        return pages

    return pages


async def scrape_many(urls: list[str]) -> list[ScrapedPage]:
    """Параллельно скрапим URL; JS-страницы добираем через Playwright."""
    sem = asyncio.Semaphore(settings.max_concurrency)
    headers = {"User-Agent": settings.user_agent}

    async with httpx.AsyncClient(
        timeout=settings.request_timeout, headers=headers
    ) as client:

        async def _guarded(u: str) -> ScrapedPage | None:
            async with sem:
                return await scrape_page(client, u)

        results = await asyncio.gather(*[_guarded(u) for u in urls])

    missing_urls = [url for url, page in zip(urls, results, strict=False) if page is None]
    if missing_urls:
        rendered_pages = await _scrape_with_playwright(missing_urls)
        results = [
            page if page is not None else rendered_pages.get(url)
            for url, page in zip(urls, results, strict=False)
        ]

    async with httpx.AsyncClient(
        timeout=settings.request_timeout, headers=headers
    ) as client:

        async def _guarded_contact(u: str) -> ScrapedPage | None:
            async with sem:
                return await scrape_contact_page(client, u)

        contact_source_by_root = {
            root: url
            for url in urls
            if (root := _site_root_url(url)) is not None
        }
        contact_pages = await asyncio.gather(
            *[_guarded_contact(u) for u in contact_source_by_root.values()]
        )
        contact_page_by_root = dict(
            zip(contact_source_by_root, contact_pages, strict=False)
        )
        contact_results = [
            contact_page_by_root.get(root) if (root := _site_root_url(url)) else None
            for url in urls
        ]

    results = [
        _merge_with_contact_page(page, contact_page)
        if page is not None
        else contact_page
        for page, contact_page in zip(results, contact_results, strict=False)
    ]

    return [r for r in results if r is not None]
