"""Ежедневный сбор рекламной статистики по SKU.

**Почему это самое срочное в итерации.** `products/sku` отдаёт только сегодня и вчера.
День, не снятый вовремя, потерян навсегда — в отличие от остатков FBO, которые
бэкфиллятся отчётом за полгода. Поэтому сбор запускается с запасом после полуночи МСК и
берёт **вчера**: сегодняшние сутки ещё не закрыты.

🔴 **Путь вызова — методы клиента напрямую, мимо `call_tool`.** От этого зависит порядок
пакетов: шейпинг, усечение ответа и плоский текст `Ошибка: …` живут только на MCP-пути и
сборщика не касаются. Если решение когда-нибудь изменится и сборщик станет MCP-клиентом,
зависимость B→C возвращается.

**Три исхода, а не два.** Сбор записывает прогон в `collection_run` до того, как пойдёт в
Ozon, и закрывает его явным `ok` или `failed`. Без этого «строк за день нет» означало бы
сразу две разные вещи: расхода не было (нормально) и сбор не отработал (сутки потеряны).
По самому ряду их не различить.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from . import series, timezones

SOURCE = "products_sku"


@dataclass
class CollectResult:
    """Исход одного сбора. Читается механически, а не по тексту сообщения."""

    day_msk: str
    shop_id: str
    status: str                       # ok | failed
    campaigns: int = 0
    rows_written: int = 0
    distinct_sku: int = 0
    expense_total: float = 0.0
    error: str | None = None
    run_id: int | None = None
    unknown_fields: set[str] = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


async def collect_day(
    perf: Any,
    db: aiosqlite.Connection,
    *,
    shop_id: str,
    day: str | None = None,
    pause_s: float | None = None,
) -> CollectResult:
    """Снять рекламный срез за день и записать его в `ad_daily`.

    Повторный прогон за уже собранный день **перезаписывает** строки, а не дублирует:
    ключ ряда — `(date_msk, sku, campaign_id, shop_id)`.

    Отказ не превращается в пустой результат: он закрывает прогон статусом `failed` с
    текстом причины и поднимается наверх. Вызывающий обязан различать эти исходы —
    именно ради этого возвращается `CollectResult`, а не число строк.
    """
    day = day or timezones.yesterday_msk()
    timezones.require_plain_day(day, "day")
    started = timezones.now_msk_iso()
    run_id = await series.start_run(
        db, day_msk=day, shop_id=shop_id, source=SOURCE, started_at=started)
    await db.commit()

    result = CollectResult(day_msk=day, shop_id=shop_id, status="failed", run_id=run_id)
    try:
        listing = await perf.campaigns_all(adv_object_type="SKU")
        campaign_ids = [int(item["id"]) for item in listing["list"]]
        result.campaigns = len(campaign_ids)

        rows = await perf.statistics_products_sku_all(campaign_ids, day, pause_s=pause_s)

        prepared = []
        for row in rows:
            if row.get("_unknown"):
                result.unknown_fields.update(row["_unknown"])
            if row.get("date_msk") != day:
                raise ValueError(
                    f"Ozon вернул строку за {row.get('date_msk')!r}, а запрошен {day!r}. "
                    "Писать её в ряд нельзя: день перестанет означать день."
                )
            prepared.append({
                **{key: row.get(key) for key in series.AD_DAILY_COLUMNS
                   if key not in ("shop_id", "source", "fetched_at")},
                "shop_id": shop_id,
                "source": SOURCE,
                "fetched_at": timezones.now_msk_iso(),
            })

        result.rows_written = await series.upsert_ad_daily(db, prepared)
        result.distinct_sku = len({row["sku"] for row in prepared})
        result.expense_total = round(sum(row.get("expense") or 0.0 for row in prepared), 2)
        result.status = "ok"
    except BaseException as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        await series.finish_run(
            db, run_id, status="failed", finished_at=timezones.now_msk_iso(),
            campaigns=result.campaigns, rows_written=result.rows_written,
            error=result.error[:2000],
        )
        raise

    await series.finish_run(
        db, run_id, status="ok", finished_at=timezones.now_msk_iso(),
        campaigns=result.campaigns, rows_written=result.rows_written,
    )
    return result
