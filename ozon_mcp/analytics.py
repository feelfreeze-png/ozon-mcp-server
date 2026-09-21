"""Знаменатель отчёта: общие заказы по товарам из `/v1/analytics/data`.

**Зачем.** «Доля рекламных заказов в общих» требует знаменателя, которого в рекламных
методах нет вовсе. Берётся он здесь — измерением `sku × day`.

🔴 **Эндпоинт молча выбрасывает неизвестные метрики и измерения.** Замерено 21.09.2026,
и последствие хуже, чем «пришло меньше»: метрики возвращаются **массивом по позициям**, а
выброшенное не оставляет дырку — остальные сдвигаются влево.

Замер на `totals` за 18–20.09::

    запрошено ["ordered_units", "revenue"]  → [177, 415580]
    запрошено ["НЕТ_ТАКОЙ",     "revenue"]  → [415580]

Вызывающий, читающий `metrics[0]` как заказы, получил бы **415580 вместо 177** —
выручку под видом числа заказов, в две с лишним тысячи раз больше. Не ошибка, не пустота:
неверное число под правильным именем, и ни один признак на это не указывает. Код ответа
остаётся 200.

Измерения выбрасываются так же: `["sku", "выдуманное"]` вернуло строки с одним измерением.

**Отсюда три решения.**

1. Белый список снят **опытом**, а не взят из документации: каждый кандидат проверялся в
   паре с заведомо рабочей метрикой — вернулось две, значит жив.
2. Состав ответа сверяется с запросом. Запрошено N — обязано вернуться N, иначе отказ.
3. Наружу метрики уходят **словарём по именам**, а не массивом. Тогда сдвиг перестаёт
   быть возможным в принципе, а не только обнаруживаемым.

⚠️ Измерение `day` **уже нарезано по Москве** — переводить его нельзя
(`timezones.FIELD_KINDS`).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from . import numbers, timezones

#: Метрики, принимаемые эндпоинтом. Снято живым замером 21.09.2026: каждая шла в паре
#: с `ordered_units`, и обе вернулись. Значения за неделю ненулевые у всех девяти.
#:
#: ⚠️ Поправка к нашей же документации: `hits_view`, `session_view` и
#: `position_category` записаны в CLAUDE.md как снятые Ozon. Замер это не подтверждает —
#: они принимаются и отдают осмысленные числа (496 204 показа, 233 629 сессий,
#: позиция 94,3 за 14–20.09).
METRICS: frozenset[str] = frozenset({
    "ordered_units",        # ЗНАМЕНАТЕЛЬ отчёта
    "revenue",
    "delivered_units",
    "returns",
    "cancellations",
    "hits_view",
    "session_view",
    "position_category",
    "conv_tocart",
})

#: Измерения, принимаемые эндпоинтом. Тот же замер.
DIMENSIONS: frozenset[str] = frozenset({
    "sku", "spu", "day", "week", "month", "category1", "brand",
})

#: Проверенные отказы — чтобы не проверять их заново и не принимать за опечатку.
KNOWN_REJECTED: dict[str, str] = {
    "adv_view_pdp": "метрика: выброшена молча (вернулась одна из двух), 21.09.2026",
    "modelid": "измерение: отвергнуто кодом 400, 21.09.2026",
}

#: Страница обхода. Оракула полноты у этого эндпоинта НЕТ — в ответе нет числа строк,
#: только суммы метрик. Поэтому обход идёт до страницы короче лимита и ограничен сверху.
PAGE_SIZE = 1000
MAX_PAGES = 50
PAGE_PAUSE_S = 3.0


class AnalyticsContractError(ValueError):
    """Запрос или ответ разошлись с проверенным контрактом."""


@dataclass
class AnalyticsRows:
    """Строки аналитики. Метрики — словарём, чтобы сдвиг был невозможен."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    totals: dict[str, float] = field(default_factory=dict)
    pages: int = 0
    truncated: bool = False

    def __len__(self) -> int:
        return len(self.rows)


def check_request(metrics: list[str], dimensions: list[str]) -> None:
    """Отвергнуть неизвестное имя ДО отправки.

    Ответ на такой запрос придёт с кодом 200 и будет выглядеть данными, поэтому ловить
    опечатку после отправки — значит ловить её в цифрах отчёта.
    """
    if not metrics:
        raise AnalyticsContractError("не запрошено ни одной метрики")
    if not dimensions:
        raise AnalyticsContractError("не запрошено ни одного измерения")
    for name, allowed, kind in ((m, METRICS, "метрика") for m in metrics):
        if name not in allowed:
            hint = KNOWN_REJECTED.get(name)
            raise AnalyticsContractError(
                f"{kind} {name!r} не в проверенном списке. "
                + (f"Известно: {hint}. " if hint else "")
                + "Эндпоинт выбрасывает неизвестные имена МОЛЧА, с кодом 200, сдвигая "
                "остальные значения влево — проверять после отправки поздно."
            )
    for name in dimensions:
        if name not in DIMENSIONS:
            hint = KNOWN_REJECTED.get(name)
            raise AnalyticsContractError(
                f"измерение {name!r} не в проверенном списке. "
                + (f"Известно: {hint}. " if hint else "")
                + "Неизвестное измерение выбрасывается молча."
            )


def _shape(payload: Any, metrics: list[str], dimensions: list[str]) -> AnalyticsRows:
    result = payload.get("result", payload) if isinstance(payload, dict) else {}
    raw_rows = result.get("data") or []
    out = AnalyticsRows()

    for index, raw in enumerate(raw_rows):
        got_metrics = raw.get("metrics")
        got_dimensions = raw.get("dimensions")
        if not isinstance(got_metrics, list) or len(got_metrics) != len(metrics):
            raise AnalyticsContractError(
                f"строка {index}: запрошено метрик {len(metrics)} {metrics}, вернулось "
                f"{len(got_metrics) if isinstance(got_metrics, list) else got_metrics!r}. "
                "Значения идут массивом по позициям, поэтому недостача не оставляет "
                "дырку — остальные сдвигаются, и число читается под чужим именем."
            )
        if not isinstance(got_dimensions, list) or len(got_dimensions) != len(dimensions):
            raise AnalyticsContractError(
                f"строка {index}: запрошено измерений {len(dimensions)} {dimensions}, "
                f"вернулось {len(got_dimensions) if isinstance(got_dimensions, list) else got_dimensions!r}"
            )
        out.rows.append({
            "dimensions": {
                name: {"id": entry.get("id"), "name": entry.get("name") or None}
                for name, entry in zip(dimensions, got_dimensions)
            },
            "metrics": dict(zip(metrics, got_metrics)),
        })

    totals = result.get("totals")
    if isinstance(totals, list):
        if len(totals) != len(metrics):
            raise AnalyticsContractError(
                f"итоги: запрошено метрик {len(metrics)}, в totals {len(totals)}. "
                "Замерено: выброшенная метрика исчезает и отсюда, сдвигая остальные."
            )
        out.totals = dict(zip(metrics, totals))
    return out


async def fetch(
    seller: Any, *, date_from: str, date_to: str,
    metrics: list[str], dimensions: list[str],
    limit: int = PAGE_SIZE, offset: int = 0,
) -> AnalyticsRows:
    """Один запрос с проверкой состава ответа."""
    timezones.require_plain_day(date_from, "date_from")
    timezones.require_plain_day(date_to, "date_to")
    check_request(metrics, dimensions)
    payload = await seller.analytics_data(
        date_from, date_to, metrics, dimensions, limit=limit, offset=offset)
    shaped = _shape(payload, metrics, dimensions)
    shaped.pages = 1
    return shaped


async def fetch_all(
    seller: Any, *, date_from: str, date_to: str,
    metrics: list[str], dimensions: list[str],
    page_size: int = PAGE_SIZE, pause_s: float | None = None,
) -> AnalyticsRows:
    """Обойти все страницы.

    ⚠️ **Оракула полноты здесь нет.** В отличие от списка кампаний, где `total` из
    ответа задаёт число записей, этот эндпоинт отдаёт только суммы метрик. Поэтому
    признак конца — страница короче лимита, а сверху стоит потолок в `MAX_PAGES`:
    упёршись в него, обход помечает результат усечённым, а не выдаёт за полный.
    """
    pause = PAGE_PAUSE_S if pause_s is None else pause_s
    collected = AnalyticsRows()
    offset = 0
    for page in range(MAX_PAGES):
        if page and pause:
            await asyncio.sleep(pause)
        chunk = await fetch(seller, date_from=date_from, date_to=date_to,
                            metrics=metrics, dimensions=dimensions,
                            limit=page_size, offset=offset)
        collected.rows.extend(chunk.rows)
        collected.pages += 1
        if page == 0:
            collected.totals = chunk.totals
        if len(chunk.rows) < page_size:
            return collected
        offset += page_size

    collected.truncated = True
    return collected


async def orders_by_sku_day(
    seller: Any, *, date_from: str, date_to: str,
    page_size: int = PAGE_SIZE, pause_s: float | None = None,
) -> dict[tuple[int, str], int]:
    """Знаменатель отчёта: `(sku, московские сутки) → общее число заказов`.

    ⚠️ `day` приходит уже московским — переводить нельзя. Здесь он берётся как есть,
    но формат проверяется: метка времени вместо суток развалила бы ключ.
    """
    data = await fetch_all(
        seller, date_from=date_from, date_to=date_to,
        metrics=["ordered_units"], dimensions=["sku", "day"],
        page_size=page_size, pause_s=pause_s)
    if data.truncated:
        raise AnalyticsContractError(
            f"обход аналитики упёрся в потолок {MAX_PAGES} страниц и неполон. "
            "Оракула полноты у этого эндпоинта нет, поэтому усечённый результат "
            "выдавать за знаменатель нельзя — доля получится завышенной."
        )
    out: dict[tuple[int, str], int] = {}
    for row in data.rows:
        sku = numbers.parse_int(row["dimensions"]["sku"]["id"], field="sku")
        day = timezones.require_plain_day(str(row["dimensions"]["day"]["id"]), "day")
        value = numbers.parse_int(row["metrics"]["ordered_units"], field="ordered_units")
        if sku is None:
            continue
        out[(sku, day)] = (out.get((sku, day)) or 0) + (value or 0)
    return out
