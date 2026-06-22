"""CLI-запуск сервиса.

Примеры:
    python cli.py "мука пшеничная" --region Москва
    python cli.py "упаковка для еды" --region Казань --max 5
    python cli.py "сахар" --mock          # без ключей, на демо-данных
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from supplier_finder.models import SearchRequest


def main() -> None:
    parser = argparse.ArgumentParser(description="Поиск поставщиков продуктов питания")
    parser.add_argument("category", help="Категория товара, напр. 'мука пшеничная'")
    parser.add_argument("--city", default=None, help="Город")
    parser.add_argument("--region", default=None, help="Регион")
    parser.add_argument("--max", type=int, default=8, dest="max_suppliers")
    parser.add_argument("--mock", action="store_true", help="Демо-режим без API")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Подробные логи запросов в GigaChat (с -vv — ещё и тексты промптов/ответов)",
    )
    parser.add_argument(
        "-vv",
        dest="very_verbose",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    # Логи GigaChat-запросов: -v -> INFO, -vv -> DEBUG (с промптами и ответами).
    if args.verbose or args.very_verbose:
        logging.basicConfig(
            level=logging.DEBUG if args.very_verbose else logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

    req = SearchRequest(
        category=args.category,
        city=args.city,
        region=args.region,
        max_suppliers=args.max_suppliers,
    )

    if args.mock:
        from supplier_finder.mock_data import mock_results

        ranked = mock_results(req)
    else:
        from supplier_finder.pipeline import find_suppliers

        def _on_pages_found(
            candidate_urls: list[str], scraped_urls: list[str], scraped_pages: list
        ) -> None:
            urls = scraped_urls or candidate_urls
            if not urls:
                return

            print("\nНайденные страницы:")
            for url in urls:
                print(f" - {url}")
            print()
            if scraped_pages:
                print("Содержимое найденных страниц:")
                for i, page in enumerate(scraped_pages, 1):
                    print(f"\n--- {i}. {page.url} ---")
                    print(page.text)
                print()

        try:
            ranked = asyncio.run(find_suppliers(req, on_pages_found=_on_pages_found))
        except Exception as exc:
            print(f"Ошибка на этапе извлечения данных. Тип ошибки: {type(exc).__name__}.")
            return

    if not ranked:
        print("Ничего не найдено.")
        return

    for i, r in enumerate(ranked, 1):
        s = r.supplier
        print(f"\n{i}. {s.name}  —  {r.score} баллов")
        print(f"   Регион:    {s.region or '—'}")
        print(f"   Контакты:  {s.phone or '—'} | {s.email or '—'}")
        print(f"   Мин.заказ: {s.min_order or '—'} | Цена: {s.price_info or '—'}")
        print(f"   Сертиф.:   {', '.join(s.certificates) or '—'}")
        print(f"   Источник:  {s.source_url}")
        if r.reasons:
            print(f"   Почему:    {'; '.join(r.reasons)}")


if __name__ == "__main__":
    main()
