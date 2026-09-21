"""Лимиты Ozon: одно место, и у каждого числа — происхождение.

**Зачем пометка.** Числа лимитов взяты из справочника базы знаний и в большинстве своём
**не замерены**. Тот же справочник уже один раз разошёлся с замером — на единицах
бюджета, — поэтому опираться на число, не зная его происхождения, здесь нельзя. Пометка
и есть та самая разница между «проверено» и «где-то написано».

**Зачем чанкинг у нас, а не у Ozon.** Запрос сверх лимита Ozon отвергает по-разному: где
кодом 400 с внятным текстом, где 429, а `products/sku` на неверное имя поля отвечает
`empty campaigns` — то есть отказ выглядит как пустой результат. Свой отказ называет и
лимит, и его происхождение, и его видно до того, как запрос ушёл.

⚠️ **`HANDBOOK` означает «не проверено».** Прежде чем опереться на такое число в новом
коде — замерить и переписать запись здесь, а не в комментарии рядом с вызовом.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Sequence, TypeVar

MEASURED = "замерено"
HANDBOOK = "справочник"

T = TypeVar("T")


@dataclass(frozen=True)
class Limit:
    """Число с происхождением. Без происхождения число здесь не заводится."""

    value: int
    origin: str
    what: str
    evidence: str

    @property
    def verified(self) -> bool:
        return self.origin == MEASURED


LIMITS: dict[str, Limit] = {
    "products_sku.campaigns": Limit(
        30, MEASURED, "кампаний в одном запросе products/sku",
        "живой вызов 21.09.2026: 30 campaignIds прошли за 0,20 с; потолок не нащупан, "
        "поэтому 30 — не граница API, а проверенная безопасная порция",
    ),
    "campaigns.page_size": Limit(
        100, MEASURED, "записей на странице списка кампаний",
        "живой вызов 21.09.2026: 248 SKU-кампаний обойдены тремя страницами по 100",
    ),
    "statistics.campaigns": Limit(
        10, HANDBOOK, "кампаний в одной асинхронной выгрузке",
        "справочник базы знаний; живьём не проверялось",
    ),
    "statistics.days": Limit(
        62, HANDBOOK, "дней в периоде асинхронной выгрузки",
        "справочник базы знаний; живьём не проверялось",
    ),
    "analytics_stocks.skus": Limit(
        100, HANDBOOK, "SKU в /v1/analytics/stocks",
        "справочник; замечено, что ручка перемежает 429 и HTTP 500 — сам лимит не замерен",
    ),
    "competitive_bids.skus": Limit(
        200, HANDBOOK, "SKU в запросе конкурентных ставок",
        "справочник базы знаний; живьём не проверялось",
    ),
    "cpo_min_bids.skus": Limit(
        200, HANDBOOK, "SKU в запросе фиксированных ставок CPO",
        "справочник базы знаний; живьём не проверялось",
    ),
    "campaign_products.add": Limit(
        500, HANDBOOK, "товаров, добавляемых в кампанию за раз",
        "справочник базы знаний; живьём не проверялось",
    ),
    "search_promo.skus": Limit(
        1000, HANDBOOK, "SKU во включении и отключении «оплаты за заказ»",
        "справочник базы знаний; живьём не проверялось",
    ),
    "placement_report.days": Limit(
        31, HANDBOOK, "дней в одном отчёте размещения",
        "справочник базы знаний; живьём не проверялось",
    ),
    "placement_report.per_day": Limit(
        5, HANDBOOK, "прогонов отчёта размещения в сутки",
        "справочник базы знаний; расходуется безвозвратно, поэтому проверять дорого",
    ),
}


class LimitExceeded(ValueError):
    """Запрос сверх лимита. Отвергнут нами, а не Ozon."""


def limit(key: str) -> Limit:
    try:
        return LIMITS[key]
    except KeyError:
        raise KeyError(
            f"лимит {key!r} не заведён. Числа лимитов живут одним местом — "
            "в ozon_mcp/limits.py, с пометкой происхождения."
        ) from None


def check(key: str, items: Sequence[T]) -> Sequence[T]:
    """Отвергнуть запрос сверх лимита, назвав число и его происхождение."""
    known = limit(key)
    if len(items) > known.value:
        raise LimitExceeded(
            f"{len(items)} > {known.value}: {known.what}. "
            f"Происхождение числа — {known.origin} ({known.evidence}). "
            "Запрос отвергнут до отправки: ответ Ozon сверх лимита бывает неотличим "
            "от пустого результата."
        )
    return items


def chunks(key: str, items: Sequence[T]) -> Iterator[list[T]]:
    """Разбить на порции по лимиту. Пустой вход не даёт ни одной порции."""
    size = limit(key).value
    for start in range(0, len(items), size):
        yield list(items[start:start + size])


def unverified() -> list[str]:
    """Лимиты, на которые мы опираемся, не замерив. Для отчёта, а не для украшения."""
    return sorted(key for key, value in LIMITS.items() if not value.verified)
