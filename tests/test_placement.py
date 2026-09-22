"""D4: разбор отчёта размещения и загрузка его в ряд остатков.

Заголовок и три факта о содержимом выверены на **живом** файле кабинета 22.09.2026
(окно 2026-08-22…2026-09-21, 3671 строка, sha256 `ca073994b0ec4a1b…`): отчёт печатает
`sds`-скю, колонка «Склад» несёт имя, а не идентификатор, идентификатора склада в файле
нет вовсе. Сам файл в репозиторий не кладётся — это выгрузка живого кабинета в публичном
форке; он лежит на ноде в `/data/artifacts` и у владельца в `~/src/ozon-mcp-artifacts`.
Здесь фикстура собирается руками по тому же заголовку.
"""

import io
import json
import zipfile

import aiosqlite
import pytest
import pytest_asyncio

from ozon_mcp import placement, series


def _sheet(rows: list[list]) -> bytes:
    """Собрать xlsx руками — так пишут не-питоновские генераторы, как и Ozon."""
    cells = []
    for index, row in enumerate(rows, start=1):
        parts = []
        for position, value in enumerate(row):
            ref = f"{chr(ord('A') + position)}{index}"
            if value is None:
                continue
            if isinstance(value, str):
                parts.append(f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>')
            else:
                parts.append(f'<c r="{ref}"><v>{value}</v></c>')
        cells.append(f'<row r="{index}">{"".join(parts)}</row>')
    sheet = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(cells)}</sheetData></worksheet>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


#: 46286 = 2026-09-21. Отсчёт от 1899-12-30, как хранит сам Excel.
DAY_SERIAL = 46286
OTHER_SERIAL = 46285


def _row(serial=DAY_SERIAL, sku=914120635, warehouse="ХАБАРОВСК_2_РФЦ", units=3):
    return [serial, sku, "бк246", "Посуда", "Ведерко", warehouse, "",
            2110, units, 0, 0, 0]


def _file(rows=None):
    return _sheet([list(placement.HEADER)] + (rows if rows is not None else [_row()]))


def _parse(data, **kwargs):
    kwargs.setdefault("shop_id", "main")
    kwargs.setdefault("date_from", "2026-09-20")
    kwargs.setdefault("date_to", "2026-09-21")
    kwargs.setdefault("fetched_at", "2026-09-22T12:00:00+03:00")
    return placement.parse(data, **kwargs)


# ── Заголовок ────────────────────────────────────────────────────────────────


def test_header_of_the_live_file_is_pinned():
    """Состав колонок — это замер, а не догадка. 12 штук, порядок значим."""
    assert len(placement.HEADER) == 12
    assert placement.HEADER[0] == "Дата"
    assert placement.HEADER[1] == "SKU"
    assert placement.HEADER[5] == "Склад"
    assert placement.HEADER[8] == "Кол-во экземпляров"


def test_renamed_column_is_refused_not_parsed_by_position():
    """🔴 Молчаливая смена состава колонок — главный отказ этого места.

    «Платный объем в миллилитрах» стоит через одну от «Кол-во экземпляров», и обе —
    целые числа. Разбор по номерам позиций записал бы в `qty` соседнюю величину, и ни
    один итог этого не показал бы.
    """
    header = list(placement.HEADER)
    header[8] = "Количество экземпляров"     # то же по смыслу, другое написание
    with pytest.raises(placement.PlacementError) as exc:
        _parse(_sheet([header, _row()]))
    assert "Количество экземпляров" in str(exc.value)
    assert "Кол-во экземпляров" in str(exc.value)


def test_column_order_change_is_refused():
    header = list(placement.HEADER)
    header[5], header[6] = header[6], header[5]
    with pytest.raises(placement.PlacementError):
        _parse(_sheet([header, _row()]))


# ── Окно, дубли, числа ───────────────────────────────────────────────────────


def test_row_outside_the_window_is_named_not_dropped():
    """«Ozon вернул строку за X, а запрошен Y» — тот же сторож, что у сборщика рекламы.

    Молчаливое отбрасывание неотличимо от пустого отчёта: в обоих случаях строк ноль.
    """
    parsed = _parse(_file([_row(serial=46000)]))     # 46000 = 2025-12-09
    assert parsed.rows == []
    assert parsed.out_of_window == 1
    reason = parsed.rejected[0].reason
    assert "2025-12-09" in reason and "2026-09-20…2026-09-21" in reason


def test_duplicate_key_keeps_the_first_and_counts_the_second():
    """Дубль `дата × sku × склад` — класс 3 автоматически.

    Без этого второй строкой молча перезаписалась бы первая: ключ `stock_daily` тот же.
    """
    parsed = _parse(_file([_row(units=3), _row(units=99)]))
    assert len(parsed.rows) == 1
    assert parsed.rows[0]["qty"] == 3
    assert parsed.duplicates == 1


def test_fractional_quantity_is_not_rounded():
    """Дробное количество не округляем: округление подменило бы величину молча.

    `qty` остаётся NULL — то есть «не измерено», а не «ноль», — а исходное значение
    уезжает в `breakdown`, откуда его можно достать глазами.
    """
    parsed = _parse(_file([_row(units=2.5)]))
    assert parsed.rows[0]["qty"] is None
    assert json.loads(parsed.rows[0]["breakdown"])["units_raw"] == 2.5
    assert parsed.fractional == 1


def test_integer_valued_float_is_kept_as_number():
    parsed = _parse(_file([_row(units=3.0)]))
    assert parsed.rows[0]["qty"] == 3


# ── Три выясненных живьём факта ──────────────────────────────────────────────


def test_warehouse_name_goes_into_both_columns():
    """Мост со снимком строится через `warehouse_name`: идентификатора отчёт не печатает.

    Замер 22.09.2026: совпадений имён отчёта с `stock_daily.warehouse`
    (идентификатором) — ноль из 44; с `warehouse_name` — 39 из 44, а после свёртки
    регистра — 42 из 44.
    """
    parsed = _parse(_file())
    row = parsed.rows[0]
    assert row["warehouse"] == "ХАБАРОВСК_2_РФЦ"
    assert row["warehouse_name"] == "ХАБАРОВСК_2_РФЦ"


def test_excel_serial_becomes_a_plain_day():
    parsed = _parse(_file())
    assert parsed.rows[0]["date_msk"] == "2026-09-21"


def test_breakdown_keeps_the_billing_counters():
    """Остальные счётчики отчёта не выбрасываются: какой понадобится завтра — неизвестно."""
    breakdown = json.loads(_parse(_file()).rows[0]["breakdown"])
    assert breakdown["offer_id"] == "бк246"
    assert breakdown["volume_ml"] == 2110
    assert "paid_units" in breakdown and "placement_cost" in breakdown


def test_fbo_whitelist_is_the_set_of_names_from_the_file():
    parsed = _parse(_file([_row(), _row(sku=1, warehouse="ОМСК_РФЦ")]))
    assert placement.fbo_warehouses(parsed) == {"ХАБАРОВСК_2_РФЦ", "ОМСК_РФЦ"}


# ── Запись в ряд ─────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db():
    connection = await aiosqlite.connect(":memory:")
    await series.migrate(connection)
    yield connection
    await connection.close()


@pytest.mark.asyncio
async def test_backfill_does_not_eat_the_snapshot_of_the_same_day(db):
    """🔴 Тот самый дефект, ради которого `source` переехал в первичный ключ.

    До миграции 4 строка бэкфилла с тем же `(дата, sku, склад, магазин)` не
    «смешивалась» со снимком, а молча съедала его: `INSERT OR REPLACE` оставлял одну
    строку, и измеренный остаток подменялся величиной тарификации. Приёмка D4 на таком
    ключе была невычислима в принципе.
    """
    await series.upsert_stock_daily(db, [{
        "date_msk": "2026-09-21", "sku": 914120635, "warehouse": "ХАБАРОВСК_2_РФЦ",
        "shop_id": "main", "source": "snapshot", "qty": 8,
        "fetched_at": "2026-09-21T13:19:41+03:00",
        "warehouse_name": "ХАБАРОВСК_2_РФЦ", "cluster_name": "Дальний Восток",
        "breakdown": "{}",
    }])
    await placement.load(db, _parse(_file()))

    async with db.execute(
        "SELECT source, qty FROM stock_daily WHERE date_msk = '2026-09-21' "
        "AND sku = 914120635 ORDER BY source"
    ) as cursor:
        got = await cursor.fetchall()
    assert got == [("placement_report", 3), ("snapshot", 8)]


@pytest.mark.asyncio
async def test_reloading_the_same_window_does_not_duplicate(db):
    parsed = _parse(_file())
    await placement.load(db, parsed)
    await placement.load(db, parsed)
    async with db.execute(
        "SELECT count(*) FROM stock_daily WHERE source = 'placement_report'"
    ) as cursor:
        assert (await cursor.fetchone())[0] == 1
