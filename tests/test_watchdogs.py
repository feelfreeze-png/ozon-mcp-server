"""C5: три сторожа ряда.

**Приёмка пакета — отрицательная, для каждого по отдельности:** удалить день
(непрерывность); подменить значение (сверка); сломать семантику (сумма). Отдельно:
день из нулей сторож непрерывности трогать не должен — расхода не было ≠ не собирали.

🔴 Без отрицательной приёмки «сторож молчит» неотличимо от «сторож не работает», а гейт
1→2 прямо требует различать эти два случая.
"""

import pytest
import pytest_asyncio

from ozon_mcp import series, timezones as tz, watchdogs

AD_COLUMNS = ", ".join(series.AD_DAILY_COLUMNS)
AD_INSERT = (f"INSERT OR REPLACE INTO ad_daily ({AD_COLUMNS}) "
             f"VALUES ({', '.join('?' * len(series.AD_DAILY_COLUMNS))})")


@pytest_asyncio.fixture
async def db(tmp_path):
    conn = await series.open_db(tmp_path)
    yield conn
    await conn.close()


async def _write_day(conn, day, *, shop="main", rows=((1, 777, 10.0, 2),)):
    for sku, campaign, expense, orders in rows:
        await conn.execute(AD_INSERT, (
            day, sku, campaign, shop, expense, 100, 5, 2, orders, 0,
            900.0, 0.0, 900.0, "products_sku", "2026-09-20T03:15:00+03:00",
        ))
    await conn.commit()


async def _write_run(conn, day, *, shop="main", status="ok", rows_written=1):
    run = await series.start_run(conn, day_msk=day, shop_id=shop,
                                 source="products_sku", started_at=tz.now_msk_iso())
    if status != "running":
        await series.finish_run(conn, run, status=status, finished_at=tz.now_msk_iso(),
                                campaigns=2, rows_written=rows_written)
    await conn.commit()
    return run


def _window(days):
    from datetime import date, timedelta

    last = date.fromisoformat(tz.yesterday_msk())
    return [(last - timedelta(days=offset)).isoformat() for offset in reversed(range(days))]


# ── Сторож непрерывности ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_continuous_series_is_quiet(db):
    for day in _window(5):
        await _write_run(db, day)
        await _write_day(db, day)
    verdict = await watchdogs.check_continuity(db, shop_id="main", days=5)
    assert verdict.quiet, verdict.alerts
    assert verdict.checked == 5


@pytest.mark.asyncio
async def test_removing_a_day_raises_an_alert(db):
    """Отрицательная приёмка сторожа непрерывности."""
    days = _window(5)
    for day in days:
        await _write_run(db, day)
        await _write_day(db, day)

    await db.execute("DELETE FROM ad_daily WHERE date_msk = ?", (days[2],))
    await db.execute("DELETE FROM collection_run WHERE day_msk = ?", (days[2],))
    await db.commit()

    verdict = await watchdogs.check_continuity(db, shop_id="main", days=5)
    assert not verdict.quiet
    assert any(days[2] in alert and "сутки потеряны" in alert for alert in verdict.alerts)


@pytest.mark.asyncio
async def test_a_day_of_zeroes_is_not_an_alert(db):
    """🔴 Расхода не было ≠ не собирали. Прогон есть, строк нет — это норма.

    Именно здесь сторож, построенный на одном лишь ряде, дал бы ложную тревогу
    каждый выходной без рекламы.
    """
    days = _window(4)
    for day in days:
        await _write_run(db, day, rows_written=0 if day == days[1] else 1)
        if day != days[1]:
            await _write_day(db, day)

    verdict = await watchdogs.check_continuity(db, shop_id="main", days=4)
    assert verdict.quiet, verdict.alerts
    assert verdict.detail["пустые дни"] == [days[1]]


@pytest.mark.asyncio
async def test_a_run_that_never_finished_is_an_alert(db):
    """Прогон в состоянии running — это не «собрано», а «оборвалось»."""
    days = _window(3)
    for day in days:
        await _write_run(db, day, status="running" if day == days[1] else "ok")
        await _write_day(db, day)

    verdict = await watchdogs.check_continuity(db, shop_id="main", days=3)
    assert any("не завершился" in alert for alert in verdict.alerts), verdict.alerts


@pytest.mark.asyncio
async def test_failed_run_is_an_alert(db):
    days = _window(3)
    for day in days:
        await _write_run(db, day, status="failed" if day == days[0] else "ok")
    verdict = await watchdogs.check_continuity(db, shop_id="main", days=3)
    assert any(days[0] in alert for alert in verdict.alerts), verdict.alerts


@pytest.mark.asyncio
async def test_empty_history_is_unknown_not_quiet(db):
    """🔴 «Нечего проверять» и «проверено, чисто» — разные ответы.

    Свернуть первое во второе значит выдать несостоявшуюся проверку за зелёный свет.
    """
    verdict = await watchdogs.check_continuity(db, shop_id="main", days=7)
    assert verdict.checked == 0
    assert not verdict.quiet, "пустая история выдана за «молчит по делу»"
    assert verdict.unknown
    assert "НЕ ПРОВЕРЕНО" in str(verdict)


@pytest.mark.asyncio
async def test_days_before_the_series_started_are_not_alerts(db):
    """Ряд начался позавчера — вчерашние дыры реальны, прошлогодние нет."""
    days = _window(10)
    for day in days[-3:]:
        await _write_run(db, day)
        await _write_day(db, day)
    verdict = await watchdogs.check_continuity(db, shop_id="main", days=10)
    assert verdict.quiet, verdict.alerts
    assert verdict.checked == 3


@pytest.mark.asyncio
async def test_another_shop_does_not_fill_the_gap(db):
    days = _window(3)
    for day in days:
        await _write_run(db, day, shop="сосед")
        await _write_day(db, day, shop="сосед")
    await _write_run(db, days[0])
    verdict = await watchdogs.check_continuity(db, shop_id="main", days=3)
    assert len(verdict.alerts) == 2, verdict.alerts


# ── Сторож сверки ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_matching_resnapshot_is_quiet(db):
    day = tz.yesterday_msk()
    await _write_day(db, day, rows=((1, 777, 10.0, 2), (2, 777, 5.5, 1)))
    fresh = [{"sku": 1, "campaign_id": 777, "expense": 10.0, "orders": 2},
             {"sku": 2, "campaign_id": 777, "expense": 5.5, "orders": 1}]
    verdict = await watchdogs.check_against_resnapshot(db, shop_id="main", day=day,
                                                       fresh_rows=fresh)
    assert verdict.quiet, verdict.alerts


@pytest.mark.asyncio
async def test_substituted_value_is_caught(db):
    """Отрицательная приёмка сторожа сверки: подменяем одно значение."""
    day = tz.yesterday_msk()
    await _write_day(db, day, rows=((1, 777, 10.0, 2),))
    fresh = [{"sku": 1, "campaign_id": 777, "expense": 12.5, "orders": 2}]
    verdict = await watchdogs.check_against_resnapshot(db, shop_id="main", day=day,
                                                       fresh_rows=fresh)
    assert not verdict.quiet
    assert any("расход" in alert and "12.5" in alert for alert in verdict.alerts)


@pytest.mark.asyncio
async def test_changed_orders_are_caught_too(db):
    day = tz.yesterday_msk()
    await _write_day(db, day, rows=((1, 777, 10.0, 2),))
    verdict = await watchdogs.check_against_resnapshot(
        db, shop_id="main", day=day,
        fresh_rows=[{"sku": 1, "campaign_id": 777, "expense": 10.0, "orders": 9}])
    assert any("заказы" in alert for alert in verdict.alerts), verdict.alerts


@pytest.mark.asyncio
async def test_a_row_appearing_or_vanishing_is_caught(db):
    day = tz.yesterday_msk()
    await _write_day(db, day, rows=((1, 777, 10.0, 2),))
    verdict = await watchdogs.check_against_resnapshot(
        db, shop_id="main", day=day,
        fresh_rows=[{"sku": 1, "campaign_id": 777, "expense": 10.0, "orders": 2},
                    {"sku": 42, "campaign_id": 777, "expense": 1.0, "orders": 0}])
    assert any("нет в накопленном" in alert for alert in verdict.alerts), verdict.alerts

    verdict = await watchdogs.check_against_resnapshot(db, shop_id="main", day=day,
                                                       fresh_rows=[])
    assert any("нет в переснятом" in alert for alert in verdict.alerts), verdict.alerts


@pytest.mark.asyncio
async def test_kopeck_rounding_is_within_tolerance(db):
    """Замерено при приёмке C1: расхождение в одну копейку — округление, а не дефект."""
    day = tz.yesterday_msk()
    await _write_day(db, day, rows=((1, 777, 10.00, 2),))
    verdict = await watchdogs.check_against_resnapshot(
        db, shop_id="main", day=day,
        fresh_rows=[{"sku": 1, "campaign_id": 777, "expense": 10.01, "orders": 2}])
    assert verdict.quiet, verdict.alerts


@pytest.mark.asyncio
async def test_nothing_to_compare_is_unknown_not_quiet(db):
    verdict = await watchdogs.check_against_resnapshot(
        db, shop_id="main", day=tz.yesterday_msk(), fresh_rows=[])
    assert verdict.checked == 0 and not verdict.quiet and verdict.unknown


# ── Сторож семантики ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sum_matching_daily_is_quiet(db):
    day = tz.yesterday_msk()
    await _write_day(db, day, rows=((1, 777, 10.0, 2), (2, 777, 5.0, 1)))
    daily = {"rows": [{"moneySpent": "15,00", "orders": "3"}]}
    verdict = await watchdogs.check_sum_against_daily(db, shop_id="main", day=day,
                                                      daily_payload=daily)
    assert verdict.quiet, verdict.alerts
    assert verdict.detail["расхождение"] == 0.0


@pytest.mark.asyncio
async def test_broken_semantics_is_caught(db):
    """Отрицательная приёмка сторожа семантики: daily перестал быть суммой."""
    day = tz.yesterday_msk()
    await _write_day(db, day, rows=((1, 777, 10.0, 2),))
    daily = {"rows": [{"moneySpent": "10.00", "orders": "7"}]}
    verdict = await watchdogs.check_sum_against_daily(db, shop_id="main", day=day,
                                                      daily_payload=daily)
    assert any("семантика заказов изменилась" in alert for alert in verdict.alerts)


@pytest.mark.asyncio
async def test_diverging_expense_is_caught(db):
    day = tz.yesterday_msk()
    await _write_day(db, day, rows=((1, 777, 10.0, 2),))
    daily = {"rows": [{"moneySpent": "99.00", "orders": "2"}]}
    verdict = await watchdogs.check_sum_against_daily(db, shop_id="main", day=day,
                                                      daily_payload=daily)
    assert any("расхождение" in alert for alert in verdict.alerts)


@pytest.mark.asyncio
async def test_model_orders_are_part_of_the_sum(db):
    """Замерено при приёмке C1: daily.orders = orders + modelOrders, а не один из них."""
    day = tz.yesterday_msk()
    await db.execute(AD_INSERT, (
        day, 1, 777, "main", 10.0, 100, 5, 2, 2, 1,
        900.0, 0.0, 900.0, "products_sku", "2026-09-20T03:15:00+03:00",
    ))
    await db.commit()
    verdict = await watchdogs.check_sum_against_daily(
        db, shop_id="main", day=day,
        daily_payload={"rows": [{"moneySpent": "10.00", "orders": "3"}]})
    assert verdict.quiet, verdict.alerts


@pytest.mark.asyncio
async def test_empty_sides_are_unknown_not_quiet(db):
    day = tz.yesterday_msk()
    verdict = await watchdogs.check_sum_against_daily(db, shop_id="main", day=day,
                                                      daily_payload={"rows": []})
    assert verdict.checked == 0 and not verdict.quiet and verdict.unknown


@pytest.mark.asyncio
async def test_comma_and_dot_both_parse_in_daily(db):
    """Разделитель у Ozon непостоянен — сторож не должен зависеть от его формы."""
    day = tz.yesterday_msk()
    await _write_day(db, day, rows=((1, 777, 2255.19, 2),))
    for form in ("2255,19", "2255.19"):
        verdict = await watchdogs.check_sum_against_daily(
            db, shop_id="main", day=day,
            daily_payload={"rows": [{"moneySpent": form, "orders": "2"}]})
        assert verdict.quiet, (form, verdict.alerts)
