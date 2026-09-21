"""E1: знаменатель отчёта и сторож молчаливого выбрасывания.

🔴 **Приёмка пакета — на ЧАСТИЧНО неверном наборе.** Тест, где неверны ВСЕ метрики,
зеленеет и без нашего сторожа: ошибку выдаст сам Ozon по своей причине. Настоящий
дефект — молчаливое выбрасывание части набора при успешном ответе.

**Замер 21.09.2026, определивший конструкцию.** Метрики приходят массивом по позициям, и
выброшенная не оставляет дырку — остальные сдвигаются влево::

    запрошено ["ordered_units", "revenue"]  → totals [177, 415580]
    запрошено ["НЕТ_ТАКОЙ",     "revenue"]  → totals [415580]

Читающий `metrics[0]` как заказы получил бы 415580 вместо 177 — выручку под видом числа
заказов, в две с лишним тысячи раз больше. Код ответа при этом 200.
"""

import asyncio

import pytest

from ozon_mcp import analytics, timezones as tz


class FakeSeller:
    """Повторяет замеренное поведение: неизвестные имена выбрасываются молча."""

    def __init__(self, *, known_metrics=None, known_dimensions=None, rows=None):
        self.known_metrics = known_metrics or {
            "ordered_units": 8, "revenue": 5392, "returns": 1}
        self.known_dimensions = known_dimensions or {"sku", "day"}
        self.rows = rows
        self.calls = []

    async def analytics_data(self, date_from, date_to, metrics, dimensions,
                             filters=None, limit=1000, offset=0):
        self.calls.append((tuple(metrics), tuple(dimensions), limit, offset))
        kept_metrics = [m for m in metrics if m in self.known_metrics]
        kept_dims = [d for d in dimensions if d in self.known_dimensions]
        if self.rows is not None:
            data = self.rows[offset:offset + limit]
        else:
            data = [{
                "dimensions": [{"id": "913050946", "name": "Товар"}
                               if d == "sku" else {"id": "2026-09-20", "name": ""}
                               for d in kept_dims],
                "metrics": [self.known_metrics[m] for m in kept_metrics],
            }]
        return {"result": {"data": data,
                           "totals": [self.known_metrics[m] for m in kept_metrics]}}


# ── Белый список снят опытом ─────────────────────────────────────────────────


def test_the_whitelist_is_what_was_measured():
    """Девять метрик и семь измерений, каждое проверено парой с заведомо рабочей."""
    assert "ordered_units" in analytics.METRICS
    assert {"sku", "day"} <= analytics.DIMENSIONS
    assert "adv_view_pdp" not in analytics.METRICS
    assert "modelid" not in analytics.DIMENSIONS


def test_the_measured_rejections_are_remembered_with_their_reason():
    """Проверенный отказ не должен проверяться заново и приниматься за опечатку."""
    assert "выброшена молча" in analytics.KNOWN_REJECTED["adv_view_pdp"]
    assert "400" in analytics.KNOWN_REJECTED["modelid"]


def test_funnel_metrics_are_in_the_list_because_the_measurement_says_so():
    """⚠️ Поправка к нашей документации: они записаны снятыми, а замер их не подтверждает.

    За 14–20.09 вернули 496 204 показа, 233 629 сессий и позицию 94,3 — осмысленные
    числа, а не нули.
    """
    assert {"hits_view", "session_view", "position_category"} <= analytics.METRICS


@pytest.mark.parametrize("bad", ["adv_view_pdp", "выдуманная", ""])
def test_an_unknown_metric_is_refused_before_sending(bad):
    """Ответ на такой запрос придёт кодом 200 и будет выглядеть данными."""
    with pytest.raises(analytics.AnalyticsContractError, match="не в проверенном списке"):
        analytics.check_request(["ordered_units", bad], ["sku"])


def test_an_unknown_dimension_is_refused_before_sending():
    with pytest.raises(analytics.AnalyticsContractError, match="измерение"):
        analytics.check_request(["ordered_units"], ["sku", "modelid"])


def test_an_empty_request_is_refused():
    with pytest.raises(analytics.AnalyticsContractError):
        analytics.check_request([], ["sku"])
    with pytest.raises(analytics.AnalyticsContractError):
        analytics.check_request(["ordered_units"], [])


# ── Сторож состава ответа: главная приёмка пакета ────────────────────────────


@pytest.mark.asyncio
async def test_a_partially_wrong_set_is_caught(monkeypatch):
    """🔴 Приёмка E1. Запрошено N, вернулось N−1 — и это обязано быть отказом.

    Проверка идёт в обход белого списка: сторож ответа должен работать сам по себе, а
    не полагаться на то, что до него уже отсеяли. Иначе он проверяет отсеиватель.
    """
    monkeypatch.setattr(analytics, "METRICS", analytics.METRICS | {"НЕТ_ТАКОЙ"})
    seller = FakeSeller()
    with pytest.raises(analytics.AnalyticsContractError, match="сдвигаются"):
        await analytics.fetch(seller, date_from="2026-09-18", date_to="2026-09-20",
                              metrics=["ordered_units", "НЕТ_ТАКОЙ"], dimensions=["sku"])


@pytest.mark.asyncio
async def test_the_measured_shift_is_reproduced_and_refused(monkeypatch):
    """Ровно замеренный случай: выдуманная ПЕРВОЙ, выручка съезжает на её место."""
    monkeypatch.setattr(analytics, "METRICS", analytics.METRICS | {"НЕТ_ТАКОЙ"})
    seller = FakeSeller(known_metrics={"revenue": 415580})
    with pytest.raises(analytics.AnalyticsContractError) as caught:
        await analytics.fetch(seller, date_from="2026-09-18", date_to="2026-09-20",
                              metrics=["НЕТ_ТАКОЙ", "revenue"], dimensions=["sku"])
    assert "вернулось 1" in str(caught.value)


@pytest.mark.asyncio
async def test_a_dropped_dimension_is_caught(monkeypatch):
    monkeypatch.setattr(analytics, "DIMENSIONS", analytics.DIMENSIONS | {"выдуманное"})
    seller = FakeSeller()
    with pytest.raises(analytics.AnalyticsContractError, match="измерений"):
        await analytics.fetch(seller, date_from="2026-09-18", date_to="2026-09-20",
                              metrics=["ordered_units"], dimensions=["sku", "выдуманное"])


@pytest.mark.asyncio
async def test_totals_are_checked_too(monkeypatch):
    """Выброшенная метрика исчезает и из итогов — замерено."""
    monkeypatch.setattr(analytics, "METRICS", analytics.METRICS | {"НЕТ_ТАКОЙ"})

    class ShortTotals(FakeSeller):
        async def analytics_data(self, *a, **kw):
            payload = await super().analytics_data(*a, **kw)
            payload["result"]["data"] = [
                {"dimensions": [{"id": "1", "name": "Т"}], "metrics": [8, 5392]}]
            return payload

    with pytest.raises(analytics.AnalyticsContractError, match="totals"):
        await analytics.fetch(ShortTotals(), date_from="2026-09-18", date_to="2026-09-20",
                              metrics=["ordered_units", "НЕТ_ТАКОЙ"], dimensions=["sku"])


# ── Форма наружу: словарь, а не массив ───────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_come_out_named_not_positional():
    """Словарь делает сдвиг невозможным, а не только обнаруживаемым."""
    seller = FakeSeller()
    got = await analytics.fetch(seller, date_from="2026-09-18", date_to="2026-09-20",
                                metrics=["ordered_units", "revenue"],
                                dimensions=["sku", "day"])
    (row,) = got.rows
    assert row["metrics"] == {"ordered_units": 8, "revenue": 5392}
    assert row["dimensions"]["sku"]["id"] == "913050946"
    assert row["dimensions"]["day"]["id"] == "2026-09-20"
    assert got.totals == {"ordered_units": 8, "revenue": 5392}


@pytest.mark.asyncio
async def test_a_timestamp_period_is_refused():
    with pytest.raises(tz.TimezoneContractError):
        await analytics.fetch(FakeSeller(), date_from="2026-09-18T00:00:00Z",
                              date_to="2026-09-20", metrics=["ordered_units"],
                              dimensions=["sku"])


# ── Обход страниц без оракула полноты ────────────────────────────────────────


def _row(sku, day, orders):
    return {"dimensions": [{"id": str(sku), "name": "Т"}, {"id": day, "name": ""}],
            "metrics": [orders]}


@pytest.mark.asyncio
async def test_the_walk_stops_on_a_short_page(monkeypatch):
    monkeypatch.setattr(analytics, "PAGE_PAUSE_S", 0)
    rows = [_row(i, "2026-09-20", 1) for i in range(25)]
    seller = FakeSeller(known_metrics={"ordered_units": 1}, rows=rows)
    got = await analytics.fetch_all(seller, date_from="2026-09-20", date_to="2026-09-20",
                                    metrics=["ordered_units"], dimensions=["sku", "day"],
                                    page_size=10)
    assert len(got.rows) == 25 and got.pages == 3 and not got.truncated


@pytest.mark.asyncio
async def test_hitting_the_page_ceiling_marks_the_result_truncated(monkeypatch):
    """⚠️ Оракула полноты у эндпоинта НЕТ — значит потолок обязан быть виден."""
    monkeypatch.setattr(analytics, "PAGE_PAUSE_S", 0)
    monkeypatch.setattr(analytics, "MAX_PAGES", 3)
    rows = [_row(i, "2026-09-20", 1) for i in range(100)]
    seller = FakeSeller(known_metrics={"ordered_units": 1}, rows=rows)
    got = await analytics.fetch_all(seller, date_from="2026-09-20", date_to="2026-09-20",
                                    metrics=["ordered_units"], dimensions=["sku", "day"],
                                    page_size=10)
    assert got.truncated and got.pages == 3


@pytest.mark.asyncio
async def test_a_truncated_walk_never_becomes_a_denominator(monkeypatch):
    """Усечённый знаменатель завысил бы долю рекламных заказов — молча."""
    monkeypatch.setattr(analytics, "PAGE_PAUSE_S", 0)
    monkeypatch.setattr(analytics, "MAX_PAGES", 2)
    rows = [_row(i, "2026-09-20", 1) for i in range(100)]
    seller = FakeSeller(known_metrics={"ordered_units": 1}, rows=rows)
    with pytest.raises(analytics.AnalyticsContractError, match="доля получится завышенной"):
        await analytics.orders_by_sku_day(seller, date_from="2026-09-20",
                                          date_to="2026-09-20", page_size=10, pause_s=0)


# ── Знаменатель ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_denominator_is_keyed_by_sku_and_moscow_day(monkeypatch):
    monkeypatch.setattr(analytics, "PAGE_PAUSE_S", 0)
    rows = [_row(1, "2026-09-19", 3), _row(1, "2026-09-20", 5), _row(2, "2026-09-20", 7)]
    seller = FakeSeller(known_metrics={"ordered_units": 1}, rows=rows)
    got = await analytics.orders_by_sku_day(seller, date_from="2026-09-19",
                                            date_to="2026-09-20", pause_s=0)
    assert got == {(1, "2026-09-19"): 3, (1, "2026-09-20"): 5, (2, "2026-09-20"): 7}


@pytest.mark.asyncio
async def test_a_timestamp_in_the_day_dimension_is_refused(monkeypatch):
    """⚠️ `day` уже московский. Метка времени на его месте развалила бы ключ."""
    monkeypatch.setattr(analytics, "PAGE_PAUSE_S", 0)
    rows = [_row(1, "2026-09-20T00:00:00Z", 3)]
    seller = FakeSeller(known_metrics={"ordered_units": 1}, rows=rows)
    with pytest.raises(tz.TimezoneContractError):
        await analytics.orders_by_sku_day(seller, date_from="2026-09-20",
                                          date_to="2026-09-20", pause_s=0)


@pytest.mark.asyncio
async def test_the_denominator_asks_for_exactly_one_metric(monkeypatch):
    """Лишняя метрика в знаменателе — лишний повод для сдвига."""
    monkeypatch.setattr(analytics, "PAGE_PAUSE_S", 0)
    seller = FakeSeller(known_metrics={"ordered_units": 4},
                        rows=[_row(1, "2026-09-20", 4)])
    await analytics.orders_by_sku_day(seller, date_from="2026-09-20",
                                      date_to="2026-09-20", pause_s=0)
    metrics, dimensions, _, _ = seller.calls[0]
    assert metrics == ("ordered_units",)
    assert dimensions == ("sku", "day")
