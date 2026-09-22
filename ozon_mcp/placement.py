"""D4: загрузка отчёта размещения FBO в ряд остатков.

**Что это за величина.** «Кол-во экземпляров» — счёт **тарификации** за сутки, а не
измеренный остаток. Поэтому строки пишутся с `source='placement_report'`, который входит
в первичный ключ `stock_daily`: без этого бэкфилл не «смешался» бы со снимком, а молча
съел его (см. миграцию 4 в `series.py`).

**Заголовок закреплён, а не угадан.** Все 12 колонок выверены на живом файле кабинета
22.09.2026 (отчёт за 2026-08-22…2026-09-21, 3671 строка, sha256 `ca073994b0ec4a1b…`).
Порядок и написание пиннятся: молчаливая смена состава колонок — это ровно тот отказ,
который выглядит как пустой отчёт.

🔴 **Три вещи, выясненные тем же прогоном, и все три ломали сверку до замера:**

1. **`SKU` в отчёте — это `sds`-скю**, а не `fbo`. Проверено: 174 из 174 скю отчёта
   нашлись в `product_sku` и **все** с `source='sds'`; со снимком совпало 169 из 174.
   Джойн со снимком поэтому идёт напрямую по `sku`.
2. **Колонка «Склад» — ИМЯ, не идентификатор.** Совпадений с `stock_daily.warehouse`
   (идентификатор) — **ноль** из 44; с `warehouse_name` — 39 из 44. Мост строится через
   `warehouse_name`, и он заполнен у обоих источников.
3. **Идентификатора склада отчёт не печатает вовсе.** Поэтому в колонку `warehouse`
   строки бэкфилла кладётся **имя**: другого ключа у отчёта нет, а резолвить имя в
   идентификатор на загрузке значит либо потерять 5 складов из 44, у которых снимок
   идентификатора не знает, либо приписать им чужой. Различать источники в этой колонке
   умеет сам `source`, входящий в ключ.

**Даты.** Excel хранит их числом дней от 1899-12-30; колонка объявлена датой здесь, а не
угадывается по стилю ячейки (`xlsx.excel_serial_to_day`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Any

import aiosqlite

from . import series, timezones, xlsx

SOURCE = "placement_report"

#: Состав колонок, выверенный на живом файле 22.09.2026. Сверяется целиком и по порядку.
HEADER: tuple[str, ...] = (
    "Дата",
    "SKU",
    "Артикул",
    "Категория товара",
    "Описательный тип",
    "Склад",
    "Признак товара",
    "Суммарный объем в миллилитрах",
    "Кол-во экземпляров",
    "Платный объем в миллилитрах",
    "Кол-во платных экземпляров",
    "Начисленная стоимость размещения",
)

_DAY, _SKU, _OFFER, _CATEGORY, _KIND, _WAREHOUSE, _FLAG = 0, 1, 2, 3, 4, 5, 6
_VOLUME, _UNITS, _PAID_VOLUME, _PAID_UNITS, _COST = 7, 8, 9, 10, 11


class PlacementError(ValueError):
    """Файл не является отчётом размещения в известном нам виде. Причина названа."""


@dataclass
class Rejected:
    """Строка, не попавшая в ряд. Причина хранится текстом — её читает человек."""

    reason: str
    row: list[Any]


@dataclass
class ParsedReport:
    """Итог разбора. Числа отсюда идут в приёмку, а не пересказ словами."""

    rows: list[dict] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)
    days: set[str] = field(default_factory=set)
    skus: set[int] = field(default_factory=set)
    warehouses: set[str] = field(default_factory=set)
    fractional: int = 0

    @property
    def duplicates(self) -> int:
        return sum(1 for item in self.rejected if item.reason.startswith("дубль"))

    @property
    def out_of_window(self) -> int:
        return sum(1 for item in self.rejected if item.reason.startswith("дата вне окна"))


def check_header(header: list[str]) -> None:
    """Сверить заголовок целиком. Расхождение — отказ, а не повод разбирать по позициям.

    Разбор по номерам колонок пережил бы переименование молча и записал бы в `qty`
    соседнюю величину: «Платный объем в миллилитрах» стоит через одну от «Кол-во
    экземпляров», и обе — целые числа. Такую подмену не видно ни в одном итоге.
    """
    actual = tuple(header)
    if actual != HEADER:
        raise PlacementError(
            "состав колонок отчёта размещения не совпал с выверенным.\n"
            f"  ожидалось ({len(HEADER)}): {list(HEADER)}\n"
            f"  получено  ({len(actual)}): {list(actual)}\n"
            "Разбирать по номерам позиций нельзя: соседние колонки того же типа "
            "подменились бы молча."
        )


def _day_of(value: Any) -> str:
    """Excel-число → `YYYY-MM-DD`. Колонка объявлена датой, а не угадана по стилю."""
    if isinstance(value, str):
        timezones.require_plain_day(value, "Дата")
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return xlsx.excel_serial_to_day(float(value))
    raise ValueError(f"дата не читается: {value!r}")


def parse(
    data: bytes,
    *,
    shop_id: str,
    date_from: str,
    date_to: str,
    fetched_at: str | None = None,
) -> ParsedReport:
    """Разобрать XLSX отчёта размещения в строки `stock_daily`.

    Окно сверяется с запрошенным: «Ozon вернул строку за X, а запрошен Y» — тот же
    сторож, что у сборщика рекламы. Строка вне окна не отбрасывается молча, а попадает в
    `rejected` со своей причиной: молчаливое отбрасывание неотличимо от пустого отчёта.
    """
    timezones.require_plain_day(date_from, "date_from")
    timezones.require_plain_day(date_to, "date_to")
    if _date.fromisoformat(date_to) < _date.fromisoformat(date_from):
        raise PlacementError(f"окно {date_from}…{date_to} перевёрнуто")
    stamped = fetched_at or timezones.now_msk_iso()

    header, body = xlsx.table(data)
    check_header(header)

    result = ParsedReport()
    seen: set[tuple[str, int, str]] = set()
    for row in body:
        try:
            day = _day_of(row[_DAY])
            sku = int(row[_SKU])
            warehouse = str(row[_WAREHOUSE] or "").strip()
            if not warehouse:
                raise ValueError("строка без склада")
        except (TypeError, ValueError) as exc:
            result.rejected.append(Rejected(f"не разобралась: {exc}", row))
            continue

        if not (date_from <= day <= date_to):
            result.rejected.append(Rejected(
                f"дата вне окна: строка за {day}, запрошено {date_from}…{date_to}", row))
            continue

        key = (day, sku, warehouse)
        if key in seen:
            result.rejected.append(Rejected(f"дубль {key}", row))
            continue
        seen.add(key)

        # Дробное количество не округляем: колонка `qty` целая (STRICT), и округление
        # подменило бы величину молча. Значение уезжает в breakdown, qty остаётся NULL —
        # то есть «не измерено», а не «ноль».
        units = row[_UNITS]
        qty: int | None
        if isinstance(units, int) and not isinstance(units, bool):
            qty = units
        elif isinstance(units, float) and units.is_integer():
            qty = int(units)
        else:
            qty = None
            result.fractional += 1

        breakdown = {
            "offer_id": row[_OFFER],
            "category": row[_CATEGORY],
            "descriptive_type": row[_KIND],
            "flag": row[_FLAG],
            "volume_ml": row[_VOLUME],
            "paid_volume_ml": row[_PAID_VOLUME],
            "paid_units": row[_PAID_UNITS],
            "placement_cost": row[_COST],
        }
        if qty is None:
            breakdown["units_raw"] = units

        result.rows.append({
            "date_msk": day,
            "sku": sku,
            # Имя, а не идентификатор: его отчёт не печатает. См. шапку модуля.
            "warehouse": warehouse,
            "shop_id": shop_id,
            "source": SOURCE,
            "qty": qty,
            "fetched_at": stamped,
            "warehouse_name": warehouse,
            "cluster_name": None,
            "breakdown": json.dumps(breakdown, ensure_ascii=False, sort_keys=True),
        })
        result.days.add(day)
        result.skus.add(sku)
        result.warehouses.add(warehouse)

    return result


async def load(db: aiosqlite.Connection, parsed: ParsedReport) -> int:
    """Записать разобранные строки. Повтор того же окна перезаписывает, не задваивая."""
    return await series.upsert_stock_daily(db, parsed.rows)


def fbo_warehouses(parsed: ParsedReport) -> set[str]:
    """Белый список FBO-складов — множество имён из самого файла.

    Отчёт по построению содержит только FBO, значит различные значения колонки «Склад»
    за всё окно **и есть** список FBO-складов кабинета. Иначе классифицировать «товар не
    FBO» нечем: признака схемы у склада в `stock_daily` нет, а `/v1/analytics/stocks`
    отдаёт склады вперемешку.
    """
    return set(parsed.warehouses)
