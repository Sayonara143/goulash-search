"""Демо-данные для запуска без ключей API.

Позволяют показать UI и логику ранжирования (режим --mock / переключатель
в интерфейсе), даже когда нет доступа к GigaChat/поиску. Удобно для записи
демонстрации.
"""

from __future__ import annotations

from .models import RankedSupplier, SearchRequest
from .ranking import rank
from .models import Supplier

_SAMPLE = [
    Supplier(
        name="АгроМука",
        description="Производитель пшеничной муки высшего и первого сорта.",
        products=["мука пшеничная в/с", "мука первого сорта", "отруби"],
        website="https://agromuka.example",
        phone="+7 495 120 34 56",
        email="sales@agromuka.example",
        source_url="https://agromuka.example",
        region="Москва",
        min_order="1 тонна",
        price_info="от 28 руб/кг",
        certificates=["ГОСТ 26574", "ХАССП"],
        delivery="Доставка по ЦФО, самовывоз со склада в Подольске",
    ),
    Supplier(
        name="ЗерноТрейд",
        description="Оптовые поставки муки и крупы от производителей.",
        products=["мука пшеничная", "манная крупа"],
        website="https://zernotrade.example",
        phone="+7 812 700 11 22",
        source_url="https://zernotrade.example",
        region="Санкт-Петербург",
        min_order="500 кг",
        certificates=["ТР ТС 021/2011"],
        delivery="Доставка по СЗФО",
    ),
    Supplier(
        name="МелькомПром",
        description="Мельничный комбинат полного цикла.",
        products=["мука пшеничная в/с", "мука ржаная"],
        email="info@melkomprom.example",
        source_url="https://melkomprom.example/catalog",
        region="Москва",
        price_info="по запросу",
        certificates=["ГОСТ Р", "ISO 22000"],
    ),
    Supplier(
        name="ОптБакалея",
        description="Дистрибьютор бакалейной продукции.",
        products=["мука", "сахар", "соль"],
        phone="+7 495 555 00 99",
        source_url="https://optbakaleya.example",
        region="Москва",
    ),
]


def mock_results(req: SearchRequest) -> list[RankedSupplier]:
    return rank(_SAMPLE, req)[: req.max_suppliers]
