"""E2: чтение накопленного ряда.

**Приёмка пакета:** ассистент получает ряд за неделю одним вызовом.

🔴 Главное свойство здесь не «данные вернулись», а то, что **покрытие идёт вместе с
данными**. Сумма расхода за неделю, в которой сбор не отработал во вторник, выглядит как
неделя с низким расходом. По самим строкам эти случаи неразличимы — разницу несёт журнал
прогонов, и поэтому он в каждом ответе.
"""

import pytest
import pytest_asyncio

from ozon_mcp import series, series_read, timezones as tz

AD_COLUMNS = ", ".join(series.AD_DAILY_COLUMNS)
AD_INSERT = (f"INSERT OR REPLACE INTO ad_daily ({AD_COLUMNS}) "
             f"VALUES ({', '.join('?' * len(series.AD_DAILY_COLUMNS))})")

WEEK = [f"2026-09-{day:02d}" for day in range(14, 21)]


@pytest_asyncio.fixture
async def db(tmp_path):
    conn = await series.open_db(tmp_path)
    yield conn
    await conn.close()


async def _write(conn, day, *, sku=1, campaign=777, expense=10.0, orders=2,
                 shop="main", views=100, clicks=5, sales=900.0):
    await conn.execute(AD_INSERT, (
        day, sku, campaign, shop, expense, views, clicks, 2, orders, 0,
        sales, 0.0, 450.0, "products_sku", "2026-09-20T03:15:00+03:00"))
    await conn.commit()


async def _run(conn, day, *, shop="main", status="ok", source="products_sku"):
    run = await series.start_run(conn, day_msk=day, shop_id=shop,
                                 source=source, started_at=tz.now_msk_iso())
    if status != "running":
        await series.finish_run(conn, run, status=status,
                                finished_at=tz.now_msk_iso(), rows_written=1)
    await conn.commit()


# ── Приёмка: неделя одним вызовом ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_full_week_comes_back_in_one_call(db):
    for day in WEEK:
        await _run(db, day)
        await _write(db, day)

    got = await series_read.ad_series(db, shop_id="main",
                                      date_from=WEEK[0], date_to=WEEK[-1])
    assert len(got["rows"]) == 7
    assert got["totals"]["expense"] == pytest.approx(70.0)
    assert got["totals"]["orders"] == 14
    assert got["coverage"]["ряд полон"] is True
    assert got["период"] == {"с": WEEK[0], "по": WEEK[-1], "пояс": "МСК"}


# ── Покрытие: четыре состояния дня ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_gap_in_collection_is_visible_next_to_the_numbers(db):
    """🔴 Неделя с пропущенным вторником выглядит неделей с низким расходом.

    По строкам это неотличимо. Блок покрытия — единственное, что различает.
    """
    for day in WEEK:
        if day == WEEK[2]:
            continue
        await _run(db, day)
        await _write(db, day)

    got = await series_read.ad_series(db, shop_id="main",
                                      date_from=WEEK[0], date_to=WEEK[-1])
    assert got["coverage"]["сбора не было"] == [WEEK[2]]
    assert got["coverage"]["ряд полон"] is False
    assert got["totals"]["expense"] == pytest.approx(60.0), (
        "сумма меньше — и без покрытия это читалось бы как спад расхода"
    )


@pytest.mark.asyncio
async def test_four_day_states_do_not_merge(db):
    await _run(db, WEEK[0], status="ok")
    await _write(db, WEEK[0])
    await _run(db, WEEK[1], status="failed")
    await _run(db, WEEK[2], status="running")
    # WEEK[3] — прогона нет вовсе

    got = await series_read.coverage(db, shop_id="main",
                                     date_from=WEEK[0], date_to=WEEK[3])
    assert got["собрано"] == 1
    assert got["сбор провалился"] == [WEEK[1]]
    assert got["сбор не завершён"] == [WEEK[2]]
    assert got["сбора не было"] == [WEEK[3]]


@pytest.mark.asyncio
async def test_a_collected_day_without_spend_is_not_a_gap(db):
    """Прогон был, строк нет — расхода не было. Это ответ, а не пробел."""
    for day in WEEK[:3]:
        await _run(db, day)
    got = await series_read.ad_series(db, shop_id="main",
                                      date_from=WEEK[0], date_to=WEEK[2])
    assert got["rows"] == []
    assert got["coverage"]["ряд полон"] is True, (
        "день без расхода превратился в пробел — сторож непрерывности так же ошибётся"
    )


# ── Изоляция арендаторов ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_query_without_a_shop_is_refused(db):
    """⚠️ Таблицы общие. Запрос без магазина прочитал бы чужой расход как свой."""
    with pytest.raises(series_read.SeriesReadError, match="не задан магазин"):
        await series_read.ad_series(db, shop_id="", date_from=WEEK[0], date_to=WEEK[-1])


@pytest.mark.asyncio
async def test_a_neighbour_is_not_included(db):
    await _run(db, WEEK[0])
    await _write(db, WEEK[0], expense=10.0)
    await _run(db, WEEK[0], shop="сосед")
    await _write(db, WEEK[0], shop="сосед", expense=999.0)

    got = await series_read.ad_series(db, shop_id="main",
                                      date_from=WEEK[0], date_to=WEEK[0])
    assert got["totals"]["expense"] == pytest.approx(10.0)


# ── Разрезы ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("group_by,expected", [
    ("day", 2), ("sku", 2), ("campaign", 2), ("sku_day", 4), ("campaign_day", 4),
])
async def test_groupings(db, group_by, expected):
    for day in WEEK[:2]:
        await _run(db, day)
        for sku, campaign in ((1, 777), (2, 888)):
            await _write(db, day, sku=sku, campaign=campaign)
    got = await series_read.ad_series(db, shop_id="main", date_from=WEEK[0],
                                      date_to=WEEK[1], group_by=group_by)
    assert len(got["rows"]) == expected


@pytest.mark.asyncio
async def test_an_unknown_grouping_is_refused(db):
    with pytest.raises(series_read.SeriesReadError, match="неизвестен"):
        await series_read.ad_series(db, shop_id="main", date_from=WEEK[0],
                                    date_to=WEEK[0], group_by="как-нибудь")


@pytest.mark.asyncio
async def test_ctr_is_built_from_sums_not_averaged(db):
    """Среднее от средних расходится с правдой и делает это тихо."""
    await _run(db, WEEK[0])
    await _write(db, WEEK[0], sku=1, views=1000, clicks=10)
    await _write(db, WEEK[0], sku=2, views=10, clicks=5)
    got = await series_read.ad_series(db, shop_id="main", date_from=WEEK[0],
                                      date_to=WEEK[0], group_by="day")
    (row,) = got["rows"]
    assert row["ctr"] == pytest.approx(15 / 1010)
    assert row["ctr"] != pytest.approx((10 / 1000 + 5 / 10) / 2)


# ── ДРР здесь не считается ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drr_is_not_computed_here(db):
    """🔴 Две величины под одним именем хуже, чем одна в другом месте.

    Правило ДРР сложнее деления: из расхода исключается «Оплата за заказ: все товары».
    Посчитав ДРР и здесь, мы завели бы в системе два разных ДРР, и какой попал в отчёт,
    выяснялось бы задним числом.
    """
    await _run(db, WEEK[0])
    await _write(db, WEEK[0], expense=10.0, sales=100.0)
    got = await series_read.ad_series(db, shop_id="main", date_from=WEEK[0],
                                      date_to=WEEK[0])
    assert "drr" not in got["rows"][0]
    assert "drr" not in got["totals"]
    assert {"expense", "sales"} <= set(got["rows"][0]), "слагаемые обязаны быть отданы"


# ── Фильтры, период, усечение ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_filters_narrow_the_answer(db):
    await _run(db, WEEK[0])
    await _write(db, WEEK[0], sku=1, campaign=777)
    await _write(db, WEEK[0], sku=2, campaign=888)

    by_sku = await series_read.ad_series(db, shop_id="main", date_from=WEEK[0],
                                         date_to=WEEK[0], group_by="sku", skus=[1])
    assert [row["sku"] for row in by_sku["rows"]] == [1]

    by_campaign = await series_read.ad_series(db, shop_id="main", date_from=WEEK[0],
                                              date_to=WEEK[0], group_by="campaign",
                                              campaigns=[888])
    assert [row["campaign_id"] for row in by_campaign["rows"]] == [888]


@pytest.mark.asyncio
async def test_a_reversed_period_is_refused(db):
    with pytest.raises(series_read.SeriesReadError, match="перевёрнут"):
        await series_read.ad_series(db, shop_id="main", date_from=WEEK[-1],
                                    date_to=WEEK[0])


@pytest.mark.asyncio
async def test_a_timestamp_period_is_refused(db):
    with pytest.raises(tz.TimezoneContractError):
        await series_read.ad_series(db, shop_id="main",
                                    date_from="2026-09-14T00:00:00Z", date_to=WEEK[-1])


@pytest.mark.asyncio
async def test_truncation_says_so(db):
    """Тот же признак, что на MCP-пути: короткий ответ обязан объявлять себя коротким."""
    await _run(db, WEEK[0])
    for sku in range(5):
        await _write(db, WEEK[0], sku=sku)
    got = await series_read.ad_series(db, shop_id="main", date_from=WEEK[0],
                                      date_to=WEEK[0], group_by="sku", limit=2)
    assert got["_truncated"] is True and got["_shown"] == 2 and got["_limit"] == 2


# ── Остатки: источники не смешиваются ────────────────────────────────────────


@pytest.mark.asyncio
async def test_stock_sources_are_never_mixed(db):
    """⚠️ Снимок — измеренный остаток, отчёт — величина тарификации. Разные вещи."""
    common = dict(date_msk=WEEK[0], sku=1, warehouse="100", shop_id="main",
                  fetched_at=tz.now_msk_iso(), warehouse_name=None,
                  cluster_name=None, breakdown=None)
    await series.upsert_stock_daily(db, [
        {**common, "source": "snapshot", "qty": 3},
        {**common, "source": "placement_report", "qty": 11},
    ])
    await _run(db, WEEK[0], source="snapshot")

    snapshot = await series_read.stock_series(db, shop_id="main", date_from=WEEK[0],
                                              date_to=WEEK[0], source="snapshot")
    assert [row["qty"] for row in snapshot["rows"]] == [3]

    report = await series_read.stock_series(db, shop_id="main", date_from=WEEK[0],
                                            date_to=WEEK[0], source="placement_report")
    assert [row["qty"] for row in report["rows"]] == [11]


# ── Названия товаров ─────────────────────────────────────────────────────────


async def _card(conn, product_id, sku, name, *, shop="main", offer=None):
    await conn.execute(
        "INSERT INTO product (product_id, shop_id, offer_id, name, archived, updated_at) "
        "VALUES (?, ?, ?, ?, 0, ?)",
        (product_id, shop, offer or f"A{product_id}", name, tz.now_msk_iso()))
    await conn.execute(
        "INSERT INTO product_sku (product_id, shop_id, sku, source) VALUES (?, ?, ?, 'sds')",
        (product_id, shop, sku))
    await conn.commit()


@pytest.mark.asyncio
async def test_names_come_back_by_sku(db):
    await _card(db, 1, 101, "Пищевое ведро 8 л")
    await _card(db, 2, 102, "Сковорода 34 см")
    got = await series_read.names_for_skus(db, shop_id="main", skus=[101, 102])
    assert got == {101: "Пищевое ведро 8 л", 102: "Сковорода 34 см"}


@pytest.mark.asyncio
async def test_two_skus_of_one_card_share_its_name(db):
    """У карточки бывает два `sku` — оба должны находиться."""
    await _card(db, 1, 101, "Пищевое ведро 8 л")
    await db.execute(
        "INSERT INTO product_sku (product_id, shop_id, sku, source) VALUES (1, 'main', 202, 'fbo')")
    await db.commit()
    got = await series_read.names_for_skus(db, shop_id="main", skus=[101, 202])
    assert got == {101: "Пищевое ведро 8 л", 202: "Пищевое ведро 8 л"}


@pytest.mark.asyncio
async def test_a_sku_without_a_card_is_absent_not_blank(db):
    """🔴 Пропуск обязан быть виден: его причина — отставший каталог."""
    await _card(db, 1, 101, "Пищевое ведро 8 л")
    got = await series_read.names_for_skus(db, shop_id="main", skus=[101, 999])
    assert got == {101: "Пищевое ведро 8 л"}
    assert 999 not in got


@pytest.mark.asyncio
async def test_an_empty_name_is_not_a_name(db):
    """Пустая строка в отчёте выглядит как название из пробела."""
    await _card(db, 1, 101, "   ")
    await _card(db, 2, 102, None)
    assert await series_read.names_for_skus(db, shop_id="main", skus=[101, 102]) == {}


@pytest.mark.asyncio
async def test_a_neighbours_name_is_not_returned(db):
    """Таблицы общие для арендаторов — разделение колонкой, а не доверием."""
    await _card(db, 1, 101, "Чужое ведро", shop="другой")
    assert await series_read.names_for_skus(db, shop_id="main", skus=[101]) == {}


@pytest.mark.asyncio
async def test_batches_are_assembled_without_losing_or_doubling_rows(db):
    """Сборка из нескольких порций обязана дать то же, что дал бы один запрос.

    ⚠️ Прежняя редакция называлась «sku больше, чем SQLite берёт одним IN» и опиралась
    на предел в 999 параметров. Мутационная проверка показала, что тест проходит и при
    порции 5000, то есть не доказывает ничего: предел замерен и равен **32 766** у
    разработчика (sqlite 3.50.4) и **250 000** на проде (3.46.1) — 999 снят в 3.32.
    Настоящий дефект здесь другой: потерянная или задвоенная порция. Его и ловим —
    1200 `sku` при порции 400 это ровно три прохода.
    """
    assert series_read._NAME_BATCH * 2 < 1200, "порция выросла — тест перестал бить по швам"
    for i in range(1200):
        await db.execute(
            "INSERT INTO product (product_id, shop_id, offer_id, name, archived, updated_at) "
            "VALUES (?, 'main', ?, ?, 0, ?)",
            (i, f"A{i}", f"Товар {i}", tz.now_msk_iso()))
        await db.execute(
            "INSERT INTO product_sku (product_id, shop_id, sku, source) "
            "VALUES (?, 'main', ?, 'sds')", (i, 1000 + i))
    await db.commit()

    got = await series_read.names_for_skus(
        db, shop_id="main", skus=list(range(1000, 2200)))
    assert len(got) == 1200
    assert got[1000] == "Товар 0" and got[2199] == "Товар 1199"


@pytest.mark.asyncio
async def test_an_empty_request_asks_the_database_nothing(db):
    assert await series_read.names_for_skus(db, shop_id="main", skus=[]) == {}
    assert await series_read.names_for_skus(db, shop_id="main", skus=[None]) == {}
