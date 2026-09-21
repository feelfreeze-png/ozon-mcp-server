"""Таблица соответствия `product_id ↔ offer_id ↔ sku`.

**Зачем.** Реклама оперирует `sku`, карточка — `product_id` и `offer_id`. Без таблицы
рекламную строку не с чем связать: ни имени товара, ни артикула, ни остатков.

🔴 **У карточки несколько `sku`.** Поэтому ключ таблицы товаров — `product_id`, а `sku`
живёт отдельной строкой в `product_sku`. Соответствие `sku → product_id` однозначно,
обратное — нет, и код, считающий иначе, потеряет часть рекламной статистики.

⚠️ **Замерено полностью 21.09.2026:** 887 карточек, 1011 строк `sku`, из них **129
карточек несут по два** — `sds` плюс `fbo` либо `fbs`. Справочник обещал 124 из 856,
то есть сошёлся с точностью до изменений ассортимента. Ни на какую долю код не
опирается: он читает то, что пришло.

⚠️ Осторожно с выборкой: первая страница из 100 карточек дала две схемы у всех ста, и
принять это за долю по кабинету было бы ошибкой — страница упорядочена не случайно.
Доля считается по полному обходу, а не по первой сотне.

🔴 **Множественность приходит из `sources`, а не из `/v3/product/list`.** Тот отдаёт по
одному `sku` на карточку. Таблица, собранная только по нему, выглядит полной, будучи
неполной, — и структурная приёмка (суммы сходятся, ключи уникальны) это пропускает.
Именно так первая редакция этого модуля насчитала ноль карточек с двумя `sku`.

🔴 **`visibility="ALL"` архивные НЕ отдаёт.** Замерено: 856 против 31, пересечение
нулевое. Полный ассортимент — объединение двух выборок, и «ALL» здесь означает «все
неархивные», а не «все». Имя поля обещает больше, чем отдаёт, и проверить это можно
только вторым запросом.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from . import limits, numbers, timezones

VISIBILITIES = ("ALL", "ARCHIVED")

#: Размер страницы обхода `/v3/product/list`. Курсорный, не постраничный.
PAGE_SIZE = 1000

#: Пауза между страницами. Ручка не из режущих, но обход идёт по всему ассортименту.
PAGE_PAUSE_S = 1.0

#: Пауза между порциями `/v3/product/info/list`.
INFO_PAUSE_S = 1.0


@dataclass
class CatalogueResult:
    """Итог сборки. Числа, а не сообщение: по ним и строится приёмка."""

    shop_id: str
    products: int = 0
    skus: int = 0
    archived: int = 0
    active: int = 0
    duplicated_sku: int = 0
    multi_sku_products: int = 0
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


async def fetch_products(seller: Any, *, visibility: str) -> list[dict]:
    """Обойти `/v3/product/list` курсором до конца одной выборки.

    Оракул обхода — `total` из ответа. Курсор, оборвавшийся молча, выдал бы ассортимент
    меньшим, чем он есть, и отличить это от «мало товаров» было бы нечем.
    """
    collected: list[dict] = []
    last_id = ""
    total: int | None = None
    pages = 0
    while True:
        payload = await seller.product_list(
            limit=PAGE_SIZE, last_id=last_id, visibility=visibility)
        result = payload.get("result") or payload
        items = result.get("items") or []
        collected.extend(items)
        pages += 1
        reported = numbers.parse_int(result.get("total"), field="total")
        if reported is not None:
            total = reported
        cursor = result.get("last_id") or ""
        if not items or not cursor or (total is not None and len(collected) >= total):
            break
        last_id = cursor
        if PAGE_PAUSE_S:
            await asyncio.sleep(PAGE_PAUSE_S)

    if total is None:
        raise ValueError(
            f"/v3/product/list ({visibility}) не вернул total — полноту обхода сверять "
            "нечем, а недобор выглядит как маленький ассортимент."
        )
    if len(collected) != total:
        raise ValueError(
            f"/v3/product/list ({visibility}): собрано {len(collected)} из {total} "
            f"заявленных за {pages} страниц."
        )
    return collected


def merge_visibilities(active: list[dict], archived: list[dict]) -> list[dict]:
    """Объединить две выборки, пометив архивные.

    ⚠️ Пересечение замерено нулевым (856 и 31). Если оно вдруг появится, это значит,
    что смысл `visibility` изменился, и узнать об этом надо сразу, а не по расхождению
    итогов через месяц.
    """
    by_id: dict[int, dict] = {}
    for item in active:
        product_id = numbers.parse_int(item.get("product_id"), field="product_id")
        by_id[product_id] = {**item, "archived": 0}
    overlap = []
    for item in archived:
        product_id = numbers.parse_int(item.get("product_id"), field="product_id")
        if product_id in by_id:
            overlap.append(product_id)
        by_id[product_id] = {**item, "archived": 1}
    if overlap:
        raise ValueError(
            f"выборки ALL и ARCHIVED пересеклись по {len(overlap)} товарам "
            f"({overlap[:5]}…). Замерено, что пересечение нулевое — значит смысл "
            "visibility изменился, и объединение двух выборок больше не даёт "
            "ассортимент ровно один раз."
        )
    return list(by_id.values())


#: Схемы, которые Ozon называет в `sources`. Неизвестная не отбрасывается молча —
#: она попадает в `CatalogueResult.detail`, потому что схема управляет тем, какие
#: остатки и какая реклама относятся к этому `sku`.
KNOWN_SOURCES = frozenset({"sds", "fbo", "fbs"})


def extract_skus(item: dict) -> list[tuple[int, str | None]]:
    """Достать все `sku` карточки с пометкой схемы.

    🔴 **Множественность живёт в поле `sources` из `/v3/product/info/list`**, а не в
    отдельных полях `fbo_sku`/`sds_sku` и не в `/v3/product/list`. Последний отдаёт по
    одному `sku` на карточку, и собранная только по нему таблица выглядит полной,
    будучи неполной — ровно тот случай, когда проверка зеленеет не по той причине.

    ⚠️ Замерено полным обходом 21.09.2026: 129 карточек из 887 несут по два `sku`.
    Код ни на какую долю не опирается — он читает то, что пришло.
    """
    found: list[tuple[int, str | None]] = []
    seen: set[int] = set()
    for entry in item.get("sources") or []:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("sku")
        if raw in (None, "", 0):
            continue
        value = numbers.parse_int(raw, field="sources[].sku")
        if not value or value in seen:
            continue
        seen.add(value)
        scheme = entry.get("source") or None
        # Незнакомая схема не роняет пересборку: уронить её значит остаться вообще без
        # таблицы товаров из-за одного нового значения у Ozon. Схема записывается
        # пустой, а её имя уходит в `detail` — неполнота названа, а не проглочена.
        found.append((value, scheme if scheme in KNOWN_SOURCES else None))

    # Запасной путь: карточка без `sources` (или ответ только из `/v3/product/list`).
    if not found:
        raw = item.get("sku")
        if raw not in (None, "", 0):
            value = numbers.parse_int(raw, field="sku")
            if value:
                found.append((value, None))
    return found


async def enrich_with_sources(
    seller: Any, products: list[dict], *, result: CatalogueResult | None = None,
) -> list[dict]:
    """Дочитать `sources` из `/v3/product/info/list` порциями.

    Порция и пауза — из `limits`, с записанным происхождением. Карточка, по которой
    второй источник не ответил, **не** остаётся с одним `sku` молча: её `product_id`
    попадает в `detail`, потому что молчаливая неполнота здесь неотличима от полноты.
    """
    by_id = {numbers.parse_int(p.get("product_id"), field="product_id"): p
             for p in products}
    ids = sorted(by_id)
    enriched: set[int] = set()
    unknown_sources: set[str] = set()

    for index, batch in enumerate(limits.chunks("product_info.ids", ids)):
        if index and INFO_PAUSE_S:
            await asyncio.sleep(INFO_PAUSE_S)
        payload = await seller.product_info_list(batch)
        rows = payload.get("result", payload)
        rows = rows.get("items", rows) if isinstance(rows, dict) else rows
        for row in rows or []:
            product_id = numbers.parse_int(
                row.get("id") or row.get("product_id"), field="product_id")
            if product_id not in by_id:
                continue
            by_id[product_id]["sources"] = row.get("sources") or []
            for entry in by_id[product_id]["sources"]:
                name = (entry or {}).get("source")
                if name and name not in KNOWN_SOURCES:
                    unknown_sources.add(name)
            enriched.add(product_id)

    if result is not None:
        missing = sorted(set(ids) - enriched)
        if missing:
            result.detail["без второго источника"] = missing[:20]
            result.detail["без второго источника, всего"] = len(missing)
        if unknown_sources:
            result.detail["незнакомые схемы"] = sorted(unknown_sources)
    return list(by_id.values())


async def rebuild(
    seller: Any, db: aiosqlite.Connection, *, shop_id: str,
) -> CatalogueResult:
    """Пересобрать таблицу товаров и их `sku`.

    Обновление полное, а не разностное: карточка, исчезнувшая из кабинета, обязана
    исчезнуть и здесь, иначе реклама будет связываться с товаром, которого нет.
    """
    result = CatalogueResult(shop_id=shop_id)
    active = await fetch_products(seller, visibility="ALL")
    archived = await fetch_products(seller, visibility="ARCHIVED")
    merged = merge_visibilities(active, archived)

    result.active = len(active)
    result.archived = len(archived)
    result.products = len(merged)

    # Второй источник обязателен: `/v3/product/list` отдаёт по одному `sku` на
    # карточку, а схемы (`sds`, `fbo`, `fbs`) приходят только в `sources` из
    # `/v3/product/info/list`. Без него таблица выглядит полной, будучи неполной.
    merged = await enrich_with_sources(seller, merged, result=result)

    now = timezones.now_msk_iso()
    products = []
    sku_rows = []
    seen_sku: dict[int, int] = {}
    for item in merged:
        product_id = numbers.parse_int(item.get("product_id"), field="product_id")
        products.append((product_id, shop_id, item.get("offer_id"), item.get("name"),
                         int(item.get("archived", 0)), now))
        skus = extract_skus(item)
        if len(skus) > 1:
            result.multi_sku_products += 1
        for sku, source in skus:
            if sku in seen_sku and seen_sku[sku] != product_id:
                result.duplicated_sku += 1
            seen_sku[sku] = product_id
            sku_rows.append((product_id, shop_id, sku, source))

    await db.execute("BEGIN IMMEDIATE")
    try:
        await db.execute("DELETE FROM product_sku WHERE shop_id = ?", (shop_id,))
        await db.execute("DELETE FROM product WHERE shop_id = ?", (shop_id,))
        await db.executemany(
            "INSERT INTO product (product_id, shop_id, offer_id, name, archived, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", products)
        await db.executemany(
            "INSERT INTO product_sku (product_id, shop_id, sku, source) VALUES (?, ?, ?, ?)",
            sku_rows)
        await db.execute("COMMIT")
    except BaseException:
        import contextlib
        with contextlib.suppress(Exception):
            await db.execute("ROLLBACK")
        raise

    result.skus = len(sku_rows)
    # ⚠️ Именно `update`, а не присваивание: в `detail` уже лежат названные аномалии
    # второго источника (карточки без `sources`, незнакомые схемы). Присваивание их
    # стирало — и пересборка выглядела безупречной ровно тогда, когда было что сказать.
    result.detail.update({
        "товаров активных": result.active,
        "товаров архивных": result.archived,
        "карточек с несколькими sku": result.multi_sku_products,
    })
    return result


async def unresolved_skus(
    db: aiosqlite.Connection, *, shop_id: str, day: str,
) -> list[int]:
    """`sku` из рекламы, которым не нашлось товара.

    Пустой список означает «сверено, всё нашлось». Непустой — что таблица отстала от
    кабинета: реклама крутится на товаре, которого мы не знаем.
    """
    timezones.require_plain_day(day, "day")
    async with db.execute(
        "SELECT DISTINCT a.sku FROM ad_daily a "
        "LEFT JOIN product_sku p ON p.sku = a.sku AND p.shop_id = a.shop_id "
        "WHERE a.shop_id = ? AND a.date_msk = ? AND p.sku IS NULL",
        (shop_id, day),
    ) as cur:
        return [row[0] for row in await cur.fetchall()]


__all__ = [
    "CatalogueResult", "KNOWN_SOURCES", "PAGE_SIZE", "VISIBILITIES",
    "enrich_with_sources", "extract_skus", "fetch_products", "merge_visibilities",
    "rebuild", "unresolved_skus",
]
