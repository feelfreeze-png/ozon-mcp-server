"""Ежедневный снимок остатков в разрезе `sku × склад`.

**Почему снимок, а не история.** Ряда «sku на день» ни одна синхронная ручка Ozon не
отдаёт: они показывают состояние на сейчас. История FBO доступна отчётом задним числом
(пакет D4), поэтому пропущенный день остатков не так дорог, как пропущенный день
рекламы, — но и он не берётся ниоткуда, кроме как из вчерашнего снимка.

**Ручка выбрана замером, а не по документации.** 21.09.2026 опрошены все четыре:

* `/v1/analytics/stocks` — **разрез `sku × склад`**, то есть ровно то, что требует схема;
* `/v1/analytics/turnover/stocks` — `current_stock` одним числом, склада нет;
* `/v4/product/info/stocks` — ответил `400 Bad Request`, и склада тоже не даёт;
* `/v2/product/info/stocks-by-warehouse/fbs` — только FBS.

🔴 **Строка приходит не на каждый SKU.** Замерено: на 300 запрошенных вернулось 39
разных. Поэтому «снимок покрыл ассортимент» **не** означает «у каждого SKU есть строка».
Оно означает «про каждый SKU спросили», и разница здесь несущая: отсутствие строки — это
подтверждённый ноль, а неспрошенный SKU — неизвестность. Их различает
`SnapshotResult.requested` против `skus_with_rows`, а не сам ряд.

🔴 **`qty` — это `available_stock_count`.** Выбрано замером: в живой строке он несёт
штуки (3 на складе), тогда как `valid_stock_count` ненулевой у одной строки из 29. Все
прочие полтора десятка счётчиков сохраняются в `breakdown` строкой JSON: какой из них
понадобится завтра, сегодня неизвестно, а ряд невосстановим.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from . import limits, numbers, series, timezones

SOURCE = "snapshot"

#: Пауза между порциями. Замерено: ручка режет и отдаёт 429 даже на первой порции,
#: а между успешными порциями хватало десяти секунд. Берём с запасом.
CHUNK_PAUSE_S = 12.0

#: Поле, которое становится `qty`. Остальные уходят в `breakdown`.
QTY_FIELD = "available_stock_count"


@dataclass
class SnapshotResult:
    """Итог снимка. Числа, по которым и строится приёмка."""

    day_msk: str
    shop_id: str
    status: str = "failed"
    requested: int = 0
    rows_written: int = 0
    skus_with_rows: int = 0
    warehouses: int = 0
    qty_total: int = 0
    chunks_failed: int = 0
    run_id: int | None = None
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def skus_confirmed_empty(self) -> int:
        """Спросили и получили «нигде нет». Это ответ, а не пробел."""
        return self.requested - self.skus_with_rows


def _row_to_record(raw: dict, *, day: str, shop_id: str, stamped_at: str) -> dict:
    sku = numbers.parse_int(raw.get("sku"), field="sku")
    warehouse_id = raw.get("warehouse_id")
    if sku is None or warehouse_id in (None, ""):
        raise ValueError(
            f"строка остатков без sku или склада: {sorted(raw)[:8]}. Записать её "
            "значит завести строку, которую нельзя ни найти, ни сверить."
        )
    breakdown = {key: value for key, value in raw.items()
                 if key.endswith("_count") and key != QTY_FIELD}
    return {
        "date_msk": day,
        "sku": sku,
        # Ключ склада — идентификатор: имена Ozon меняет, и переименование развалило бы
        # ряд на два склада-призрака.
        "warehouse": str(warehouse_id),
        "shop_id": shop_id,
        "qty": numbers.parse_int(raw.get(QTY_FIELD), field=QTY_FIELD),
        "source": SOURCE,
        "fetched_at": stamped_at,
        "warehouse_name": raw.get("warehouse_name"),
        "cluster_name": raw.get("cluster_name"),
        "breakdown": json.dumps(breakdown, ensure_ascii=False, sort_keys=True),
    }


async def snapshot_day(
    seller: Any,
    db: aiosqlite.Connection,
    *,
    shop_id: str,
    skus: list[int],
    day: str | None = None,
    pause_s: float | None = None,
) -> SnapshotResult:
    """Снять остатки по перечню SKU и записать их в `stock_daily`.

    Повторный снимок за тот же день **перезаписывает**: остатки — состояние на момент
    съёма, и две записи за одни сутки означали бы два разных состояния под одним днём.

    Отказ отдельной порции не отменяет уже снятое, но и не проглатывается: число
    неудавшихся порций попадает в итог, а сам прогон закрывается `failed`. Частичный
    снимок, выданный за полный, — это ровно тот класс отказа, который не виден.
    """
    day = day or timezones.today_msk()
    timezones.require_plain_day(day, "day")
    pause = CHUNK_PAUSE_S if pause_s is None else pause_s

    started = timezones.now_msk_iso()
    run_id = await series.start_run(
        db, day_msk=day, shop_id=shop_id, source=SOURCE, started_at=started)
    await db.commit()

    unique = sorted(set(skus))
    result = SnapshotResult(day_msk=day, shop_id=shop_id, run_id=run_id,
                            requested=len(unique))
    records: list[dict] = []
    failures: list[str] = []

    try:
        for index, batch in enumerate(limits.chunks("analytics_stocks.skus", unique)):
            if index and pause:
                await asyncio.sleep(pause)
            stamped_at = timezones.now_msk_iso()
            try:
                payload = await seller.analytics_stocks(batch)
            except Exception as exc:
                result.chunks_failed += 1
                failures.append(f"{type(exc).__name__}: {exc}")
                continue
            for raw in payload.get("items") or []:
                records.append(_row_to_record(
                    raw, day=day, shop_id=shop_id, stamped_at=stamped_at))

        result.rows_written = await series.upsert_stock_daily(db, records)
        result.skus_with_rows = len({record["sku"] for record in records})
        result.warehouses = len({record["warehouse"] for record in records})
        result.qty_total = sum(record["qty"] or 0 for record in records)
        result.detail = {
            "спрошено sku": result.requested,
            "sku со строками": result.skus_with_rows,
            "sku с подтверждённым нулём": result.skus_confirmed_empty,
            "складов": result.warehouses,
        }
        if failures:
            result.detail["неудавшиеся порции"] = failures[:5]
            result.error = (
                f"порций не снялось: {result.chunks_failed}. Снимок неполон, и выдавать "
                "его за полный нельзя."
            )
            result.status = "failed"
        else:
            result.status = "ok"
    except BaseException as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        await series.finish_run(
            db, run_id, status="failed", finished_at=timezones.now_msk_iso(),
            rows_written=result.rows_written, error=result.error[:2000])
        raise

    await series.finish_run(
        db, run_id, status=result.status, finished_at=timezones.now_msk_iso(),
        campaigns=None, rows_written=result.rows_written,
        error=(result.error or None) and result.error[:2000])
    return result


async def coverage_against_catalogue(
    db: aiosqlite.Connection, *, shop_id: str, day: str,
) -> dict[str, Any]:
    """Сверка снимка с таблицей товаров (D2).

    Отвечает на вопрос приёмки «покрыт ли ассортимент» единственным честным способом:
    сколько `sku` из таблицы товаров попало в снимок и сколько нет. Второе число само
    по себе не тревога — остатка может не быть, — но оно обязано быть на виду.
    """
    timezones.require_plain_day(day, "day")
    async with db.execute(
        "SELECT count(DISTINCT sku) FROM product_sku WHERE shop_id = ?", (shop_id,)
    ) as cur:
        known = (await cur.fetchone())[0]
    async with db.execute(
        "SELECT count(DISTINCT sku) FROM stock_daily "
        "WHERE shop_id = ? AND date_msk = ? AND source = ?",
        (shop_id, day, SOURCE),
    ) as cur:
        covered = (await cur.fetchone())[0]
    async with db.execute(
        "SELECT count(DISTINCT s.sku) FROM stock_daily s "
        "LEFT JOIN product_sku p ON p.sku = s.sku AND p.shop_id = s.shop_id "
        "WHERE s.shop_id = ? AND s.date_msk = ? AND p.sku IS NULL",
        (shop_id, day),
    ) as cur:
        stranger = (await cur.fetchone())[0]
    return {
        "sku в таблице товаров": known,
        "sku со строкой остатка": covered,
        "sku в снимке без карточки": stranger,
    }
