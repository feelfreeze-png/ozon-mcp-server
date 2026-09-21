"""Чтение накопленного ряда: период, разрез, агрегация.

**Зачем.** Писателя назначили, читателя — нет. У ассистента нет доступа к хранилищу, у
backend платформы нет ключей Ozon. Ряд накапливается и никем не читается.

🔴 **Покрытие идёт вместе с данными, а не вместо них.** Сумма расхода за неделю, в
которой сбор не отработал во вторник, выглядит как неделя с низким расходом. Отличить
одно от другого по самим строкам нельзя — разницу несёт журнал прогонов, поэтому каждый
ответ содержит блок `coverage`: какие дни собраны, какие провалились, каких нет вовсе.

🔴 **ДРР здесь не считается.** Это правило этапа отчёта (E3), и оно сложнее деления:
из расхода исключается режим «Оплата за заказ: все товары» — тариф 5%, ставка одна на
кабинет, рычагом агента не является. Посчитав ДРР ещё и здесь, мы завели бы в системе
две разные величины под одним именем, и какая попала в отчёт, выяснялось бы задним
числом. Отдаются слагаемые; ДРР собирает тот, кто знает правило.

⚠️ **`shop_id` обязателен.** Таблицы общие для всех арендаторов, разделение — колонкой.
Запрос без магазина прочитал бы чужой расход, и выглядел бы он как свой.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

import aiosqlite

from . import timezones

#: Порция `sku` в запросе названий.
#:
#: ⚠️ Первая редакция обосновывала её тем, что «SQLite держит 999 параметров». Замер
#: 21.09.2026 показал, что это неверно и — что важнее — **величина разная в разных
#: местах**: на машине разработки `SQLITE_LIMIT_VARIABLE_NUMBER` = 32 766 (sqlite
#: 3.50.4), в боевом контейнере — 250 000 (sqlite 3.46.1). Предел 999 исторический,
#: снят в 3.32.
#:
#: Отсюда и размер: 400 безопасно всюду. Обосновывать порцию пределом, который у
#: разработчика и на проде отличается в восемь раз, значит проверять её не там, где
#: она сработает.
_NAME_BATCH = 400

#: Разрезы. Ключ — имя наружу, значение — колонки группировки.
GROUPINGS: dict[str, tuple[str, ...]] = {
    "day": ("date_msk",),
    "sku": ("sku",),
    "campaign": ("campaign_id",),
    "sku_day": ("sku", "date_msk"),
    "campaign_day": ("campaign_id", "date_msk"),
}

#: Суммируемые величины. Складывать можно только их: средние и доли считает читатель,
#: иначе среднее от средних молча разойдётся с правдой.
SUMMED = ("expense", "views", "clicks", "to_cart", "orders", "model_orders",
          "sales", "model_sales")

MAX_ROWS = 5000


class SeriesReadError(ValueError):
    """Запрос к ряду не выполнен. Причина названа."""


def _days(date_from: str, date_to: str) -> list[str]:
    start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    if end < start:
        raise SeriesReadError(f"период {date_from}…{date_to} перевёрнут")
    return [(start + timedelta(days=offset)).isoformat()
            for offset in range((end - start).days + 1)]


def _check(shop_id: str, date_from: str, date_to: str, group_by: str) -> list[str]:
    if not shop_id:
        raise SeriesReadError(
            "не задан магазин. Таблицы ряда общие для всех арендаторов, разделение идёт "
            "колонкой shop_id — запрос без неё прочитал бы чужой расход как свой."
        )
    timezones.require_plain_day(date_from, "date_from")
    timezones.require_plain_day(date_to, "date_to")
    if group_by not in GROUPINGS:
        raise SeriesReadError(
            f"разрез {group_by!r} неизвестен; допустимы {sorted(GROUPINGS)}"
        )
    return _days(date_from, date_to)


async def coverage(
    db: aiosqlite.Connection, *, shop_id: str, date_from: str, date_to: str,
    source: str = "products_sku",
) -> dict[str, Any]:
    """Что известно про каждый день периода.

    Четыре состояния, и ни одно не равно остальным: собран; сбор провалился; сбор
    оборвался и не завершился; сбора не было вовсе. Последнее — не ноль, а пробел.
    """
    window = _days(date_from, date_to)
    async with db.execute(
        "SELECT day_msk, status FROM collection_run "
        "WHERE shop_id = ? AND source = ? AND day_msk BETWEEN ? AND ?",
        (shop_id, source, window[0], window[-1]),
    ) as cur:
        runs: dict[str, set[str]] = {}
        for day, status in await cur.fetchall():
            runs.setdefault(day, set()).add(status)

    collected, failed, unfinished, missing = [], [], [], []
    for day in window:
        statuses = runs.get(day, set())
        if not statuses:
            missing.append(day)
        elif "ok" in statuses:
            collected.append(day)
        elif "running" in statuses:
            unfinished.append(day)
        else:
            failed.append(day)

    return {
        "дней в периоде": len(window),
        "собрано": len(collected),
        "сбор провалился": failed,
        "сбор не завершён": unfinished,
        "сбора не было": missing,
        "ряд полон": not (failed or unfinished or missing),
    }


async def ad_series(
    db: aiosqlite.Connection, *, shop_id: str, date_from: str, date_to: str,
    group_by: str = "day", skus: list[int] | None = None,
    campaigns: list[int] | None = None, limit: int = MAX_ROWS,
) -> dict[str, Any]:
    """Рекламный ряд за период в заданном разрезе.

    Возвращает `coverage`, `rows` и `totals`. Пустой `rows` при полном покрытии значит
    «расхода не было»; при неполном — «мы не знаем», и блок покрытия говорит, какой
    именно случай перед вами.
    """
    window = _check(shop_id, date_from, date_to, group_by)
    keys = GROUPINGS[group_by]

    where = ["shop_id = ?", "date_msk BETWEEN ? AND ?"]
    params: list[Any] = [shop_id, window[0], window[-1]]
    if skus:
        where.append(f"sku IN ({', '.join('?' * len(skus))})")
        params.extend(int(s) for s in skus)
    if campaigns:
        where.append(f"campaign_id IN ({', '.join('?' * len(campaigns))})")
        params.extend(int(c) for c in campaigns)

    sums = ", ".join(f"sum({name}) AS {name}" for name in SUMMED)
    grouped = ", ".join(keys)
    capped = min(int(limit), MAX_ROWS)
    async with db.execute(
        f"SELECT {grouped}, {sums}, count(*) AS строк FROM ad_daily "
        f"WHERE {' AND '.join(where)} GROUP BY {grouped} "
        f"ORDER BY {'date_msk' if 'date_msk' in keys else 'sum(expense) DESC'} "
        f"LIMIT {capped + 1}",
        params,
    ) as cur:
        raw = await cur.fetchall()

    truncated = len(raw) > capped
    rows = []
    for record in raw[:capped]:
        item = dict(zip(list(keys) + list(SUMMED) + ["строк"], record))
        views, clicks = item.get("views"), item.get("clicks")
        # Доля кликов складывается из слагаемых, а не усредняется по группам:
        # среднее от средних здесь расходится с правдой и делает это тихо.
        item["ctr"] = (clicks / views) if views else None
        rows.append(item)

    async with db.execute(
        f"SELECT {sums}, count(*) FROM ad_daily WHERE {' AND '.join(where)}", params
    ) as cur:
        totals_row = await cur.fetchone()

    answer: dict[str, Any] = {
        "период": {"с": window[0], "по": window[-1], "пояс": "МСК"},
        "разрез": group_by,
        "coverage": await coverage(db, shop_id=shop_id,
                                   date_from=window[0], date_to=window[-1]),
        "rows": rows,
        "totals": dict(zip(list(SUMMED) + ["строк"], totals_row)),
    }
    if truncated:
        answer.update({"_truncated": True, "_shown": len(rows), "_limit": capped})
    # ДРР намеренно не считается — см. докстринг модуля.
    return answer


async def stock_series(
    db: aiosqlite.Connection, *, shop_id: str, date_from: str, date_to: str,
    skus: list[int] | None = None, source: str = "snapshot",
    limit: int = MAX_ROWS,
) -> dict[str, Any]:
    """Ряд остатков за период. Источник задаётся явно и не смешивается.

    ⚠️ `snapshot` и `placement_report` — разные величины: первая измеренный остаток,
    вторая величина тарификации. Складывать их нельзя, поэтому источник выбирается, а
    не объединяется.
    """
    window = _check(shop_id, date_from, date_to, "day")
    where = ["shop_id = ?", "date_msk BETWEEN ? AND ?", "source = ?"]
    params: list[Any] = [shop_id, window[0], window[-1], source]
    if skus:
        where.append(f"sku IN ({', '.join('?' * len(skus))})")
        params.extend(int(s) for s in skus)

    capped = min(int(limit), MAX_ROWS)
    async with db.execute(
        "SELECT date_msk, sku, sum(qty) AS qty, count(DISTINCT warehouse) AS складов "
        f"FROM stock_daily WHERE {' AND '.join(where)} "
        "GROUP BY date_msk, sku ORDER BY date_msk, sku "
        f"LIMIT {capped + 1}",
        params,
    ) as cur:
        raw = await cur.fetchall()

    truncated = len(raw) > capped
    rows = [dict(zip(("date_msk", "sku", "qty", "складов"), record))
            for record in raw[:capped]]
    answer: dict[str, Any] = {
        "период": {"с": window[0], "по": window[-1], "пояс": "МСК"},
        "источник": source,
        "coverage": await coverage(db, shop_id=shop_id, date_from=window[0],
                                   date_to=window[-1], source=source),
        "rows": rows,
    }
    if truncated:
        answer.update({"_truncated": True, "_shown": len(rows), "_limit": capped})
    return answer


async def ad_rows_for_report(
    db: aiosqlite.Connection, *, shop_id: str, date_from: str, date_to: str,
) -> list[dict[str, Any]]:
    """Строки ряда в разрезе `sku × кампания` — вход сборщика отчёта.

    Разрез именно такой, потому что правило ДРР различает кампании по виду расхода:
    агрегировав до товара заранее, мы потеряли бы возможность выделить тариф.
    """
    window = _check(shop_id, date_from, date_to, "sku")
    async with db.execute(
        "SELECT sku, campaign_id, sum(expense) AS expense, sum(orders) AS orders, "
        "sum(model_orders) AS model_orders, sum(sales) AS sales "
        "FROM ad_daily WHERE shop_id = ? AND date_msk BETWEEN ? AND ? "
        "GROUP BY sku, campaign_id",
        (shop_id, window[0], window[-1]),
    ) as cur:
        return [dict(zip(("sku", "campaign_id", "expense", "orders",
                          "model_orders", "sales"), row))
                for row in await cur.fetchall()]


async def stock_on_day(
    db: aiosqlite.Connection, *, shop_id: str, day: str, source: str = "snapshot",
) -> dict[int, int] | None:
    """Остаток по товарам на день. `None` — снимка за этот день НЕТ.

    🔴 Разница между `None` и пустым словарём несущая: первое значит «не проверяли»,
    второе — «проверили, остатка нет нигде». Аномалия «заказы без остатка» на первом
    обязана молчать, а не обвинять.
    """
    timezones.require_plain_day(day, "day")
    async with db.execute(
        "SELECT count(*) FROM stock_daily WHERE shop_id = ? AND date_msk = ? AND source = ?",
        (shop_id, day, source),
    ) as cur:
        if (await cur.fetchone())[0] == 0:
            return None
    async with db.execute(
        "SELECT sku, sum(qty) FROM stock_daily "
        "WHERE shop_id = ? AND date_msk = ? AND source = ? GROUP BY sku",
        (shop_id, day, source),
    ) as cur:
        return {int(sku): int(qty or 0) for sku, qty in await cur.fetchall()}


async def names_for_skus(
    db: aiosqlite.Connection, *, shop_id: str, skus: Iterable[int],
) -> dict[int, str]:
    """Названия товаров по их `sku`. В словаре только то, что нашлось.

    🔴 Отсутствие `sku` в ответе — не пустяк и не повод подставить прочерк. Оно значит
    ровно одно из двух: либо каталог не пересобран после появления товара, либо `sku`
    рекламируется, а карточки под него нет. Оба случая обязан увидеть тот, кто читает
    отчёт, поэтому словарь возвращается неполным, а достраивает его — и называет
    пропуск — вызывающий.

    Пустое имя в базе к выдаче не приравнивается к найденному: пустая строка в отчёте
    выглядит как название из пробела и читается как «название есть».
    """
    wanted = sorted({int(sku) for sku in skus if sku is not None})
    if not wanted:
        return {}
    found: dict[int, str] = {}
    for batch in (wanted[i:i + _NAME_BATCH] for i in range(0, len(wanted), _NAME_BATCH)):
        async with db.execute(
            "SELECT s.sku, p.name FROM product_sku s "
            "JOIN product p ON p.product_id = s.product_id AND p.shop_id = s.shop_id "
            f"WHERE s.shop_id = ? AND s.sku IN ({', '.join('?' * len(batch))})",
            (shop_id, *batch),
        ) as cur:
            for sku, name in await cur.fetchall():
                text = (name or "").strip()
                if text:
                    found[int(sku)] = text
    return found
