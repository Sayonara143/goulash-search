"""Ранжирование поставщиков — «помоги принять решение, с кем связаться».

Считаем два сигнала:
  • completeness — насколько полно заполнена карточка (есть ли контакты, цена,
    сертификаты, доставка). Полная карточка = меньше работы менеджеру.
  • relevance — совпадает ли товар/регион с запросом.

Логика прозрачная и детерминированная (не LLM): её легко объяснить и проверить,
а к каждому поставщику прикладываем человекочитаемые причины оценки.
"""

from __future__ import annotations

from .models import RankedSupplier, SearchRequest, Supplier

# Поля, наличие которых повышает полноту карточки, и их вес.
_COMPLETENESS_FIELDS = {
    "phone": 0.20,
    "email": 0.15,
    "website": 0.10,
    "price_info": 0.15,
    "min_order": 0.10,
    "certificates": 0.15,
    "delivery": 0.10,
    "region": 0.05,
}


def _completeness(s: Supplier) -> float:
    score = 0.0
    for field, weight in _COMPLETENESS_FIELDS.items():
        value = getattr(s, field)
        if value:  # непустая строка или непустой список
            score += weight
    return round(score, 2)


def _relevance(s: Supplier, req: SearchRequest) -> tuple[float, list[str]]:
    reasons: list[str] = []
    rel = 0.0

    cat = req.category.lower()
    haystack = " ".join(
        [s.name or "", s.description or "", " ".join(s.products)]
    ).lower()
    if any(word in haystack for word in cat.split()):
        rel += 0.6
        reasons.append("товар совпадает с запросом")

    if req.region and s.region and req.region.lower() in s.region.lower():
        rel += 0.4
        reasons.append(f"работает в регионе «{req.region}»")

    return min(rel, 1.0), reasons


def rank(suppliers: list[Supplier], req: SearchRequest) -> list[RankedSupplier]:
    ranked: list[RankedSupplier] = []

    for s in suppliers:
        completeness = _completeness(s)
        relevance, reasons = _relevance(s, req)

        # Итог: 60% релевантность + 40% полнота, в шкале 0–100.
        score = round((relevance * 0.6 + completeness * 0.4) * 100, 1)

        if s.certificates:
            reasons.append(f"есть сертификаты: {', '.join(s.certificates[:3])}")
        if not s.phone and not s.email:
            reasons.append("⚠ нет прямых контактов")

        ranked.append(
            RankedSupplier(
                supplier=s,
                score=score,
                completeness=completeness,
                reasons=reasons,
            )
        )

    ranked.sort(key=lambda r: r.score, reverse=True)
    return ranked


def deduplicate(suppliers: list[Supplier]) -> list[Supplier]:
    """Убрать дубли по имени (часто один поставщик встречается на разных сайтах)."""
    seen: dict[str, Supplier] = {}
    for s in suppliers:
        key = (s.name or "").strip().lower()
        if not key:
            continue
        # Оставляем более полную карточку.
        if key not in seen or _completeness(s) > _completeness(seen[key]):
            seen[key] = s
    return list(seen.values())
