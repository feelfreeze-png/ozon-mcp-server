"""D3: ежедневный снимок остатков в разрезе `sku × склад`.

**Приёмка пакета:** снимок покрывает весь ассортимент (сверка с D2); повтор в тот же
день не задваивает.

🔴 **«Покрывает» здесь означает «про каждый SKU спросили», а не «у каждого есть строка».**
Замерено 21.09.2026: на 300 запрошенных SKU ручка вернула 39 разных. Отсутствие строки —
подтверждённый ноль; неспрошенный SKU — неизвестность. Приёмка, считающая строки,
объявила бы ассортимент покрытым на восьмую часть или, наоборот, потребовала бы строк,
которых не бывает.
"""

import json

import pytest
import pytest_asyncio

from ozon_mcp import catalogue, limits, series, stocks, timezones as tz


class FakeSeller:
    """Ручка остатков: отдаёт заранее заданные строки и считает порции."""

    def __init__(self, rows_by_sku=None, fail_chunks=(), sources=None):
        self.rows_by_sku = rows_by_sku or {}
        self.fail_chunks = set(fail_chunks)
        self.sources = dict(sources or {})
        self.chunks = []

    async def analytics_stocks(self, skus, **kwargs):
        index = len(self.chunks)
        self.chunks.append(tuple(skus))
        if index in self.fail_chunks:
            raise RuntimeError(f"429 на порции {index}")
        items = []
        for sku in skus:
            items.extend(self.rows_by_sku.get(sku, []))
        return {"items": items}


def _stock_row(sku, warehouse_id, qty, name="ХАБАРОВСК_2_РФЦ", cluster="Дальний Восток"):
    return {
        "sku": sku, "warehouse_id": warehouse_id, "warehouse_name": name,
        "cluster_name": cluster, "available_stock_count": qty,
        "valid_stock_count": 0, "transit_stock_count": 2,
        "return_to_seller_stock_count": 1, "placement_zone": "SORT",
    }


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    monkeypatch.setattr(stocks, "CHUNK_PAUSE_S", 0)
    monkeypatch.setattr(catalogue, "PAGE_PAUSE_S", 0)
    monkeypatch.setattr(catalogue, "INFO_PAUSE_S", 0)


@pytest_asyncio.fixture
async def db(tmp_path):
    conn = await series.open_db(tmp_path)
    yield conn
    await conn.close()


async def _rows(conn, sql, *args):
    async with conn.execute(sql, args) as cur:
        return [tuple(r) for r in await cur.fetchall()]


# ── Миграция 3: разрез по складу ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stock_daily_carries_the_warehouse_breakdown(db):
    """Миграция 3. Без имени склада и разбора остатка ряд нечитаем."""
    async with db.execute("PRAGMA table_info(stock_daily)") as cur:
        columns = {row[1] for row in await cur.fetchall()}
    assert {"warehouse_name", "cluster_name", "breakdown"} <= columns


# ── Снимок ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_snapshot_writes_one_row_per_sku_and_warehouse(db):
    day = tz.today_msk()
    seller = FakeSeller({1: [_stock_row(1, 100, 3), _stock_row(1, 200, 5, "КАЗАНЬ_РФЦ")],
                         2: [_stock_row(2, 100, 0)]})
    result = await stocks.snapshot_day(seller, db, shop_id="main", skus=[1, 2], day=day)

    assert result.ok
    assert result.rows_written == 3
    assert result.skus_with_rows == 2
    assert result.warehouses == 2
    assert result.qty_total == 8
    assert await _rows(db, "SELECT count(*) FROM stock_daily") == [(3,)]


@pytest.mark.asyncio
async def test_warehouse_key_is_the_id_not_the_name(db):
    """🔴 Имена складов Ozon меняет; переименование развалило бы ряд на два призрака."""
    day = tz.today_msk()
    seller = FakeSeller({1: [_stock_row(1, 100, 3, name="СТАРОЕ_ИМЯ")]})
    await stocks.snapshot_day(seller, db, shop_id="main", skus=[1], day=day)

    seller.rows_by_sku[1] = [_stock_row(1, 100, 4, name="НОВОЕ_ИМЯ")]
    await stocks.snapshot_day(seller, db, shop_id="main", skus=[1], day=day)

    assert await _rows(db, "SELECT warehouse, warehouse_name, qty FROM stock_daily") == [
        ("100", "НОВОЕ_ИМЯ", 4)]


@pytest.mark.asyncio
async def test_repeat_on_the_same_day_does_not_double(db):
    """Приёмка: повтор в тот же день перезаписывает.

    Остаток — состояние на момент съёма. Две записи за одни сутки означали бы два
    разных состояния под одним днём, и какое из них верное, узнать было бы нельзя.
    """
    day = tz.today_msk()
    seller = FakeSeller({1: [_stock_row(1, 100, 3)]})
    first = await stocks.snapshot_day(seller, db, shop_id="main", skus=[1], day=day)
    second = await stocks.snapshot_day(seller, db, shop_id="main", skus=[1], day=day)

    assert first.rows_written == second.rows_written == 1
    assert await _rows(db, "SELECT count(*) FROM stock_daily") == [(1,)]


@pytest.mark.asyncio
async def test_the_full_breakdown_is_kept(db):
    """Полтора десятка счётчиков сохраняются: ряд невосстановим, а нужный неизвестен."""
    day = tz.today_msk()
    seller = FakeSeller({1: [_stock_row(1, 100, 3)]})
    await stocks.snapshot_day(seller, db, shop_id="main", skus=[1], day=day)

    (raw,) = await _rows(db, "SELECT breakdown FROM stock_daily")
    breakdown = json.loads(raw[0])
    assert breakdown["transit_stock_count"] == 2
    assert breakdown["return_to_seller_stock_count"] == 1
    assert "available_stock_count" not in breakdown, "это поле уехало в qty"


@pytest.mark.asyncio
async def test_qty_is_the_available_count(db):
    """Выбрано замером: valid_stock_count ненулевой у одной строки из 29."""
    day = tz.today_msk()
    row = _stock_row(1, 100, 7)
    row["valid_stock_count"] = 999
    await stocks.snapshot_day(FakeSeller({1: [row]}), db, shop_id="main",
                              skus=[1], day=day)
    assert await _rows(db, "SELECT qty FROM stock_daily") == [(7,)]


@pytest.mark.asyncio
async def test_chunking_respects_the_measured_limit(db):
    day = tz.today_msk()
    seller = FakeSeller({})
    size = limits.limit("analytics_stocks.skus").value
    await stocks.snapshot_day(seller, db, shop_id="main",
                              skus=list(range(size * 2 + 5)), day=day)
    assert [len(c) for c in seller.chunks] == [size, size, 5]


# ── Пустота подтверждённая против неизвестной ────────────────────────────────


@pytest.mark.asyncio
async def test_a_sku_without_rows_is_confirmed_empty_not_unknown(db):
    """🔴 Замерено: на 300 запрошенных вернулось 39. Остальные — не пробел, а ноль."""
    day = tz.today_msk()
    seller = FakeSeller({1: [_stock_row(1, 100, 3)]})
    result = await stocks.snapshot_day(seller, db, shop_id="main",
                                       skus=[1, 2, 3], day=day)
    assert result.ok
    assert result.requested == 3
    assert result.skus_with_rows == 1
    assert result.skus_confirmed_empty == 2
    assert result.detail["sku с подтверждённым нулём"] == 2


@pytest.mark.asyncio
async def test_a_failed_chunk_makes_the_whole_snapshot_failed(db):
    """Частичный снимок, выданный за полный, — отказ, которого не видно."""
    day = tz.today_msk()
    seller = FakeSeller({i: [_stock_row(i, 100, 1)] for i in range(150)},
                        fail_chunks={0})
    result = await stocks.snapshot_day(seller, db, shop_id="main",
                                       skus=list(range(150)), day=day)

    assert not result.ok and result.status == "failed"
    assert result.chunks_failed == 1
    assert "Снимок неполон" in result.error
    # Снятое не выбрасывается: вторая порция записана.
    assert result.rows_written == 50

    assert await _rows(
        db, "SELECT status FROM collection_run WHERE source = ?", stocks.SOURCE
    ) == [("failed",)]


@pytest.mark.asyncio
async def test_a_successful_snapshot_is_recorded_as_a_run(db):
    """Тот же журнал, что у рекламы: «не снимали» обязано отличаться от «нечего снимать»."""
    day = tz.today_msk()
    result = await stocks.snapshot_day(FakeSeller({}), db, shop_id="main",
                                       skus=[1], day=day)
    assert result.ok
    assert await _rows(
        db, "SELECT day_msk, status, source FROM collection_run"
    ) == [(day, "ok", "snapshot")]


@pytest.mark.asyncio
async def test_a_row_without_sku_or_warehouse_is_refused(db):
    """Строку, которую нельзя ни найти, ни сверить, записывать нельзя."""
    day = tz.today_msk()
    broken = {"sku": None, "warehouse_id": 100, "available_stock_count": 1}
    with pytest.raises(ValueError, match="без sku или склада"):
        await stocks.snapshot_day(FakeSeller({1: [broken]}), db,
                                  shop_id="main", skus=[1], day=day)
    assert await _rows(db, "SELECT count(*) FROM stock_daily") == [(0,)]
    assert await _rows(
        db, "SELECT status FROM collection_run"
    ) == [("failed",)]


# ── Приёмка: сверка с таблицей товаров ───────────────────────────────────────


@pytest.mark.asyncio
async def test_coverage_is_measured_against_the_catalogue(db):
    """Приёмка D3: покрытие меряется таблицей товаров из D2, а не числом строк."""
    day = tz.today_msk()
    seller_products = _FakeCatalogue({1: [{"sku": 11, "source": "sds"},
                                          {"sku": 12, "source": "fbo"}],
                                      2: [{"sku": 21, "source": "sds"}]})
    await catalogue.rebuild(seller_products, db, shop_id="main")

    await stocks.snapshot_day(FakeSeller({11: [_stock_row(11, 100, 3)]}), db,
                              shop_id="main", skus=[11, 12, 21], day=day)

    coverage = await stocks.coverage_against_catalogue(db, shop_id="main", day=day)
    assert coverage["sku в таблице товаров"] == 3
    assert coverage["sku со строкой остатка"] == 1
    assert coverage["sku в снимке без карточки"] == 0


@pytest.mark.asyncio
async def test_a_stock_row_for_an_unknown_sku_is_surfaced(db):
    """Остаток по товару, которого нет в таблице, — значит таблица отстала."""
    day = tz.today_msk()
    await stocks.snapshot_day(FakeSeller({999: [_stock_row(999, 100, 1)]}), db,
                              shop_id="main", skus=[999], day=day)
    coverage = await stocks.coverage_against_catalogue(db, shop_id="main", day=day)
    assert coverage["sku в снимке без карточки"] == 1


class _FakeCatalogue:
    """Минимальный кабинет товаров для сверки покрытия."""

    def __init__(self, sources):
        self.sources = sources

    async def product_list(self, limit=100, last_id="", visibility="ALL"):
        if visibility == "ARCHIVED":
            return {"result": {"items": [], "last_id": "", "total": 0}}
        items = [{"product_id": pid, "offer_id": f"A{pid}", "name": "Т"}
                 for pid in self.sources]
        return {"result": {"items": items, "last_id": "", "total": len(items)}}

    async def product_info_list(self, product_id):
        return {"items": [{"id": pid, "sources": self.sources.get(pid, [])}
                          for pid in product_id]}


# ── Миграция 4: снимок и бэкфилл лежат РЯДОМ, а не поверх ────────────────────


@pytest.mark.asyncio
async def test_backfill_does_not_eat_the_snapshot(db):
    """🔴 Отрицательный сторож против дефекта, заложенного в A3.

    Прежний ключ `(date_msk, sku, warehouse, shop_id)` не включал `source`, а запись
    идёт через `INSERT OR REPLACE`. Проверено прогоном на той DDL: строка бэкфилла с тем
    же ключом оставляла `count(*) = 1`, и выживал `placement_report`. То есть бэкфилл не
    «смешивался» со снимком, как опасается ТЗ, а молча СЪЕДАЛ его: измеренный остаток
    подменялся величиной тарификации.

    Цена дефекта — вся приёмка D4. «Доля SKU с совпадением qty за день, где есть и
    снимок, и бэкфилл» на прежнем ключе невычислима: сравнивать не с чем уже в момент
    записи, а прогон при этом закрывается как успешный.
    """
    day = tz.today_msk()
    stamped = tz.now_msk_iso()
    common = dict(date_msk=day, sku=1, warehouse="100", shop_id="main",
                  fetched_at=stamped, warehouse_name="ХАБАРОВСК_2_РФЦ",
                  cluster_name="Дальний Восток", breakdown=None)

    await series.upsert_stock_daily(db, [{**common, "source": "snapshot", "qty": 3}])
    await series.upsert_stock_daily(
        db, [{**common, "source": "placement_report", "qty": 11}])

    rows = await _rows(db, "SELECT source, qty FROM stock_daily ORDER BY source")
    assert rows == [("placement_report", 11), ("snapshot", 3)], (
        "бэкфилл и снимок обязаны лежать рядом: иначе сверять D4 не с чем"
    )


@pytest.mark.asyncio
async def test_repeat_of_the_same_source_still_overwrites(db):
    """Повтор ОДНОГО источника по-прежнему перезаписывает — иначе день задвоится."""
    day = tz.today_msk()
    common = dict(date_msk=day, sku=1, warehouse="100", shop_id="main",
                  source="snapshot", fetched_at=tz.now_msk_iso(),
                  warehouse_name=None, cluster_name=None, breakdown=None)
    await series.upsert_stock_daily(db, [{**common, "qty": 3}])
    await series.upsert_stock_daily(db, [{**common, "qty": 4}])
    assert await _rows(db, "SELECT count(*), qty FROM stock_daily") == [(1, 4)]


@pytest.mark.asyncio
async def test_coverage_counts_only_the_snapshot(db):
    """Сверка покрытия обязана считать снимок, а не сумму двух источников."""
    day = tz.today_msk()
    common = dict(date_msk=day, warehouse="100", shop_id="main",
                  fetched_at=tz.now_msk_iso(), warehouse_name=None,
                  cluster_name=None, breakdown=None)
    await series.upsert_stock_daily(db, [
        {**common, "sku": 1, "source": "snapshot", "qty": 3},
        {**common, "sku": 2, "source": "placement_report", "qty": 9},
    ])
    coverage = await stocks.coverage_against_catalogue(db, shop_id="main", day=day)
    assert coverage["sku со строкой остатка"] == 1, (
        "в покрытие снимка попал sku, которого снимок не видел"
    )


@pytest.mark.asyncio
async def test_a_failed_chunk_carries_its_reason_into_the_journal(db):
    """🔴 Найдено живым прогоном 22.09.2026.

    Ночной снимок отдал `порций не снялось: 1` — и ни слова о том, почему. Причина
    вычислялась и терялась между вычислением и записью: в журнале прогонов оставался
    только счётчик, а разбирать приходилось по логу контейнера, который живёт до
    перезапуска. Факт отказа без его причины — половина отказа.
    """
    class Flaky:
        def __init__(self):
            self.calls = 0

        async def analytics_stocks(self, skus):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("Ozon ответил 500")
            return {"items": [{"sku": skus[0], "warehouse_id": 1,
                               "warehouse_name": "Склад", "cluster_name": "Кластер",
                               "available_stock_count": 7}]}

    skus = list(range(1, 251))
    result = await stocks.snapshot_day(Flaky(), db, shop_id="main", skus=skus, pause_s=0)

    assert not result.ok
    assert "порций не снялось: 1" in result.error
    assert "Ozon ответил 500" in result.error, "причина не доехала до журнала"

    async with db.execute(
        "SELECT status, error FROM collection_run WHERE source = 'snapshot'") as cur:
        status, error = await cur.fetchone()
    assert status == "failed"
    assert "Ozon ответил 500" in error, "причина не доехала до collection_run"
