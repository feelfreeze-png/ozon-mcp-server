"""C4: ежедневный сбор.

**Приёмка пакета:** два прогона подряд не задваивают строки; прогон за уже собранный
день перезаписывает, а не дублирует.

Сверх приёмки здесь закрепляется то, ради чего заведена таблица `collection_run`:
«строк за день нет» обязано различаться на «расхода не было» и «сбор не отработал». По
самому ряду их не отличить, а цена разная — первое нормально, второе означает потерянные
навсегда сутки.
"""

import pytest
import pytest_asyncio

from ozon_mcp import collector, series, timezones as tz


class FakePerf:
    """Кабинет из двух кампаний. Считает вызовы, чтобы проверять путь, а не только итог."""

    def __init__(self, rows_by_day=None, campaigns=(777, 888), fail_on=None):
        self.rows_by_day = rows_by_day or {}
        self._campaigns = list(campaigns)
        self.fail_on = fail_on
        self.calls = []

    async def campaigns_all(self, **kwargs):
        self.calls.append(("campaigns_all", kwargs))
        if self.fail_on == "campaigns":
            raise RuntimeError("список кампаний не отдался")
        return {"list": [{"id": str(c)} for c in self._campaigns],
                "total": len(self._campaigns), "pages": 1, "pageSize": 100}

    async def statistics_products_sku_all(self, campaigns, day, *, pause_s=None):
        self.calls.append(("sku", tuple(campaigns), day))
        if self.fail_on == "sku":
            raise RuntimeError("порция не собралась")
        return [dict(row) for row in self.rows_by_day.get(day, [])]


def _shop(shop_id, *, seller=True, performance=True):
    """Магазин так, как его отдаёт `cfg.get_shop_list` — вместе с тем, что он умеет."""
    return {"id": shop_id, "name": shop_id,
            "can": {"seller": seller, "performance": performance}}


def _row(sku, campaign, day, expense=1.5, **extra):
    base = {"sku": sku, "campaign_id": campaign, "date_msk": day,
            "expense": expense, "views": 100, "clicks": 5, "to_cart": 2,
            "orders": 1, "model_orders": 0, "sales": 900.0, "model_sales": 0.0,
            "price": 900.0, "ctr": 0.05}
    base.update(extra)
    return base


@pytest_asyncio.fixture
async def db(tmp_path):
    conn = await series.open_db(tmp_path)
    yield conn
    await conn.close()


async def _count(conn, table="ad_daily"):
    async with conn.execute(f"SELECT count(*) FROM {table}") as cur:
        return (await cur.fetchone())[0]


# ── Приёмка: повтор не задваивает ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_runs_in_a_row_do_not_double_the_rows(db):
    day = tz.yesterday_msk()
    perf = FakePerf({day: [_row(1, 777, day), _row(2, 888, day)]})

    first = await collector.collect_day(perf, db, shop_id="main", day=day, pause_s=0)
    second = await collector.collect_day(perf, db, shop_id="main", day=day, pause_s=0)

    assert first.rows_written == 2 and second.rows_written == 2
    assert await _count(db) == 2, "повторный прогон задвоил ряд"


@pytest.mark.asyncio
async def test_repeat_overwrites_rather_than_keeping_the_stale_value(db):
    """`INSERT OR REPLACE`, а не `IGNORE`: Ozon пересчитывает цифры дня задним числом.

    `IGNORE` оставил бы первую, более старую версию, и пересчёт до нас бы не дошёл.
    """
    day = tz.yesterday_msk()
    perf = FakePerf({day: [_row(1, 777, day, expense=1.5)]})
    await collector.collect_day(perf, db, shop_id="main", day=day, pause_s=0)

    perf.rows_by_day[day] = [_row(1, 777, day, expense=9.99)]
    await collector.collect_day(perf, db, shop_id="main", day=day, pause_s=0)

    async with db.execute("SELECT expense FROM ad_daily") as cur:
        assert [r[0] for r in await cur.fetchall()] == [9.99]


@pytest.mark.asyncio
async def test_two_shops_do_not_overwrite_each_other(db):
    day = tz.yesterday_msk()
    perf = FakePerf({day: [_row(1, 777, day)]})
    await collector.collect_day(perf, db, shop_id="main", day=day, pause_s=0)
    await collector.collect_day(perf, db, shop_id="второй", day=day, pause_s=0)
    assert await _count(db) == 2


@pytest.mark.asyncio
async def test_different_days_accumulate(db):
    today, yesterday = tz.today_msk(), tz.yesterday_msk()
    perf = FakePerf({yesterday: [_row(1, 777, yesterday)], today: [_row(1, 777, today)]})
    await collector.collect_day(perf, db, shop_id="main", day=yesterday, pause_s=0)
    await collector.collect_day(perf, db, shop_id="main", day=today, pause_s=0)
    assert await _count(db) == 2


# ── Что именно записалось ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_source_and_timestamp_are_stamped(db):
    """🔴 Снимок и бэкфилл смешивать нельзя — источник ставится при записи."""
    day = tz.yesterday_msk()
    perf = FakePerf({day: [_row(1, 777, day)]})
    await collector.collect_day(perf, db, shop_id="main", day=day, pause_s=0)

    async with db.execute("SELECT source, fetched_at, shop_id FROM ad_daily") as cur:
        source, fetched_at, shop = await cur.fetchone()
    assert source == "products_sku"
    assert shop == "main"
    assert fetched_at.endswith("+03:00"), "метка без московского пояса"


@pytest.mark.asyncio
async def test_result_carries_numbers_not_just_a_message(db):
    day = tz.yesterday_msk()
    perf = FakePerf({day: [_row(1, 777, day, expense=1.5), _row(1, 888, day, expense=2.5),
                           _row(2, 777, day, expense=3.0)]})
    result = await collector.collect_day(perf, db, shop_id="main", day=day, pause_s=0)

    assert result.ok and result.status == "ok"
    assert result.rows_written == 3
    assert result.distinct_sku == 2, "один SKU в двух кампаниях — это один SKU"
    assert result.expense_total == 7.0
    assert result.campaigns == 2


@pytest.mark.asyncio
async def test_yesterday_is_the_default_day(db):
    """Сегодняшние сутки ещё не закрыты — брать их как итог нельзя."""
    day = tz.yesterday_msk()
    perf = FakePerf({day: [_row(1, 777, day)]})
    result = await collector.collect_day(perf, db, shop_id="main", pause_s=0)
    assert result.day_msk == day


@pytest.mark.asyncio
async def test_row_from_another_day_is_refused(db):
    """Если Ozon отдаст не тот день, записать его значит сделать день бессмысленным."""
    day = tz.yesterday_msk()
    perf = FakePerf({day: [_row(1, 777, "2020-01-01")]})
    with pytest.raises(ValueError, match="перестанет означать день"):
        await collector.collect_day(perf, db, shop_id="main", day=day, pause_s=0)
    assert await _count(db) == 0


@pytest.mark.asyncio
async def test_new_field_from_ozon_reaches_the_result(db):
    """Изменение формы ответа обязано дойти до вызывающего, а не остаться в строке."""
    day = tz.yesterday_msk()
    perf = FakePerf({day: [_row(1, 777, day, _unknown={"новое": 1})]})
    result = await collector.collect_day(perf, db, shop_id="main", day=day, pause_s=0)
    assert result.unknown_fields == {"новое"}


# ── Три исхода: журнал прогонов ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_day_is_recorded_as_collected(db):
    """🔴 «Расхода не было» и «не собирали» обязаны различаться.

    По самому ряду их не отличить: в обоих случаях строк нет. Разницу несёт прогон.
    """
    day = tz.yesterday_msk()
    perf = FakePerf({day: []})
    result = await collector.collect_day(perf, db, shop_id="main", day=day, pause_s=0)

    assert result.ok and result.rows_written == 0
    async with db.execute(
        "SELECT status, rows_written FROM collection_run WHERE day_msk = ?", (day,)
    ) as cur:
        assert tuple(await cur.fetchone()) == ("ok", 0)


@pytest.mark.asyncio
async def test_a_day_never_attempted_has_no_run_at_all(db):
    """Обратная сторона: отсутствие прогона — это и есть «не собирали»."""
    async with db.execute("SELECT count(*) FROM collection_run") as cur:
        assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["campaigns", "sku"])
async def test_failure_is_recorded_and_raised_not_turned_into_emptiness(db, stage):
    """Отказ не становится пустым днём: он и записан как failed, и поднят наверх."""
    day = tz.yesterday_msk()
    perf = FakePerf({day: [_row(1, 777, day)]}, fail_on=stage)

    with pytest.raises(RuntimeError):
        await collector.collect_day(perf, db, shop_id="main", day=day, pause_s=0)

    async with db.execute(
        "SELECT status, error FROM collection_run WHERE day_msk = ?", (day,)
    ) as cur:
        status, error = tuple(await cur.fetchone())
    assert status == "failed"
    assert error and "RuntimeError" in error
    assert await _count(db) == 0


@pytest.mark.asyncio
async def test_run_is_written_before_going_to_ozon(db):
    """Прогон, оборвавшийся жёстко, обязан остаться видимым как `running`.

    Незавершённый сбор не должен выглядеть как отсутствие сбора.
    """
    day = tz.yesterday_msk()
    seen = {}

    class Watching(FakePerf):
        async def campaigns_all(self, **kwargs):
            async with db.execute(
                "SELECT status FROM collection_run WHERE day_msk = ?", (day,)
            ) as cur:
                row = await cur.fetchone()
                seen["во время запроса"] = tuple(row) if row else None
            return await super().campaigns_all(**kwargs)

    await collector.collect_day(Watching({day: []}), db, shop_id="main", day=day, pause_s=0)
    assert seen["во время запроса"] == ("running",), seen


@pytest.mark.asyncio
async def test_finish_run_refuses_a_third_status(db):
    run = await series.start_run(db, day_msk=tz.yesterday_msk(), shop_id="main",
                                 source="products_sku", started_at=tz.now_msk_iso())
    with pytest.raises(ValueError):
        await series.finish_run(db, run, status="наверное_ок",
                                finished_at=tz.now_msk_iso())


@pytest.mark.asyncio
async def test_collector_calls_the_client_directly_not_through_call_tool(db):
    """🔴 Путь вызова назван явно, потому что от него зависит порядок пакетов."""
    day = tz.yesterday_msk()
    perf = FakePerf({day: [_row(1, 777, day)]})
    await collector.collect_day(perf, db, shop_id="main", day=day, pause_s=0)

    assert [c[0] for c in perf.calls] == ["campaigns_all", "sku"]
    assert perf.calls[0][1]["adv_object_type"] == "SKU", "берутся не только SKU-кампании"


# ── Расписание и подключение к приложению ────────────────────────────────────


def test_next_run_is_the_configured_msk_hour(monkeypatch):
    """Час считается по МСК, а не по поясу хоста: сутки Ozon московские."""
    from datetime import datetime, timedelta

    from ozon_mcp import app

    for zone in ("Pacific/Kiritimati", "Etc/GMT+12"):
        import time as _time
        monkeypatch.setenv("TZ", zone)
        if hasattr(_time, "tzset"):
            _time.tzset()
        left = app._seconds_until_next_run()
        assert 0 < left <= 24 * 3600
        fires = tz.now_msk() + timedelta(seconds=left)
        assert fires.hour == app.COLLECT_AT_MSK_HOUR, (zone, fires.isoformat())
        assert fires.minute == 0
    monkeypatch.delenv("TZ", raising=False)
    import time as _time
    if hasattr(_time, "tzset"):
        _time.tzset()
    assert isinstance(datetime.now(), datetime)


@pytest.mark.asyncio
async def test_collect_says_so_out_loud_when_the_store_is_unavailable(monkeypatch, capsys):
    """🔴 Недоступное хранилище не должно выглядеть как «день без расхода».

    Сервер продолжает отвечать на чтение, и по его поведению отличить это нельзя —
    значит сказать обязан лог.
    """
    from ozon_mcp import app

    monkeypatch.setattr(series, "is_enabled", lambda: False)
    monkeypatch.setattr(app, "_series_error", "SeriesSchemaError: проверка")
    await app._collect_once()

    printed = capsys.readouterr().out
    assert "СБОР НЕ ИДЁТ" in printed
    assert "теряются безвозвратно" in printed


@pytest.mark.asyncio
async def test_one_shop_failing_does_not_stop_the_others(monkeypatch, capsys, db):
    """Отказ одного магазина уже записан как failed — соседей он останавливать не должен."""
    from ozon_mcp import app

    day = tz.yesterday_msk()
    good = FakePerf({day: [_row(1, 777, day)]})
    bad = FakePerf({day: []}, fail_on="campaigns")

    monkeypatch.setattr(series, "is_enabled", lambda: True)
    monkeypatch.setattr(series, "connection", lambda: db)
    monkeypatch.setattr(app.cfg, "get_shop_list",
                        lambda _d: [_shop("плохой"), _shop("хороший")])
    monkeypatch.setattr(app, "get_perf_for_shop",
                        lambda shop_id: bad if shop_id == "плохой" else good)

    await app._collect_once()

    printed = capsys.readouterr().out
    assert "СБОР ПРОВАЛЕН плохой" in printed
    assert "сбор хороший" in printed
    assert await _count(db) == 1, "сосед не собрался после чужого отказа"

    async with db.execute(
        "SELECT shop_id, status FROM collection_run ORDER BY shop_id"
    ) as cur:
        assert [tuple(r) for r in await cur.fetchall()] == [
            ("плохой", "failed"), ("хороший", "ok")]
